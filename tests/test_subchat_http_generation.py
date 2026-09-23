"""Real loopback HTTP for the opt-in browser-free generation handoff."""
import asyncio
import json
import subprocess
from contextlib import asynccontextmanager
from io import BytesIO

import httpx
import pytest
from test_subchat_http_catalog import catalog
from test_subchat_http_only import CATALOG_URL, SECRET, credentials, session_payload
from test_subchat_http_only_cli import command, environment

from anywhere_computer.state import Ledger
from anywhere_computer.subchat import SubchatOutcomeUnknown, Subchats
from anywhere_computer.subchat_http import HTTPOnlySubchatBackend
from anywhere_computer.subchat_http_generation import (
    ObservedHTTPGeneration,
    read_http_generation_handoff,
)
from anywhere_computer.subchat_state import SubchatHTTPSelection, SubchatSubmissions

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
    def __init__(self):
        self.requests = []
        self.messages = []
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
                assert headers['openai-sentinel-proof-token'] == PROOF
                assert headers['accept'] == 'application/json'
                payload = json.dumps({'persona': 'fixture', 'prepare_token': 'fresh-token',
                    'turnstile': {'required': True, 'dx': 'fixture'},
                    'proofofwork': {'required': True, 'seed': 'fixture', 'difficulty': 'fixture'},
                    'so': {'required': True, 'collector_dx': 'fixture',
                           'snapshot_dx': 'fixture'}}).encode()
            elif path == '/backend-api/f/conversation/prepare':
                assert method == 'POST' and data['client_prepare_state'] == 'sent'
                assert data['partial_query']['extra'] == 'keep'
                assert data['partial_query']['content']['extra'] == 'keep'
                assert data['partial_query']['id'] != 'old-input'
                assert data['partial_query']['content']['parts'] != ['old prompt']
                assert headers['accept'] == 'application/json'
                assert headers['openai-sentinel-chat-requirements-prepare-token'] == 'fixture'
                payload = json.dumps({'status': 'ok', 'conduit_token': 'fixture'}).encode()
            elif path == '/backend-api/conversation/' + CHAT:
                assert method == 'GET'
                payload = json.dumps({'current_node': 'wrong-node' if self.stale else
                                      self.current_node, 'mapping': {}}).encode()
            elif path == '/backend-api/f/conversation':
                assert method == 'POST'
                assert headers['accept'] == 'text/event-stream'
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
            elif path == '/backend-api/conversations/' + CHAT:
                assert method == 'GET'
                payload = json.dumps({'conversation_id': CHAT,
                                      'messages': self.messages}).encode()
            else:
                status, payload = '404 Not Found', b'{}'
            if path == self.fail_stage:
                status, payload = '403 Forbidden', b'{}'
            writer.write(f'HTTP/1.1 {status}\r\nContent-Type: {content_type}\r\n'
                         f'Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n'.encode()
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


async def setup(tmp_path, api, client):
    ledger = Ledger(tmp_path)
    store = SubchatSubmissions(ledger.connection)
    proxy = LocalRequests(client, api.origin)

    async def factory():
        return proxy

    backend = HTTPOnlySubchatBackend(factory, credentials(), generation=handoff(),
                                     store=store, generation_origin=api.origin)
    return ledger, store, Subchats(store, backend)


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
                    assert PROOF.encode() not in (tmp_path / 'operations.sqlite3').read_bytes()
                finally:
                    ledger.close()
            finally:
                await client.aclose()


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
