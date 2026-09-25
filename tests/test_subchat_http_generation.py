"""Real loopback HTTP for the opt-in browser-free generation handoff."""
import asyncio
import gzip
import json
import subprocess
from contextlib import asynccontextmanager
from io import BytesIO

import httpx
import pytest
from pydantic import SecretStr
from test_subchat_http_catalog import catalog
from test_subchat_http_only import CATALOG_URL, SECRET, credentials, session_payload
from test_subchat_http_only_cli import command, environment

from anywhere_computer.models import Request
from anywhere_computer.state import Ledger
from anywhere_computer.subchat import SubchatOutcomeUnknown, SubchatPreflightFailed, Subchats
from anywhere_computer.subchat_browser.catalog import project_http_catalog
from anywhere_computer.subchat_cli import Command, dispatch
from anywhere_computer.subchat_http import HTTPOnlySubchatBackend
from anywhere_computer.subchat_http_generation import (
    ObservedHTTPGeneration,
    _json_response,
    dispatch_generation,
    read_http_generation_handoff,
)
from anywhere_computer.subchat_http_sender import HTTPGenerationPlan
from anywhere_computer.subchat_mcp import session as mcp_session
from anywhere_computer.subchat_state import (
    SubchatAccountMismatch,
    SubchatHTTPSelection,
    SubchatSelectionError,
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


@pytest.mark.parametrize('account,token,status', [
    ('fixture-account', SECRET.removeprefix('Bearer '), 200),
    ('other-account', SECRET.removeprefix('Bearer '), 200),
    ('fixture-account', 'another-token', 200),
    ('fixture-account', SECRET.removeprefix('Bearer '), 403),
])
async def test_explicit_generation_verifies_cookie_account_before_send(
        account, token, status):
    from anywhere_computer.subchat_http_session import ObservedHTTPSession

    requests = []

    def respond(request):
        requests.append(request)
        assert request.method == 'GET'
        assert str(request.url) == 'https://chatgpt.com/api/auth/session'
        assert request.headers['cookie'] == 'session=' + PROOF
        assert request.headers['user-agent'] == handoff().headers['user-agent']
        assert request.headers['referer'] == 'https://chatgpt.com/'
        assert 'authorization' not in request.headers
        assert 'chatgpt-account-id' not in request.headers
        return httpx.Response(status, json={
            'account': {'id': account}, 'accessToken': token})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        async def factory():
            return client

        session = ObservedHTTPSession.model_validate({
            **session_payload(), 'cookie': 'session=' + PROOF})
        backend = HTTPOnlySubchatBackend(factory, session, generation=handoff(),
                                         store=object())
        if status == 200 and account == 'fixture-account' and token == (
                SECRET.removeprefix('Bearer ')):
            await backend._verify_explicit_generation_account()
        elif status == 403:
            with pytest.raises(Exception) as rejected:
                await backend._verify_explicit_generation_account()
            assert getattr(rejected.value, 'status', None) == 403
        else:
            with pytest.raises(SubchatAccountMismatch):
                await backend._verify_explicit_generation_account()
    assert len(requests) == 1


async def test_explicit_account_mismatch_stops_before_reservation_or_post(tmp_path):
    from anywhere_computer.subchat_http_session import ObservedHTTPSession

    requests = []

    def respond(request):
        requests.append(request)
        assert request.method == 'GET'
        if str(request.url) == CATALOG_URL:
            return httpx.Response(200, json=catalog())
        assert str(request.url) == 'https://chatgpt.com/api/auth/session'
        return httpx.Response(200, json={
            'account': {'id': 'different-account'},
            'accessToken': SECRET.removeprefix('Bearer ')})

    ledger = Ledger(tmp_path)
    try:
        store = SubchatSubmissions(ledger.connection)
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            async def factory():
                return client

            session = ObservedHTTPSession.model_validate({
                **session_payload(), 'cookie': 'session=' + PROOF})
            backend = HTTPOnlySubchatBackend(factory, session, generation=handoff(),
                                             store=store)
            service = Subchats(store, backend)
            with pytest.raises(SubchatAccountMismatch):
                await service.send('a' * 32, 'test', 'Future Chat', 'Future effort',
                                   owner=None, http_selection=SELECTION)
        assert len(requests) == 2
        assert store.get('a' * 32, owner=None).state == 'prepared'
        assert store.connection.execute(
            'SELECT COUNT(*) FROM subchat_http_dispatch_claims').fetchone()[0] == 0
    finally:
        ledger.close()


@pytest.mark.parametrize('field,value,failed_stage', [
    ('conduit_token', 'bad\r\nvalue', 'prepare_failed'),
    ('sentinel_token', None, 'sentinel_failed'),
    ('sentinel_token', '日本語', 'sentinel_failed'),
])
async def test_invalid_fresh_token_stops_before_dispatch(tmp_path, field, value,
                                                        failed_stage):
    async with LocalChat(**{field: value}) as api:
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
            ledger, store, service = await setup(tmp_path, api, client)
            try:
                with pytest.raises(SubchatPreflightFailed):
                    await service.send('f' * 32, 'token check', 'Future Chat',
                                       'Future effort', owner=None, http_selection=SELECTION)
                assert store.get('f' * 32, owner=None).state == 'preflight_failed'
                assert store.http_progress('f' * 32, owner=None)['stage'] == failed_stage
                assert not any(path == '/backend-api/f/conversation' for _, path, _, _
                               in api.requests)
                assert store.connection.execute('SELECT COUNT(*) FROM '
                    'subchat_http_dispatch_claims').fetchone()[0] == 0
            finally:
                ledger.close()


class LocalChat:
    def __init__(self, *, rotate_cookie=False,
                 rotate_stage_cookies=False,
                 compress_generation=False, oversize_sentinel=False,
                 challenged_generation=False, conduit_token='fresh-conduit',
                 sentinel_token='fresh-token', prepare_state='sent',
                 catalog_payload=None):
        self.requests = []
        self.messages = []
        self.rotate_cookie = rotate_cookie
        self.rotate_stage_cookies = rotate_stage_cookies
        self.compress_generation = compress_generation
        self.oversize_sentinel = oversize_sentinel
        self.challenged_generation = challenged_generation
        self.conduit_token = conduit_token
        self.sentinel_token = sentinel_token
        self.prepare_state = prepare_state
        self.catalog_payload = catalog_payload
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
                payload = json.dumps(self.catalog_payload or catalog()).encode()
            elif path == '/backend-api/sentinel/chat-requirements/prepare':
                assert method == 'POST' and data == {'p': 'observed-p'}
                if 'x-openai-target-path' in headers:
                    assert headers['x-openai-target-path'] == path
                    assert headers['x-openai-target-route'] == path
                assert not any(key.startswith('openai-sentinel-') for key in headers)
                assert headers['accept'] == 'application/json'
                payload = json.dumps({'persona': 'fixture',
                    'prepare_token': self.sentinel_token,
                    'turnstile': {'required': True, 'dx': 'fixture'},
                    'proofofwork': {'required': True, 'seed': 'fixture', 'difficulty': 'fixture'},
                    'so': {'required': True, 'collector_dx': 'fixture',
                           'snapshot_dx': 'fixture'}}).encode()
                if self.oversize_sentinel:
                    payload = b'{"padding":"' + b'a' * 1_100_000 + b'"}'
            elif path == '/backend-api/f/conversation/prepare':
                assert method == 'POST' and data['client_prepare_state'] == self.prepare_state
                if 'x-openai-target-path' in headers:
                    assert headers['x-openai-target-path'] == path
                    assert headers['x-openai-target-route'] == path
                assert data['partial_query']['extra'] == 'keep'
                assert data['partial_query']['content']['extra'] == 'keep'
                assert data['partial_query']['id'] != 'old-input'
                assert data['partial_query']['content']['parts'] != ['old prompt']
                assert headers['accept'] == 'application/json'
                assert not any(key.startswith('openai-sentinel-') for key in headers)
                assert 'x-conduit-token' not in headers
                payload = json.dumps({'status': 'ok',
                    'conduit_token': self.conduit_token}).encode()
            elif path == '/backend-api/conversation/' + CHAT:
                assert method == 'GET'
                assert not any(key.startswith('openai-sentinel-') for key in headers)
                payload = json.dumps({'current_node': 'wrong-node' if self.stale else
                                      self.current_node, 'mapping': {}}).encode()
            elif path == '/backend-api/f/conversation':
                assert method == 'POST'
                if 'x-openai-target-path' in headers:
                    assert headers['x-openai-target-path'] == path
                    assert headers['x-openai-target-route'] == path
                assert headers['accept'] == 'text/event-stream'
                assert headers['openai-sentinel-proof-token'] == PROOF
                assert headers['openai-sentinel-turnstile-token'] == PROOF
                assert headers['openai-sentinel-chat-requirements-prepare-token'] == 'fresh-token'
                if self.conduit_token is None:
                    assert 'x-conduit-token' not in headers
                else:
                    assert headers['x-conduit-token'] == self.conduit_token
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
                if self.challenged_generation:
                    content_type, payload = 'text/html', b'<secret-response-body>'
            set_cookie = ('Set-Cookie: session=rotated; Path=/\r\n'
                          if self.rotate_cookie and path == (
                              '/backend-api/sentinel/chat-requirements/prepare') else '')
            if self.rotate_stage_cookies:
                stage = {
                    '/backend-api/f/conversation/prepare': 'prepared',
                    '/backend-api/sentinel/chat-requirements/prepare': 'sentinel',
                    '/backend-api/f/conversation': 'generated',
                }.get(path)
                if stage is not None:
                    set_cookie += ''.join(
                        f'Set-Cookie: {name}={stage}; Path=/\r\n'
                        for name in ('oai-sc', '_uasid', '_umsid'))
            content_encoding = ('Content-Encoding: gzip\r\n'
                                if self.compress_generation and path == (
                                    '/backend-api/f/conversation') else '')
            mitigation = ('Cf-Mitigated: challenge\r\n' if self.challenged_generation
                          and path == self.fail_stage else '')
            writer.write(f'HTTP/1.1 {status}\r\nContent-Type: {content_type}\r\n'
                         f'Content-Length: {len(payload)}\r\n{set_cookie}{content_encoding}'
                         f'{mitigation}'
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
        self.cookies = client.cookies
        self.event_hooks = client.event_hooks

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


async def setup(tmp_path, api, client, *, generation=None, chrome_login=False,
                session=None):
    ledger = Ledger(tmp_path)
    store = SubchatSubmissions(ledger.connection)
    proxy = LocalRequests(client, api.origin)

    async def factory():
        return proxy

    backend = HTTPOnlySubchatBackend(factory, credentials() if session is None else session,
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


async def test_http_only_rejects_version_label_before_reservation(tmp_path):
    payload = catalog()
    payload['versions'][0]['id'] = 'latest'
    payload['versions'][0]['display_text'] = '5.6'
    payload['versions'][0]['intelligence_presets'][0]['title'] = 'Instant'
    payload['models'][0]['title'] = 'GPT-5.6 Sol'
    payload['versions'].append({**payload['versions'][0], 'id': '5.6',
                                'display_text': 'GPT-5.6 Sol'})
    async with LocalChat(catalog_payload=payload) as api:
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
            ledger, store, service = await setup(tmp_path, api, client)
            try:
                selected = SubchatHTTPSelection.model_validate(
                    project_http_catalog(json.dumps(payload).encode())['versions'][0]
                    ['choices'][0]['http_selection'])
                with pytest.raises(SubchatSelectionError) as caught:
                    await service.send('b' * 32, 'test', '5.6', 'Instant',
                                       owner=None, http_selection=selected)
                assert (caught.value.field, caught.value.reason) == ('model', 'mismatch')
                assert store.get('b' * 32, owner=None).state == 'prepared'
                assert store.connection.execute(
                    'SELECT COUNT(*) FROM subchat_http_dispatch_claims').fetchone()[0] == 0
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
                        '/backend-api/f/conversation/prepare',
                        '/backend-api/sentinel/chat-requirements/prepare',
                        '/backend-api/f/conversation'] * 2
                    assert posts[0][1]['partial_query']['id'] == first.user_message_id
                    assert 'conversation_id' not in posts[2][1]
                    assert posts[3][1]['parent_message_id'] == parent.answer_message_id
                    assert posts[5][1]['parent_message_id'] == parent.answer_message_id
                    assert posts[5][1]['messages'][0]['id'] == final.user_message_id
                    assert sum(path.startswith('/backend-api/conversation/') for _, path, _, _
                               in api.requests) == 1
                    assert store.connection.execute(
                        'SELECT COUNT(*) FROM subchat_http_dispatch_claims').fetchone()[0] == 2
                    first_events = store.http_events(first.operation_id, owner=None)
                    assert [event['stage'] for event in first_events] == [
                        'prepare_request', 'prepare_response', 'sentinel_request',
                        'sentinel_response', 'dispatch_claimed', 'generation_request',
                        'generation_response', 'sse_candidate', 'history_receipt', 'history_final',
                    ]
                    second_events = store.http_events(queued.operation_id, owner=None)
                    assert [event['stage'] for event in second_events] == [
                        'prepare_request', 'prepare_response', 'sentinel_request',
                        'sentinel_response', 'branch_request', 'branch_response',
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


async def test_observed_new_chat_without_conduit_uses_its_prepare_shape(tmp_path):
    data = handoff_data()
    data['prepare_template']['client_prepare_state'] = 'none'
    data['prepare_template'].pop('is_do_not_remember')
    data['prepare_template']['parent_message_id'] = 'client-created-root'
    data['generation_template'].pop('conversation_id')
    data['generation_template']['parent_message_id'] = 'client-created-root'
    data['headers']['x-conduit-token'] = 'stale-conduit'
    generation = ObservedHTTPGeneration.from_data(
        data, authorization=SECRET, account_id='fixture-account')
    async with LocalChat(conduit_token=None, prepare_state='none') as api:
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
            ledger, _, service = await setup(tmp_path, api, client, generation=generation)
            try:
                sent = await service.send('8' * 32, 'new shape', 'Future Chat',
                    'Future effort', owner=None, http_selection=SELECTION)
                assert sent.state == 'sending'
                final = await service.recover(sent.operation_id, owner=None)
                assert final.state == 'completed' and final.answer == 'answer: new shape'
                posts = [(path, headers, body) for method, path, headers, body in api.requests
                         if method == 'POST']
                assert [path for path, _, _ in posts] == [
                    '/backend-api/f/conversation/prepare',
                    '/backend-api/sentinel/chat-requirements/prepare',
                    '/backend-api/f/conversation']
                assert posts[0][2]['parent_message_id'] == 'client-created-root'
                assert posts[2][2]['parent_message_id'] == 'client-created-root'
                assert all('x-conduit-token' not in headers for _, headers, _ in posts)
            finally:
                ledger.close()


async def test_generation_adds_fresh_prepare_token_when_handoff_lacked_it(tmp_path):
    data = handoff_data()
    data['headers'].pop('openai-sentinel-chat-requirements-prepare-token')
    generation = ObservedHTTPGeneration.from_data(
        data, authorization=SECRET, account_id='fixture-account')
    async with LocalChat() as api:
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
                           for headers in post_headers[:2])
                assert post_headers[2]['openai-sentinel-chat-requirements-prepare-token'] == (
                    'fresh-token')
            finally:
                ledger.close()


async def test_generation_replaces_observed_requirements_token_with_fresh_prepare_token(tmp_path):
    data = handoff_data()
    data['headers'].pop('openai-sentinel-chat-requirements-prepare-token')
    observed_token = 'observed-requirements-token'
    data['headers']['openai-sentinel-chat-requirements-token'] = observed_token
    generation = ObservedHTTPGeneration.from_data(
        data, authorization=SECRET, account_id='fixture-account')
    async with LocalChat() as api:
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
                assert 'openai-sentinel-chat-requirements-token' not in post_headers[2]
                assert all('openai-sentinel-chat-requirements-prepare-token' not in headers
                           for headers in post_headers[:2])
                assert post_headers[2]['openai-sentinel-chat-requirements-prepare-token'] == (
                    'fresh-token')
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
                assert stages['/backend-api/f/conversation/prepare'] == 'session=initial'
                assert stages['/backend-api/sentinel/chat-requirements/prepare'] == (
                    'session=initial')
                assert stages['/backend-api/f/conversation'] == 'session=rotated'
            finally:
                ledger.close()


async def test_explicit_session_uses_rotating_cookie_jar_for_send_and_history(tmp_path):
    from anywhere_computer.subchat_http_session import ObservedHTTPSession

    names = ('oai-sc', '_uasid', '_umsid')
    initial_cookie = '; '.join(f'{name}=initial' for name in names)
    data = handoff_data()
    data['headers']['cookie'] = initial_cookie
    session = ObservedHTTPSession.model_validate({
        **session_payload(), 'cookie': initial_cookie})
    generation = ObservedHTTPGeneration.from_data(
        data, authorization=SECRET, account_id='fixture-account',
        cookie=initial_cookie)

    def cookie_values(headers):
        return dict(part.strip().split('=', 1) for part in headers['cookie'].split(';'))

    async with LocalChat(rotate_stage_cookies=True) as api:
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
            ledger, store, service = await setup(
                tmp_path, api, client, generation=generation, session=session)
            try:
                first = await service.send('8' * 32, 'rotating cookie', 'Future Chat',
                    'Future effort', owner=None, http_selection=SELECTION)
                assert first.state == 'sending'
                completed = await service.recover(first.operation_id, owner=None)
                assert completed.state == 'completed'
                posts = [(path, headers) for method, path, headers, _ in api.requests
                         if method == 'POST']
                assert [path for path, _ in posts] == [
                    '/backend-api/f/conversation/prepare',
                    '/backend-api/sentinel/chat-requirements/prepare',
                    '/backend-api/f/conversation']
                for (_, headers), expected in zip(
                        posts, ('initial', 'prepared', 'sentinel'), strict=True):
                    assert cookie_values(headers) == dict.fromkeys(names, expected)
                history = [headers for method, path, headers, _ in api.requests
                           if method == 'GET' and path == '/backend-api/conversations/' + CHAT]
                assert history and cookie_values(history[-1]) == dict.fromkeys(
                    names, 'generated')

                queued = service.queue('9' * 32, first.operation_id, 'next', owner=None)
                followup = await service.recover(queued.operation_id, owner=None)
                assert followup.state == 'sending'
                later_posts = [(path, headers) for method, path, headers, _ in api.requests
                               if method == 'POST']
                assert cookie_values(later_posts[3][1]) == dict.fromkeys(names, 'generated')
                assert all('initial' not in headers.get('cookie', '')
                           for _, headers in later_posts[1:])
                assert store.get(queued.operation_id, owner=None).state == 'sending'
            finally:
                ledger.close()


async def test_explicit_cookie_rotation_changes_domain_without_duplicate_or_origin_leak():
    from anywhere_computer.subchat_http_session import ObservedHTTPSession

    seen = []

    def respond(request):
        seen.append(request.headers.get('cookie'))
        if len(seen) == 1:
            header = 'oai-sc=domain-rotated; Domain=.chatgpt.com; Path=/; Secure'
        elif len(seen) == 2:
            header = 'oai-sc=host-rotated; Path=/; Secure'
        else:
            header = ''
        return httpx.Response(200, headers={'set-cookie': header})

    session = ObservedHTTPSession.model_validate({
        **session_payload(), 'cookie': 'oai-sc=initial'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        async def factory():
            return client

        backend = HTTPOnlySubchatBackend(factory, session)
        scoped = await backend._request_factory()
        for _ in range(3):
            await scoped.get('https://chatgpt.com/backend-api/test')
        assert seen == ['oai-sc=initial', 'oai-sc=domain-rotated', 'oai-sc=host-rotated']
        with pytest.raises(ValueError, match='exact Chat origin'):
            await scoped.get('https://example.invalid/backend-api/test')
        assert len(seen) == 3


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
                with pytest.raises(SubchatPreflightFailed):
                    await service.send('d' * 32, 'large preparation', 'Future Chat',
                        'Future effort', owner=None, http_selection=SELECTION)
                assert not post_called
                assert store.get('d' * 32, owner=None).state == 'preflight_failed'
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
                with pytest.raises(SubchatPreflightFailed):
                    await service.send('d' * 32, '界' * 100_000, 'Future Chat',
                                       'Future effort', owner=None,
                                       http_selection=SELECTION)
                assert store.get('d' * 32, owner=None).state == 'preflight_failed'
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
            json.dumps(data['headers']).encode(), 'fixture-account', data['sentinel_p'],
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
                    with pytest.raises(SubchatPreflightFailed):
                        await service.recover(queued.operation_id, owner=None)
                    assert store.get(queued.operation_id, owner=None).state == 'preflight_failed'
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
async def test_failed_preparation_is_terminal_without_claim_or_retry(tmp_path, stage):
    async with LocalChat() as api:
        api.fail_stage = stage
        async with httpx.AsyncClient(
                trust_env=False, follow_redirects=False,
                transport=httpx.AsyncHTTPTransport(retries=0)) as client:
            try:
                ledger, store, service = await setup(tmp_path, api, client)
                try:
                    with pytest.raises(SubchatPreflightFailed):
                        await service.send('f' * 32, 'unknown preparation', 'Future Chat',
                            'Future effort', owner=None, http_selection=SELECTION)
                    count = len(api.requests)
                    saved = await service.send('f' * 32, 'unknown preparation', 'Future Chat',
                        'Future effort', owner=None, http_selection=SELECTION)
                    assert saved.state == 'preflight_failed' and len(api.requests) == count
                    assert store.connection.execute('SELECT COUNT(*) FROM '
                        'subchat_http_dispatch_claims').fetchone()[0] == 0
                    events = store.http_events('f' * 32, owner=None)
                    failed_stage = 'sentinel' if 'sentinel' in stage else 'prepare'
                    assert [event['stage'] for event in events[-2:]] == [
                        failed_stage + '_response', failed_stage + '_failed']
                    assert events[-2]['status'] == 403
                    assert events[-1]['status'] == 403
                    assert not any(path == '/backend-api/f/conversation' for _, path, _, _
                                   in api.requests)
                finally:
                    ledger.close()
            finally:
                await client.aclose()


async def test_mcp_reports_http_preflight_failure_without_replay(tmp_path):
    async with LocalChat() as api:
        api.fail_stage = '/backend-api/f/conversation/prepare'
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
            ledger, store, service = await setup(tmp_path, api, client)
            server = mcp_session(service, serialize_recovery=False)
            try:
                operation = 'a' * 32
                result = await server.execute(Request(operation_id=operation,
                    tool='subchat_send', arguments={
                        'prompt': 'preflight',
                        'model': 'Future Chat', 'effort': 'Future effort',
                        'http_selection': SELECTION.model_dump(mode='json')}))
                assert result.state == 'failed'
                assert result.data == {'error_code': 'http_preflight_failed',
                                       'dispatched': False, 'automatic_retry': False}
                assert store.get(operation, owner=None).state == 'preflight_failed'
                count = len(api.requests)
                recovered = await server.execute(Request(operation_id='b' * 32,
                    tool='subchat_recover', arguments={'operation_id': operation}))
                assert recovered.data['state'] == 'preflight_failed'
                assert recovered.data['http_progress']['stage'] == 'prepare_failed'
                assert recovered.data['http_progress']['status'] == 403
                assert len(api.requests) == count
            finally:
                await server.close()
                ledger.close()


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


async def test_generation_403_is_claimed_recorded_and_never_reposted(tmp_path, caplog):
    async with LocalChat(challenged_generation=True) as api:
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
                    assert ('status=403 http=h1 cf_mitigated=True response_kind=html'
                            in caplog.text)
                    assert 'secret-response-body' not in caplog.text
                    assert PROOF not in caplog.text and SECRET not in caplog.text
                    events = store.http_events('2' * 32, owner=None)
                    assert [(event['stage'], event['status']) for event in events[-3:]] == [
                        ('generation_request', None), ('generation_response', 403),
                        ('generation_failed', None)]
                    assert store.connection.execute('SELECT COUNT(*) FROM '
                        'subchat_http_dispatch_claims').fetchone()[0] == 1
                    posts = [path for method, path, _, _ in api.requests if method == 'POST']
                    assert posts == [
                        '/backend-api/f/conversation/prepare',
                        '/backend-api/sentinel/chat-requirements/prepare',
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


CURRENT_ONLY_HEADERS = (
    'oai-client-build-number oai-client-version oai-device-id oai-genui-client-actions '
    'oai-session-id oai-telemetry x-conduit-token x-oai-is-client-observation '
    'x-oai-is-pending-updates x-openai-target-path x-openai-target-route').split()


def current_shape_data():
    data = handoff_data()
    names = (
        'accept accept-language authorization content-type cookie oai-echo-logs oai-language '
        'openai-sentinel-chat-requirements-token openai-sentinel-proof-token '
        'openai-sentinel-turnstile-token origin referer sec-ch-ua sec-ch-ua-mobile '
        'sec-ch-ua-platform sec-fetch-dest sec-fetch-mode sec-fetch-site user-agent '
        'x-oai-turn-trace-id x-openai-web-frontend x-openai-web-sse-compression'
    ).split()
    data['headers'] = dict.fromkeys([*names, *CURRENT_ONLY_HEADERS], 'fixture')
    data['headers'].update({
        'authorization': SECRET, 'accept': 'text/event-stream',
        'content-type': 'application/json', 'cookie': 'session=' + PROOF,
        'origin': 'https://chatgpt.com', 'referer': 'https://chatgpt.com/',
        'openai-sentinel-proof-token': PROOF,
        'openai-sentinel-turnstile-token': PROOF,
    })
    return data


def test_current_shape_without_account_header_is_bound_to_the_session_account():
    data = current_shape_data()
    parsed = ObservedHTTPGeneration.from_data(
        data, authorization=SECRET, account_id='fixture-account', cookie='session=' + PROOF)
    assert 'chatgpt-account-id' not in parsed.headers
    assert set(CURRENT_ONLY_HEADERS) <= set(parsed.headers)
    assert parsed.account_id == 'fixture-account'
    assert PROOF not in repr(parsed) and SECRET not in repr(parsed)


def test_old_shape_with_account_header_is_still_accepted():
    parsed = ObservedHTTPGeneration.from_data(
        handoff_data(), authorization=SECRET, account_id='fixture-account')
    assert parsed.headers['chatgpt-account-id'] == 'fixture-account'
    assert parsed.headers['oai-did'] == 'fixture'
    assert parsed.account_id == 'fixture-account'


@pytest.mark.parametrize('shape,field', [
    (handoff_data, 'account'), (handoff_data, 'authorization'), (handoff_data, 'cookie'),
    (current_shape_data, 'authorization'), (current_shape_data, 'cookie')])
def test_wrong_account_authorization_or_cookie_is_rejected(shape, field):
    data = shape()
    arguments = {'authorization': SECRET, 'account_id': 'fixture-account',
                 'cookie': 'session=' + PROOF}
    if field == 'account':
        arguments['account_id'] = 'other-account'
    elif field == 'authorization':
        arguments['authorization'] = 'Bearer other-authorization'
    else:
        arguments['cookie'] = 'session=other'
    with pytest.raises(ValueError, match='session or origin changed') as caught:
        ObservedHTTPGeneration.from_data(data, **arguments)
    assert PROOF not in str(caught.value) and SECRET not in str(caught.value)


def test_expected_cookie_requires_a_cookie_header_and_mismatched_header_is_rejected():
    data = current_shape_data()
    del data['headers']['cookie']
    with pytest.raises(ValueError, match='session or origin changed'):
        ObservedHTTPGeneration.from_data(
            data, authorization=SECRET, account_id='fixture-account', cookie='session=' + PROOF)
    with pytest.raises(ValueError, match='session or origin changed'):
        ObservedHTTPGeneration.from_data(
            data, authorization=SECRET, account_id='fixture-account')


def test_current_shape_requires_expected_cookie_without_account_header():
    with pytest.raises(ValueError, match='session or origin changed'):
        ObservedHTTPGeneration.from_data(
            current_shape_data(), authorization=SECRET, account_id='fixture-account')


@pytest.mark.parametrize('shape', [handoff_data, current_shape_data])
@pytest.mark.parametrize('required_protection', [
    'openai-sentinel-proof-token', 'openai-sentinel-turnstile-token'])
def test_protection_headers_and_strict_origin_shape_remain_required(shape,
                                                                   required_protection):
    cookie = 'session=' + PROOF
    data = shape()
    del data['headers'][required_protection]
    with pytest.raises(ValueError, match='Invalid HTTP generation headers'):
        ObservedHTTPGeneration.from_data(data, authorization=SECRET,
                                         account_id='fixture-account', cookie=cookie)
    data = shape()
    data['headers']['unobserved-header'] = 'fixture'
    with pytest.raises(ValueError, match='Invalid HTTP generation headers'):
        ObservedHTTPGeneration.from_data(data, authorization=SECRET,
                                         account_id='fixture-account', cookie=cookie)
    data = shape()
    data['headers']['origin'] = 'https://example.invalid'
    with pytest.raises(ValueError, match='session or origin changed'):
        ObservedHTTPGeneration.from_data(data, authorization=SECRET,
                                         account_id='fixture-account', cookie=cookie)


def test_backend_compares_stored_account_and_cookie_with_the_login_session():
    generation = ObservedHTTPGeneration.from_data(
        current_shape_data(), authorization=SECRET, account_id='fixture-account',
        cookie='session=' + PROOF)

    async def factory():
        raise AssertionError('No request is made at construction')

    HTTPOnlySubchatBackend(factory, credentials(), generation=generation,
                           store=object())
    other_account = credentials().model_copy(update={'account_id': 'other-account'})
    with pytest.raises(ValueError, match='does not match login session'):
        HTTPOnlySubchatBackend(factory, other_account, generation=generation, store=object())
    other_cookie = credentials().model_copy(update={'cookie': SecretStr('session=other')})
    with pytest.raises(ValueError, match='does not match login session'):
        HTTPOnlySubchatBackend(factory, other_cookie, generation=generation, store=object())


async def test_current_shape_generation_dispatches_with_stored_account(tmp_path):
    generation = ObservedHTTPGeneration.from_data(
        current_shape_data(), authorization=SECRET, account_id='fixture-account',
        cookie='session=' + PROOF)
    async with LocalChat() as api:
        async with httpx.AsyncClient(
                trust_env=False, follow_redirects=False,
                transport=httpx.AsyncHTTPTransport(retries=0)) as client:
            ledger, _, service = await setup(tmp_path, api, client, generation=generation)
            try:
                sent = await service.send('6' * 32, 'current shape', 'Future Chat',
                    'Future effort', owner=None, http_selection=SELECTION)
                assert sent.state == 'sending' and sent.conversation_id == CHAT
                post_headers = [headers for method, _, headers, _ in api.requests
                                if method == 'POST']
                assert len(post_headers) == 3
                assert all('chatgpt-account-id' not in headers and 'oai-did' not in headers
                           for headers in post_headers)
                assert post_headers[2]['x-openai-target-path'] == (
                    '/backend-api/f/conversation')
                for headers in post_headers[:2]:
                    assert headers['x-oai-is-client-observation'] == 'fixture'
                    assert headers['x-oai-is-pending-updates'] == 'fixture'
                assert post_headers[0]['x-openai-target-path'] == (
                    '/backend-api/f/conversation/prepare')
                assert post_headers[1]['x-openai-target-path'] == (
                    '/backend-api/sentinel/chat-requirements/prepare')
                assert 'x-conduit-token' not in post_headers[0]
                assert post_headers[0]['oai-genui-client-actions'] == 'fixture'
                assert 'x-conduit-token' not in post_headers[1]
                assert 'oai-genui-client-actions' not in post_headers[1]
                assert post_headers[0]['x-oai-turn-trace-id'] == (
                    post_headers[2]['x-oai-turn-trace-id'])
                assert post_headers[0]['x-oai-turn-trace-id'] != 'fixture'
                assert 'x-oai-turn-trace-id' not in post_headers[1]
            finally:
                ledger.close()


async def test_dispatch_rejects_a_plan_for_another_account_before_any_request(tmp_path):
    generation = ObservedHTTPGeneration.from_data(
        current_shape_data(), authorization=SECRET, account_id='fixture-account',
        cookie='session=' + PROOF)
    async with LocalChat() as api:
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
            ledger, store, _ = await setup(tmp_path, api, client, generation=generation)
            try:
                plan = HTTPGenerationPlan(
                    operation_id='5' * 32, user_message_id='user-input', prompt='prompt',
                    model_slug='future-chat', thinking_effort='future-effort',
                    account_id='other-account', conversation_id=None, predecessor_id=None)
                with pytest.raises(SubchatAccountMismatch):
                    await dispatch_generation(plan, None, handoff=generation, client=client,
                                              store=store, owner=None, origin=api.origin)
                assert api.requests == []
            finally:
                ledger.close()


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


async def test_cancelled_preparation_is_terminal_before_dispatch(tmp_path, monkeypatch):
    async with LocalChat() as api, httpx.AsyncClient(
            trust_env=False, follow_redirects=False,
            transport=httpx.AsyncHTTPTransport(retries=0)) as client:
        ledger, store, service = await setup(tmp_path, api, client)
        try:
            @asynccontextmanager
            async def cancelled_stream(self, method, url, **kwargs):
                if method == 'POST' and url.endswith(
                        '/backend-api/sentinel/chat-requirements/prepare'):
                    raise asyncio.CancelledError
                async with original_stream(self, method, url, **kwargs) as response:
                    yield response

            original_stream = LocalRequests.stream
            monkeypatch.setattr(LocalRequests, 'stream', cancelled_stream)
            operation = '9' * 32
            with pytest.raises(asyncio.CancelledError):
                await service.send(operation, 'cancelled prompt', 'Future Chat',
                    'Future effort', owner=None, http_selection=SELECTION)
            assert store.get(operation, owner=None).state == 'preflight_failed'
            assert [event['stage'] for event in store.http_events(operation, owner=None)] == [
                'prepare_request', 'prepare_response', 'sentinel_request', 'sentinel_failed']
            assert store.connection.execute(
                'SELECT COUNT(*) FROM subchat_http_dispatch_claims').fetchone()[0] == 0
            assert (await service.send(operation, 'cancelled prompt', 'Future Chat',
                'Future effort', owner=None, http_selection=SELECTION)).state == 'preflight_failed'
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
            assert store.get(child.operation_id, owner=None).state == (
                'preflight_failed' if stage == 'branch' else 'sending')
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
            assert repeated.state == ('preflight_failed' if stage == 'branch' else 'sending')
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


@pytest.mark.parametrize('cookie_mode', ['matching', 'missing', 'wrong'])
def test_cli_current_header_shape_requires_bound_explicit_cookie(tmp_path, cookie_mode):
    session = session_payload()
    if cookie_mode != 'missing':
        session['cookie'] = ('session=' + PROOF if cookie_mode == 'matching'
                             else 'session=another-account')
    lines = [session, current_shape_data(), {'action': 'capabilities'}]
    result = subprocess.run(command(tmp_path / 'state', '--http-only',
        '--http-session-stdin', '--http-generation-stdin'), env=environment(),
        input=''.join(json.dumps(item) + '\n' for item in lines).encode(),
        capture_output=True, timeout=15, check=False)
    if cookie_mode == 'matching':
        assert result.returncode == 0, result.stderr
        capability = json.loads(result.stdout)
        assert capability['generation_transport'] == 'explicit_handoff_http'
        assert capability['browser_required'] is False
    else:
        assert result.returncode == 2
        assert b'invalid_http_generation_handoff' in result.stderr
    assert SECRET.encode() not in result.stdout + result.stderr
    assert PROOF.encode() not in result.stdout + result.stderr
