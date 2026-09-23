"""Real loopback HTTP for the opt-in browser-free generation handoff."""
import asyncio
import gzip
import json
import subprocess
from contextlib import asynccontextmanager
from io import BytesIO

import httpx
import pytest
from test_subchat_http_catalog import catalog
from test_subchat_http_only import CATALOG_URL, SECRET, credentials, session_payload
from test_subchat_http_only_cli import command, environment

from anywhere_computer.models import Request
from anywhere_computer.state import Ledger
from anywhere_computer.subchat import SubchatOutcomeUnknown, Subchats
from anywhere_computer.subchat_cli import Command, dispatch
from anywhere_computer.subchat_http import HTTPOnlySubchatBackend
from anywhere_computer.subchat_http_generation import (
    ObservedHTTPGeneration,
    _json_response,
    read_http_generation_handoff,
)
from anywhere_computer.subchat_http_sender import HTTPGenerationPlan
from anywhere_computer.subchat_mcp import session as mcp_session
from anywhere_computer.subchat_state import (
    SubchatHTTPSelection,
    SubchatSubmission,
    SubchatSubmissions,
)

CHAT = '00000000-0000-0000-0000-000000000001'
SELECTION = SubchatHTTPSelection(version_id='future', preset_id=7,
    model_slug='future-chat', thinking_effort='future-effort')
PROOF = 'fixture-protection-value'


def handoff_data():
    headers = dict.fromkeys((
        'accept authorization chatgpt-account-id content-type cookie oai-did oai-echo-logs '
        'oai-language openai-sentinel-chat-requirements-prepare-token '
        'openai-sentinel-proof-token openai-sentinel-turnstile-token origin originator '
        'referer sec-ch-ua sec-ch-ua-arch sec-ch-ua-bitness sec-ch-ua-full-version '
        'sec-ch-ua-full-version-list sec-ch-ua-mobile sec-ch-ua-model sec-ch-ua-platform '
        'sec-ch-ua-platform-version user-agent x-oai-turn-trace-id x-openai-codex-window-type '
        'x-openai-web-frontend x-openai-web-sse-compression').split(), 'fixture')
    headers.update(authorization=SECRET, **{'chatgpt-account-id': 'fixture-account',
        'accept': 'text/event-stream', 'content-type': 'application/json',
        'cookie': 'session=' + PROOF,
        'origin': 'https://chatgpt.com', 'referer': 'https://chatgpt.com/',
        'openai-sentinel-proof-token': PROOF,
        'openai-sentinel-turnstile-token': PROOF})
    return {'headers': headers, 'sentinel_p': 'observed-p',
            'prepare_template': {'client_prepare_state': 'sent',
                'action': 'next', 'model': 'old-model', 'thinking_effort': 'old-effort',
                'is_do_not_remember': False, 'timezone': 'Asia/Tokyo',
                'timezone_offset_min': -540, 'local_function_names': [],
                'partial_query': {'id': 'old-input', 'author': {'role': 'user'},
                                  'content': {'content_type': 'text',
                                              'parts': ['old prompt'], 'extra': 'keep'},
                                  'extra': 'keep'}},
            'generation_template': {'client_prepare_state': 'sent',
                'action': 'next', 'messages': [{'id': 'old-input',
                    'author': {'role': 'user'},
                    'content': {'content_type': 'text', 'parts': ['old prompt']},
                    'metadata': {'is_visually_hidden_from_conversation': False},
                    'channel': None, 'recipient': 'all', 'status': 'finished_successfully',
                    'end_turn': None, 'weight': 1.0, 'create_time': 1.0,
                    'update_time': 1.0}],
                'model': 'old-model', 'thinking_effort': 'old-effort',
                'conversation_id': 'old-conversation', 'parent_message_id': 'old-parent'}}


def handoff():
    data = handoff_data()
    return ObservedHTTPGeneration.from_data(data, authorization=SECRET,
                                            account_id='fixture-account')


class LocalChat:
    def __init__(self, *, prepare_token_present=True, rotate_cookie=False,
                 compress_generation=False, oversize_sentinel=False):
        self.requests = []
        self.messages = []
        self.prepare_token_present = prepare_token_present
        self.rotate_cookie = rotate_cookie
        self.compress_generation = compress_generation
        self.oversize_sentinel = oversize_sentinel
        self.current_node = None
        self.stale = False
        self.lost_generation = False
        self.lost_after_candidate = False
        self.fail_stage = None

    async def __aenter__(self):
        self.server = await asyncio.start_server(self.handle, '127.0.0.1', 0)
        self.origin = 'http://127.0.0.1:' + str(self.server.sockets[0].getsockname()[1])
        return self

    async def __aexit__(self, *_):
        self.server.close()
        await self.server.wait_closed()

    async def handle(self, reader, writer):
        try:
            head = await reader.readuntil(b'\r\n\r\n')
            lines = head.decode('iso-8859-1').split('\r\n')
            method, path, _ = lines[0].split(' ', 2)
            headers = dict(line.lower().split(': ', 1) for line in lines[1:] if ': ' in line)
            body = await reader.readexactly(int(headers.get('content-length', '0')))
            data = json.loads(body) if body else None
            self.requests.append((method, path, headers, data))
            status = '200 OK'
            content_type = 'application/json'
            if path.startswith('/backend-api/models?'):
                payload = json.dumps(catalog()).encode()
            elif path == '/backend-api/sentinel/chat-requirements/prepare':
                assert method == 'POST' and data == {'p': 'observed-p'}
                assert not any(key.startswith('openai-sentinel-') for key in headers)
                assert headers['accept'] == 'application/json'
                payload = json.dumps({'persona': 'fixture', 'prepare_token': 'fresh-token',
                    'turnstile': {'required': True, 'dx': 'fixture'},
                    'proofofwork': {'required': True, 'seed': 'fixture', 'difficulty': 'fixture'},
                    'so': {'required': True, 'collector_dx': 'fixture',
                           'snapshot_dx': 'fixture'}}).encode()
                if self.oversize_sentinel:
                    payload = b'{"padding":"' + b'a' * 1_100_000 + b'"}'
            elif path == '/backend-api/f/conversation/prepare':
                assert method == 'POST' and data['client_prepare_state'] == 'sent'
                assert data['partial_query']['extra'] == 'keep'
                assert data['partial_query']['content']['extra'] == 'keep'
                assert data['partial_query']['id'] != 'old-input'
                assert data['partial_query']['content']['parts'] != ['old prompt']
                assert headers['accept'] == 'application/json'
                assert not any(key.startswith('openai-sentinel-') for key in headers)
                payload = json.dumps({'status': 'ok', 'conduit_token': 'fixture'}).encode()
            elif path == '/backend-api/conversation/' + CHAT:
                assert method == 'GET'
                assert not any(key.startswith('openai-sentinel-') for key in headers)
                payload = json.dumps({'current_node': 'wrong-node' if self.stale else
                                      self.current_node, 'mapping': {}}).encode()
            elif path == '/backend-api/f/conversation':
                assert method == 'POST'
                assert headers['accept'] == 'text/event-stream'
                assert headers['openai-sentinel-proof-token'] == PROOF
                assert headers['openai-sentinel-turnstile-token'] == PROOF
                assert ('openai-sentinel-chat-requirements-prepare-token' in headers
                        ) is self.prepare_token_present
                if self.prepare_token_present:
                    assert headers['openai-sentinel-chat-requirements-prepare-token'] == 'fixture'
                if self.lost_generation:
                    return
                if self.lost_after_candidate:
                    chunk = f'data: {{"conversation_id":"{CHAT}"}}\n\n'.encode()
                    writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n'
                                 + f'Content-Length: {len(chunk) + 100}\r\n'
                                   'Connection: close\r\n\r\n'.encode() + chunk)
                    await writer.drain()
                    return
                incoming = data['messages'][0]
                assert incoming['id'] != 'old-input'
                assert data['model'] == 'future-chat'
                assert data['thinking_effort'] == 'future-effort'
                assert incoming['metadata'] == {'is_visually_hidden_from_conversation': False}
                assert incoming['recipient'] == 'all' and incoming['weight'] == 1.0
                assert incoming['create_time'] > 1.0 and incoming['update_time'] == 1.0
                binding = {'request_id': 'request-' + incoming['id'],
                           'turn_exchange_id': 'exchange-' + incoming['id'],
                           'working_turn_id': 'work-' + incoming['id']}
                self.messages.append({'id': incoming['id'], 'author': {'role': 'user'},
                    'content': incoming['content'], 'metadata': binding,
                    'status': 'finished_successfully'})
                answer_id = 'answer-' + incoming['id']
                self.messages.append({'id': answer_id, 'author': {'role': 'assistant'},
                    'content': {'content_type': 'text', 'parts': ['answer: ' +
                        incoming['content']['parts'][0]]},
                    'metadata': {**binding, 'is_complete': True,
                                 'finish_details': {'type': 'stop'}},
                    'status': 'finished_successfully', 'channel': 'final', 'end_turn': True})
                self.current_node = answer_id
                payload = ('data: non-json-progress\n\n'
                           f'data: {{"conversation_id":"{CHAT}"}}\n\n'
                           'data: [DONE]\n\n').encode()
                content_type = 'text/event-stream'
                if self.compress_generation:
                    payload = gzip.compress(payload)
            elif path == '/backend-api/conversations/' + CHAT:
                assert method == 'GET'
                payload = json.dumps({'conversation_id': CHAT,
                                      'messages': self.messages}).encode()
            else:
                status, payload = '404 Not Found', b'{}'
            if path == self.fail_stage:
                status, payload = '403 Forbidden', b'{}'
            set_cookie = ('Set-Cookie: session=rotated; Path=/\r\n'
                          if self.rotate_cookie and path == (
                              '/backend-api/sentinel/chat-requirements/prepare') else '')
            content_encoding = ('Content-Encoding: gzip\r\n'
                                if self.compress_generation and path == (
                                    '/backend-api/f/conversation') else '')
            writer.write(f'HTTP/1.1 {status}\r\nContent-Type: {content_type}\r\n'
                         f'Content-Length: {len(payload)}\r\n{set_cookie}{content_encoding}'
                         'Connection: close\r\n\r\n'.encode()
                         + payload)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()


class LocalRequests:
    def __init__(self, client, origin):
        self.client = client
        self.origin = origin

    async def get(self, url, **kwargs):
        if url.startswith('https://chatgpt.com/'):
            url = self.origin + url.removeprefix('https://chatgpt.com')
        assert url.startswith(self.origin + '/')
        return await self.client.get(url, **kwargs)

    async def post(self, url, **kwargs):
        if url.startswith('https://chatgpt.com/'):
            url = self.origin + url.removeprefix('https://chatgpt.com')
        assert url.startswith(self.origin + '/')
        return await self.client.post(url, **kwargs)

    @asynccontextmanager
    async def stream(self, method, url, **kwargs):
        if url.startswith('https://chatgpt.com/'):
            url = self.origin + url.removeprefix('https://chatgpt.com')
        assert url.startswith(self.origin + '/')
        async with self.client.stream(method, url, **kwargs) as response:
            yield response


async def setup(tmp_path, api, client, *, generation=None, chrome_login=False):
    ledger = Ledger(tmp_path)
    store = SubchatSubmissions(ledger.connection)
    proxy = LocalRequests(client, api.origin)

    async def factory():
        return proxy

    backend = HTTPOnlySubchatBackend(factory, credentials(),
                                     generation=handoff() if generation is None else generation,
                                     store=store, generation_origin=api.origin,
                                     chrome_login=chrome_login)
    return ledger, store, Subchats(store, backend)


async def test_invalid_model_selection_reports_field_without_generation(tmp_path):
    from io import StringIO

    from anywhere_computer.subchat_cli import process_lines

    async with LocalChat() as api:
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
            ledger, store, service = await setup(tmp_path, api, client)
            try:
                invalid = SELECTION.model_copy(update={'model_slug': 'does-not-exist'})
                source = StringIO(json.dumps({'action': 'send', 'operation_id': 'e' * 32,
                    'prompt': 'test', 'model': 'Future Chat', 'effort': 'Future effort',
                    'http_selection': invalid.model_dump()}) + '\n')
                destination = StringIO()
                await process_lines(service, source, destination)
                result = json.loads(destination.getvalue())
                assert result['state'] == 'invalid_parameter'
                assert result['field'] == 'model_slug'
                assert result['reason'] == 'mismatch'
                assert result['dispatched'] is False
                assert result['corrected_request_requires_new_operation_id'] is True
                assert store.get('e' * 32, owner=None).state == 'prepared'
                assert not any(path == '/backend-api/f/conversation'
                               for _, path, _, _ in api.requests)
                mcp = mcp_session(service, observe_catalog=service.backend.catalog,
                                  observe_http_catalog=service.backend.http_catalog)
                reply = await mcp.execute(Request(operation_id='f' * 32,
                    tool='subchat_send', arguments={'prompt': 'test', 'model': 'Future Chat',
                        'effort': 'Future effort', 'http_selection': invalid.model_dump()}))
                assert reply.state == 'failed'
                assert reply.data['error_code'] == 'invalid_parameter'
                assert reply.data['field'] == 'model_slug'
                assert reply.data['dispatched'] is False
                assert not any(path == '/backend-api/f/conversation'
                               for _, path, _, _ in api.requests)
            finally:
                ledger.close()


@pytest.mark.parametrize(('model', 'reason'), [(None, 'required'), ('GPT-7', 'mismatch')])
async def test_null_or_unknown_model_never_reaches_generation(tmp_path, model, reason):
    from io import StringIO

    from anywhere_computer.subchat_cli import process_lines

    async with LocalChat() as api:
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
            ledger, store, service = await setup(tmp_path, api, client)
            try:
                source = StringIO(json.dumps({'action': 'send', 'operation_id': '1' * 32,
                    'prompt': 'test', 'model': model, 'effort': 'Future effort',
                    'http_selection': SELECTION.model_dump()}) + '\n')
                destination = StringIO()
                await process_lines(service, source, destination)
                result = json.loads(destination.getvalue())
                assert result['state'] == 'invalid_parameter'
                assert result['field'] == 'model'
                assert result['reason'] == reason
                assert result['dispatched'] is False
                assert not any(path == '/backend-api/f/conversation'
                               for _, path, _, _ in api.requests)
                if model is not None:
                    assert store.get('1' * 32, owner=None).state == 'prepared'
            finally:
                ledger.close()


@pytest.mark.parametrize(('field', 'bad_value'), [
    ('prompt', 8675309), ('model', 128), ('effort', {'bad': True}),
    ('conversation_id', 12345), ('work_context', 42), ('resources', -99),
    ('http_selection.version_id', 777), ('http_selection.preset_id', '7'),
    ('http_selection.thinking_effort', 128),
])
async def test_malformed_required_and_optional_fields_are_named_without_dispatch(
        tmp_path, field, bad_value):
    from io import StringIO

    from anywhere_computer.subchat_cli import process_lines

    async with LocalChat() as api:
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
            ledger, _, service = await setup(tmp_path, api, client)
            try:
                data = {'action': 'send', 'operation_id': '2' * 32,
                        'prompt': 'private-prompt-marker', 'model': 'Future Chat',
                        'effort': 'Future effort',
                        'http_selection': SELECTION.model_dump()}
                target = data
                parts = field.split('.')
                for part in parts[:-1]:
                    target = target[part]
                target[parts[-1]] = bad_value
                destination = StringIO()
                await process_lines(service, StringIO(json.dumps(data) + '\n'), destination)
                raw = destination.getvalue()
                result = json.loads(raw)
                assert result['state'] == 'invalid_parameter'
                assert result['dispatched'] is False
                assert any(parts[-1] in item['path'] for item in result['invalid_params'])
                assert 'private-prompt-marker' not in raw
                assert not any(path == '/backend-api/f/conversation'
                               for _, path, _, _ in api.requests)
            finally:
                ledger.close()


async def test_new_and_followup_use_http_only_and_history_final(tmp_path):
    async with LocalChat() as api:
        async with httpx.AsyncClient(
                trust_env=False, follow_redirects=False,
                transport=httpx.AsyncHTTPTransport(retries=0)) as client:
            try:
                ledger, store, service = await setup(tmp_path, api, client)
                try:
                    first = await service.send('a' * 32, 'new prompt', 'Future Chat',
                        'Future effort', owner=None, http_selection=SELECTION)
                    assert first.state == 'sending' and first.conversation_id == CHAT
                    assert first.user_message_id is not None
                    recovered = await service.recover(first.operation_id, owner=None)
                    assert recovered.state == 'completed'
                    parent = store.get(first.operation_id, owner=None)
                    queued = service.queue('b' * 32, first.operation_id, 'follow-up', owner=None)
                    assert queued.state == 'queued'
                    second = await service.recover(queued.operation_id, owner=None)
                    assert second.state == 'sending' and second.conversation_id == CHAT
                    final = await service.recover(queued.operation_id, owner=None)
                    assert final.state == 'completed' and final.answer == 'answer: follow-up'
                    posts = [(path, body) for method, path, _, body in api.requests
                             if method == 'POST']
                    assert [path for path, _ in posts] == [
                        '/backend-api/sentinel/chat-requirements/prepare',
                        '/backend-api/f/conversation/prepare', '/backend-api/f/conversation'] * 2
                    assert posts[1][1]['partial_query']['id'] == first.user_message_id
                    assert 'conversation_id' not in posts[2][1]
                    assert posts[4][1]['parent_message_id'] == parent.answer_message_id
                    assert posts[5][1]['parent_message_id'] == parent.answer_message_id
                    assert posts[5][1]['messages'][0]['id'] == final.user_message_id
                    assert sum(path.startswith('/backend-api/conversation/') for _, path, _, _
                               in api.requests) == 1
                    assert store.connection.execute(
                        'SELECT COUNT(*) FROM subchat_http_dispatch_claims').fetchone()[0] == 2
                    first_events = store.http_events(first.operation_id, owner=None)
                    assert [event['stage'] for event in first_events] == [
                        'sentinel_request', 'sentinel_response', 'prepare_request',
                        'prepare_response', 'dispatch_claimed', 'generation_request',
                        'generation_response', 'sse_candidate', 'history_receipt', 'history_final',
                    ]
                    second_events = store.http_events(queued.operation_id, owner=None)
                    assert [event['stage'] for event in second_events] == [
                        'sentinel_request', 'sentinel_response', 'prepare_request',
                        'prepare_response', 'branch_request', 'branch_response',
                        'dispatch_claimed', 'generation_request', 'generation_response',
                        'sse_candidate', 'history_receipt', 'history_final',
                    ]
                    assert [event['status'] for event in first_events
                            if event['stage'].endswith('_response')] == [200, 200, 200]
                    assert all(set(event) == {'operation_id', 'stage', 'status', 'timestamp'}
                               for event in first_events + second_events)
                    assert PROOF.encode() not in (tmp_path / 'operations.sqlite3').read_bytes()
                finally:
                    ledger.close()
            finally:
                await client.aclose()


async def test_generation_preserves_absent_observed_prepare_token(tmp_path):
    data = handoff_data()
    data['headers'].pop('openai-sentinel-chat-requirements-prepare-token')
    generation = ObservedHTTPGeneration.from_data(
        data, authorization=SECRET, account_id='fixture-account')
    async with LocalChat(prepare_token_present=False) as api:
        async with httpx.AsyncClient(
                trust_env=False, follow_redirects=False,
                transport=httpx.AsyncHTTPTransport(retries=0)) as client:
            ledger, _, service = await setup(tmp_path, api, client, generation=generation)
            try:
                sent = await service.send('9' * 32, 'no prepare header', 'Future Chat',
                    'Future effort', owner=None, http_selection=SELECTION)
                assert sent.state == 'sending' and sent.conversation_id == CHAT
                final = await service.recover(sent.operation_id, owner=None)
                assert final.state == 'completed'
                post_headers = [headers for method, _, headers, _ in api.requests
                                if method == 'POST']
                assert len(post_headers) == 3
                assert all('openai-sentinel-chat-requirements-prepare-token' not in headers
                           for headers in post_headers)
            finally:
                ledger.close()


async def test_generation_forwards_observed_requirements_token_verbatim(tmp_path):
    data = handoff_data()
    data['headers'].pop('openai-sentinel-chat-requirements-prepare-token')
    observed_token = 'observed-requirements-token'
    data['headers']['openai-sentinel-chat-requirements-token'] = observed_token
    generation = ObservedHTTPGeneration.from_data(
        data, authorization=SECRET, account_id='fixture-account')
    async with LocalChat(prepare_token_present=False) as api:
        async with httpx.AsyncClient(
                trust_env=False, follow_redirects=False,
                transport=httpx.AsyncHTTPTransport(retries=0)) as client:
            ledger, _, service = await setup(tmp_path, api, client, generation=generation)
            try:
                sent = await service.send('7' * 32, 'observed token', 'Future Chat',
                    'Future effort', owner=None, http_selection=SELECTION)
                assert sent.state == 'sending' and sent.conversation_id == CHAT
                post_headers = [headers for method, _, headers, _ in api.requests
                                if method == 'POST']
                assert len(post_headers) == 3
                assert all(not any(key.startswith('openai-sentinel-') for key in headers)
                           for headers in post_headers[:2])
                assert post_headers[2]['openai-sentinel-chat-requirements-token'] == (
                    observed_token)
                assert all('openai-sentinel-chat-requirements-prepare-token' not in headers
                           for headers in post_headers)
                assert observed_token.encode() not in (tmp_path / 'operations.sqlite3').read_bytes()
            finally:
                ledger.close()


async def test_chrome_generation_uses_rotating_client_cookie(tmp_path):
    async with LocalChat(rotate_cookie=True) as api:
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
            client.cookies.set('session', 'initial', domain='127.0.0.1', path='/')
            ledger, _, service = await setup(tmp_path, api, client, chrome_login=True)
            try:
                sent = await service.send('8' * 32, 'rotating cookie', 'Future Chat',
                    'Future effort', owner=None, http_selection=SELECTION)
                assert sent.state == 'sending'
                stages = {path: headers.get('cookie') for _, path, headers, _ in api.requests}
                assert stages['/backend-api/sentinel/chat-requirements/prepare'] == (
                    'session=initial')
                assert stages['/backend-api/f/conversation/prepare'] == 'session=rotated'
                assert stages['/backend-api/f/conversation'] == 'session=rotated'
            finally:
                ledger.close()


async def test_preparation_response_stops_at_raw_byte_limit():
    chunks_read = 0

    class Oversized(httpx.AsyncByteStream):
        async def __aiter__(self):
            nonlocal chunks_read
            for chunk in (b' ' * 600_000, b' ' * 600_000):
                chunks_read += 1
                yield chunk
            raise AssertionError('The response should be closed at the size limit')

    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _:
            httpx.Response(200, headers={'content-type': 'application/json'},
                           stream=Oversized()))) as client:
        async with client.stream('POST', 'https://chatgpt.com/backend-api/test') as response:
            with pytest.raises(ValueError, match='too large'):
                await _json_response(response)
    assert chunks_read == 2


async def test_oversized_preparation_never_reaches_generation(tmp_path, monkeypatch):
    post_called = False

    async def buffered_post(*_args, **_kwargs):
        nonlocal post_called
        post_called = True
        raise AssertionError('Preparation must use the bounded stream path')

    monkeypatch.setattr(LocalRequests, 'post', buffered_post)
    async with LocalChat(oversize_sentinel=True) as api:
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
            ledger, store, service = await setup(tmp_path, api, client)
            try:
                with pytest.raises(SubchatOutcomeUnknown):
                    await service.send('d' * 32, 'large preparation', 'Future Chat',
                        'Future effort', owner=None, http_selection=SELECTION)
                assert not post_called
                assert store.get('d' * 32, owner=None).state == 'sending'
                assert [event['stage'] for event in store.http_events(
                    'd' * 32, owner=None)][-2:] == ['sentinel_response', 'sentinel_failed']
                assert not any(path == '/backend-api/f/conversation' for _, path, _, _
                               in api.requests)
            finally:
                ledger.close()


@pytest.mark.parametrize(('template_name', 'failed_stage'), [
    ('prepare_template', 'prepare_failed'),
    ('generation_template', 'generation_failed'),
])
async def test_multibyte_outgoing_body_is_bounded_before_any_post(
        tmp_path, template_name, failed_stage):
    data = handoff_data()
    data[template_name]['padding'] = 'x' * 900_000
    generation = ObservedHTTPGeneration.from_data(
        data, authorization=SECRET, account_id='fixture-account')
    async with LocalChat() as api:
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
            ledger, store, service = await setup(tmp_path, api, client,
                                                  generation=generation)
            try:
                with pytest.raises(SubchatOutcomeUnknown):
                    await service.send('d' * 32, '界' * 100_000, 'Future Chat',
                                       'Future effort', owner=None,
                                       http_selection=SELECTION)
                assert store.get('d' * 32, owner=None).state == 'sending'
                assert [event['stage'] for event in store.http_events(
                    'd' * 32, owner=None)] == [failed_stage]
                assert all(method != 'POST' for method, _, _, _ in api.requests)
            finally:
                ledger.close()


@pytest.mark.parametrize('template_name', ['prepare_template', 'generation_template'])
def test_completed_outgoing_body_accepts_exact_byte_limit(template_name, monkeypatch):
    monkeypatch.setattr('anywhere_computer.subchat_http_generation.time.time', lambda: 1.0)
    operation_id, message_id = 'e' * 32, 'f' * 32
    submission = SubchatSubmission(
        operation_id=operation_id, prompt='界', model='Future Chat',
        effort='Future effort', state='sending', user_message_id=message_id,
        provider_account_id='fixture-account', http_selection=SELECTION)
    plan = HTTPGenerationPlan.from_reserved(submission)
    data = handoff_data()
    data[template_name]['padding'] = ''
    baseline = ObservedHTTPGeneration.from_data(
        data, authorization=SECRET, account_id='fixture-account')
    def body_for(handoff):
        return (handoff.prepare_body(plan) if template_name == 'prepare_template'
                else handoff.generation_body(plan, submission))
    padding = 1_048_576 - len(body_for(baseline))
    assert padding > 0
    data[template_name]['padding'] = 'x' * padding
    # Exercise the completed-body boundary directly; the handoff's separate
    # template limit counts its less compact input serialization.
    def with_templates() -> ObservedHTTPGeneration:
        return ObservedHTTPGeneration(
            json.dumps(data['headers']).encode(), data['sentinel_p'],
            json.dumps(data['prepare_template']).encode(),
            json.dumps(data['generation_template']).encode())

    handoff = with_templates()
    assert len(body_for(handoff)) == 1_048_576
    data[template_name]['padding'] += 'x'
    oversized = with_templates()
    with pytest.raises(ValueError, match='bounded request|too large'):
        body_for(oversized)


async def test_compressed_generation_is_not_decoded_or_replayed(tmp_path):
    async with LocalChat(compress_generation=True) as api:
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
            ledger, store, service = await setup(tmp_path, api, client)
            try:
                with pytest.raises(SubchatOutcomeUnknown):
                    await service.send('c' * 32, 'compressed reply', 'Future Chat',
                        'Future effort', owner=None, http_selection=SELECTION)
                assert store.get('c' * 32, owner=None).state == 'sending'
                assert [event['stage'] for event in store.http_events(
                    'c' * 32, owner=None)][-1] == 'generation_failed'
                assert sum(path == '/backend-api/f/conversation' for _, path, _, _
                           in api.requests) == 1
                generation_paths = {
                    '/backend-api/sentinel/chat-requirements/prepare',
                    '/backend-api/f/conversation/prepare',
                    '/backend-api/f/conversation',
                }
                assert all(headers.get('accept-encoding') == 'identity' for _, path,
                           headers, _ in api.requests if path in generation_paths)
            finally:
                ledger.close()


async def test_stale_branch_and_lost_post_do_not_replay(tmp_path):
    async with LocalChat() as api:
        async with httpx.AsyncClient(
                trust_env=False, follow_redirects=False,
                transport=httpx.AsyncHTTPTransport(retries=0)) as client:
            try:
                ledger, store, service = await setup(tmp_path, api, client)
                try:
                    first = await service.send('c' * 32, 'parent', 'Future Chat',
                        'Future effort', owner=None, http_selection=SELECTION)
                    await service.recover(first.operation_id, owner=None)
                    api.stale = True
                    queued = service.queue('d' * 32, first.operation_id, 'stale', owner=None)
                    with pytest.raises(SubchatOutcomeUnknown):
                        await service.recover(queued.operation_id, owner=None)
                    assert store.get(queued.operation_id, owner=None).state == 'sending'
                    assert sum(path == '/backend-api/f/conversation' for _, path, _, _
                               in api.requests) == 1
                    assert store.connection.execute('SELECT COUNT(*) FROM '
                        'subchat_http_dispatch_claims').fetchone()[0] == 1
                    assert [event['stage'] for event in store.http_events(
                        queued.operation_id, owner=None)][-2:] == [
                            'branch_response', 'branch_stale']
                    api.lost_generation = True
                    with pytest.raises(SubchatOutcomeUnknown):
                        await service.send('e' * 32, 'lost', 'Future Chat',
                            'Future effort', owner=None, http_selection=SELECTION)
                    count = len(api.requests)
                    repeated = await service.send('e' * 32, 'lost', 'Future Chat',
                        'Future effort', owner=None, http_selection=SELECTION)
                    assert repeated.state == 'sending' and len(api.requests) == count
                    assert store.connection.execute('SELECT COUNT(*) FROM '
                        'subchat_http_dispatch_claims').fetchone()[0] == 2
                finally:
                    ledger.close()
            finally:
                await client.aclose()


@pytest.mark.parametrize('stage', [
    '/backend-api/sentinel/chat-requirements/prepare',
    '/backend-api/f/conversation/prepare',
])
async def test_failed_preparation_keeps_unknown_without_claim_or_retry(tmp_path, stage):
    async with LocalChat() as api:
        api.fail_stage = stage
        async with httpx.AsyncClient(
                trust_env=False, follow_redirects=False,
                transport=httpx.AsyncHTTPTransport(retries=0)) as client:
            try:
                ledger, store, service = await setup(tmp_path, api, client)
                try:
                    with pytest.raises(SubchatOutcomeUnknown):
                        await service.send('f' * 32, 'unknown preparation', 'Future Chat',
                            'Future effort', owner=None, http_selection=SELECTION)
                    count = len(api.requests)
                    saved = await service.send('f' * 32, 'unknown preparation', 'Future Chat',
                        'Future effort', owner=None, http_selection=SELECTION)
                    assert saved.state == 'sending' and len(api.requests) == count
                    assert store.connection.execute('SELECT COUNT(*) FROM '
                        'subchat_http_dispatch_claims').fetchone()[0] == 0
                    events = store.http_events('f' * 32, owner=None)
                    failed_stage = 'sentinel' if 'sentinel' in stage else 'prepare'
                    assert events[-1]['stage'] == failed_stage + '_failed'
                    assert events[-2]['stage'] == failed_stage + '_response'
                    assert events[-2]['status'] == 403
                    assert not any(path == '/backend-api/f/conversation' for _, path, _, _
                                   in api.requests)
                finally:
                    ledger.close()
            finally:
                await client.aclose()


async def test_lost_stream_checkpoints_candidate_but_not_receipt(tmp_path):
    async with LocalChat() as api:
        api.lost_after_candidate = True
        async with httpx.AsyncClient(
                trust_env=False, follow_redirects=False,
                transport=httpx.AsyncHTTPTransport(retries=0)) as client:
            try:
                ledger, store, service = await setup(tmp_path, api, client)
                try:
                    with pytest.raises(SubchatOutcomeUnknown):
                        await service.send('1' * 32, 'candidate only', 'Future Chat',
                            'Future effort', owner=None, http_selection=SELECTION)
                    saved = store.get('1' * 32, owner=None)
                    assert saved.state == 'sending' and saved.conversation_id == CHAT
                    assert saved.answer is None
                    recovered = await service.recover('1' * 32, owner=None)
                    assert recovered.state == 'sending' and recovered.answer is None
                    assert [event['stage'] for event in store.http_events(
                        '1' * 32, owner=None)][-3:] == [
                            'sse_candidate', 'generation_failed', 'history_unknown']
                    assert sum(path == '/backend-api/f/conversation' for _, path, _, _
                               in api.requests) == 1
                finally:
                    ledger.close()
            finally:
                await client.aclose()


async def test_generation_403_is_claimed_recorded_and_never_reposted(tmp_path):
    async with LocalChat() as api:
        api.fail_stage = '/backend-api/f/conversation'
        async with httpx.AsyncClient(
                trust_env=False, follow_redirects=False,
                transport=httpx.AsyncHTTPTransport(retries=0)) as client:
            try:
                ledger, store, service = await setup(tmp_path, api, client)
                try:
                    with pytest.raises(SubchatOutcomeUnknown) as caught:
                        await service.send('2' * 32, 'rejected prompt', 'Future Chat',
                            'Future effort', owner=None, http_selection=SELECTION)
                    assert PROOF not in str(caught.value) and SECRET not in str(caught.value)
                    saved = store.get('2' * 32, owner=None)
                    assert saved.state == 'sending'
                    assert saved.generation_http_status == 403
                    events = store.http_events('2' * 32, owner=None)
                    assert [(event['stage'], event['status']) for event in events[-3:]] == [
                        ('generation_request', None), ('generation_response', 403),
                        ('generation_failed', None)]
                    assert store.connection.execute('SELECT COUNT(*) FROM '
                        'subchat_http_dispatch_claims').fetchone()[0] == 1
                    posts = [path for method, path, _, _ in api.requests if method == 'POST']
                    assert posts == [
                        '/backend-api/sentinel/chat-requirements/prepare',
                        '/backend-api/f/conversation/prepare',
                        '/backend-api/f/conversation',
                    ]
                    request_count = len(api.requests)
                    repeated = await service.send('2' * 32, 'rejected prompt', 'Future Chat',
                        'Future effort', owner=None, http_selection=SELECTION)
                    assert repeated.state == 'sending' and len(api.requests) == request_count
                    assert PROOF.encode() not in (tmp_path / 'operations.sqlite3').read_bytes()
                finally:
                    ledger.close()
            finally:
                await client.aclose()


def test_handoff_parser_redacts_secrets_and_requires_exact_session():
    data = handoff_data()
    raw = json.dumps(data).encode() + b'\n'
    parsed = read_http_generation_handoff(BytesIO(raw), authorization=SECRET,
                                          account_id='fixture-account')
    assert PROOF not in repr(parsed) and SECRET not in repr(parsed)
    parsed.headers['chatgpt-account-id'] = 'mutated'
    parsed.generation_template['model'] = 'mutated'
    assert parsed.headers['chatgpt-account-id'] == 'fixture-account'
    assert parsed.generation_template['model'] == 'old-model'
    data['headers']['chatgpt-account-id'] = 'other-account'
    with pytest.raises(ValueError) as caught:
        read_http_generation_handoff(BytesIO(json.dumps(data).encode() + b'\n'),
                                     authorization=SECRET, account_id='fixture-account')
    assert PROOF not in str(caught.value) and SECRET not in str(caught.value)
    assert CATALOG_URL.startswith('https://chatgpt.com/')


def test_handoff_accepts_observed_token_variants_and_rejects_unknown_headers():
    data = handoff_data()
    data['headers'].pop('openai-sentinel-chat-requirements-prepare-token')
    parsed = ObservedHTTPGeneration.from_data(
        data, authorization=SECRET, account_id='fixture-account')
    assert 'openai-sentinel-chat-requirements-prepare-token' not in parsed.headers

    data['headers']['openai-sentinel-chat-requirements-token'] = 'observed-token'
    parsed = ObservedHTTPGeneration.from_data(
        data, authorization=SECRET, account_id='fixture-account')
    assert parsed.headers['openai-sentinel-chat-requirements-token'] == 'observed-token'

    data['headers']['unobserved-header'] = 'fixture'
    with pytest.raises(ValueError, match='Invalid HTTP generation headers'):
        ObservedHTTPGeneration.from_data(
            data, authorization=SECRET, account_id='fixture-account')
    data['headers'].pop('unobserved-header')

    data['headers'].pop('openai-sentinel-proof-token')
    with pytest.raises(ValueError, match='Invalid HTTP generation headers'):
        ObservedHTTPGeneration.from_data(
            data, authorization=SECRET, account_id='fixture-account')


def test_http_events_are_private_bounded_and_durable(tmp_path):
    operation = '3' * 32
    ledger = Ledger(tmp_path)
    try:
        store = SubchatSubmissions(ledger.connection)
        store.prepare(operation, 'private prompt', 'model', 'effort', owner='private-owner')
        for _ in range(70):
            store.record_http_event(operation, 'history_unknown', owner='private-owner')
        events = store.http_events(operation, owner='private-owner')
        assert len(events) == 64
        assert store.http_progress(operation, owner='private-owner') == {
            key: events[-1][key] for key in ('stage', 'status', 'timestamp')}
        with pytest.raises(ValueError):
            store.http_events(operation, owner=None)
        with pytest.raises(ValueError):
            store.record_http_event(operation, 'private prompt', owner='private-owner')
    finally:
        ledger.close()
    reopened = Ledger(tmp_path)
    try:
        assert SubchatSubmissions(reopened.connection).http_events(
            operation, owner='private-owner') == events
    finally:
        reopened.close()


async def test_http_progress_surfaces_preserve_pending_and_final_state(tmp_path):
    async with LocalChat() as api, httpx.AsyncClient(
            trust_env=False, follow_redirects=False,
            transport=httpx.AsyncHTTPTransport(retries=0)) as client:
        ledger, store, service = await setup(tmp_path, api, client)
        try:
            pending = await service.send('5' * 32, 'private prompt', 'Future Chat',
                'Future effort', owner=None, http_selection=SELECTION)
            assert pending.state == 'sending'
            cli = json.loads(await dispatch(service, Command(action='status',
                operation_id=pending.operation_id)))
            assert cli['state'] == 'sending' and cli['answer'] is None
            assert cli['http_progress']['stage'] == 'sse_candidate'
            server = mcp_session(service, serialize_recovery=False)
            try:
                status = await server.execute(Request(operation_id='6' * 32,
                    tool='subchat_status', arguments={'operation_id': pending.operation_id}))
                assert status.data['http_progress'] == cli['http_progress']
                final = await server.execute(Request(operation_id='8' * 32,
                    tool='subchat_recover', arguments={'operation_id': pending.operation_id}))
                assert final.data['state'] == 'completed'
                assert final.data['http_progress']['stage'] == 'history_final'
            finally:
                await server.close()
        finally:
            ledger.close()


async def test_cancelled_preparation_keeps_original_operation_unknown(tmp_path, monkeypatch):
    async with LocalChat() as api, httpx.AsyncClient(
            trust_env=False, follow_redirects=False,
            transport=httpx.AsyncHTTPTransport(retries=0)) as client:
        ledger, store, service = await setup(tmp_path, api, client)
        try:
            @asynccontextmanager
            async def cancelled_stream(self, method, url, **kwargs):
                raise asyncio.CancelledError
                yield

            monkeypatch.setattr(LocalRequests, 'stream', cancelled_stream)
            operation = '9' * 32
            with pytest.raises(asyncio.CancelledError):
                await service.send(operation, 'cancelled prompt', 'Future Chat',
                    'Future effort', owner=None, http_selection=SELECTION)
            assert store.get(operation, owner=None).state == 'sending'
            assert [event['stage'] for event in store.http_events(operation, owner=None)] == [
                'sentinel_request', 'sentinel_failed']
            assert store.connection.execute(
                'SELECT COUNT(*) FROM subchat_http_dispatch_claims').fetchone()[0] == 0
            assert (await service.send(operation, 'cancelled prompt', 'Future Chat',
                'Future effort', owner=None, http_selection=SELECTION)).state == 'sending'
            assert api.requests and all(path != '/backend-api/f/conversation'
                                        for _, path, _, _ in api.requests)
        finally:
            ledger.close()


@pytest.mark.parametrize('stage', ['branch', 'generation'])
async def test_cancelled_followup_or_generation_preserves_claim_boundary(
        tmp_path, monkeypatch, stage):
    async with LocalChat() as api, httpx.AsyncClient(
            trust_env=False, follow_redirects=False,
            transport=httpx.AsyncHTTPTransport(retries=0)) as client:
        ledger, store, service = await setup(tmp_path, api, client)
        try:
            parent = await service.send('6' * 32, 'parent', 'Future Chat',
                'Future effort', owner=None, http_selection=SELECTION)
            assert (await service.recover(parent.operation_id, owner=None)).state == 'completed'
            child = service.queue('7' * 32, parent.operation_id, 'child', owner=None)
            original_stream = LocalRequests.stream

            @asynccontextmanager
            async def cancelled_stream(self, method, url, **kwargs):
                target = ('/backend-api/conversation/' + CHAT if stage == 'branch'
                          else '/backend-api/f/conversation')
                if url.endswith(target):
                    raise asyncio.CancelledError
                async with original_stream(self, method, url, **kwargs) as response:
                    yield response

            monkeypatch.setattr(LocalRequests, 'stream', cancelled_stream)
            with pytest.raises(asyncio.CancelledError):
                await service.recover(child.operation_id, owner=None)
            assert store.get(child.operation_id, owner=None).state == 'sending'
            stages = [event['stage'] for event in store.http_events(
                child.operation_id, owner=None)]
            assert stages[-2:] == ([
                'branch_request', 'branch_failed'] if stage == 'branch' else [
                'generation_request', 'generation_failed'])
            assert store.connection.execute(
                'SELECT COUNT(*) FROM subchat_http_dispatch_claims').fetchone()[0] == (
                    1 if stage == 'branch' else 2)
            count = len(api.requests)
            repeated = await service.recover(child.operation_id, owner=None)
            assert repeated.state == 'sending'
            assert not any(path == '/backend-api/f/conversation' for _, path, _, _
                           in api.requests[count:])
        finally:
            ledger.close()


def test_cli_opt_in_handoff_reports_capability_without_browser_or_secret_output(tmp_path):
    lines = [session_payload(), handoff_data(), {'action': 'capabilities'}]
    result = subprocess.run(command(tmp_path / 'state', '--http-only',
        '--http-session-stdin', '--http-generation-stdin'), env=environment(),
        input=''.join(json.dumps(item) + '\n' for item in lines).encode(),
        capture_output=True, timeout=15, check=False)
    assert result.returncode == 0, result.stderr
    capability = json.loads(result.stdout)
    assert capability['generation_transport'] == 'explicit_handoff_http'
    assert capability['browser_required'] is False
    assert SECRET.encode() not in result.stdout + result.stderr
    assert PROOF.encode() not in result.stdout + result.stderr
