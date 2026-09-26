"""CLI, stdio MCP and real HTTP-client tests; all credentials and HTTP data are synthetic."""
import asyncio
import json
import os
import subprocess
import sys
from contextlib import asynccontextmanager
from io import BytesIO, StringIO, TextIOWrapper
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_subchat_http_catalog import catalog
from test_subchat_http_history import sample
from test_subchat_http_only import (
    CATALOG_URL,
    SECRET,
    Client,
    credentials,
    seed,
    session_payload,
)

from anywhere_computer.state import Ledger
from anywhere_computer.subchat_state import SubchatSubmissions

ROOT = Path(__file__).resolve().parents[1]


def command(state, *args):
    program = '''
import builtins
original = builtins.__import__
def guard(name, *args, **kwargs):
    if (name == 'playwright' or name.startswith('playwright.')
            or name.endswith('subchat_browser.backend')):
        raise AssertionError('Saved-only HTTP controller imported a browser dependency')
    return original(name, *args, **kwargs)
builtins.__import__ = guard
from anywhere_computer.subchat_cli import main
main()
'''
    return [sys.executable, '-c', program, '--state-dir', str(state), *args]


def environment():
    return {**os.environ, 'PYTHONPATH': str(ROOT / 'src')}


@pytest.mark.parametrize('handoff', [False, True])
def test_cli_subprocess_capability_and_blocked_send_without_browser_import(tmp_path, handoff):
    args = ['--http-only'] + (['--http-session-stdin'] if handoff else [])
    operations = [{'action': 'capabilities'},
                  {'action': 'send', 'operation_id': 'b' * 32, 'prompt': 'fixture',
                   'model': 'dynamic', 'effort': 'dynamic'},
                  {'action': 'status', 'operation_id': 'b' * 32}]
    lines = ([session_payload()] if handoff else []) + operations
    result = subprocess.run(command(tmp_path / 'state', *args), env=environment(),
                            input=''.join(json.dumps(item) + '\n' for item in lines).encode(),
                            capture_output=True, timeout=15, check=False)
    assert result.returncode == 0, result.stderr
    replies = list(map(json.loads, result.stdout.splitlines()))
    assert len(replies) == 3
    assert replies[0]['generation_transport'] == 'unavailable'
    assert replies[1]['state'] == 'http_generation_unavailable'
    assert replies[1]['dispatched'] is False
    assert replies[2]['state'] == 'prepared'
    assert not result.stderr
    assert SECRET.encode() not in result.stdout + result.stderr


@pytest.mark.parametrize('args', [[], ['--http-session-stdin'],
    ['--http-only', '--browser-profile', '/unused-profile'],
    ['--http-only', '--http-read'], ['--http-only', '--minimized']])
def test_cli_incompatible_modes_fail_before_ledger_creation(tmp_path, args):
    state = tmp_path / 'not-created'
    result = subprocess.run(command(state, *args), env=environment(), input=b'',
                            capture_output=True, timeout=15, check=False)
    assert result.returncode == 2
    assert not state.exists()


def test_cli_invalid_session_redacted_before_any_operation(tmp_path):
    state = tmp_path / 'not-created'
    result = subprocess.run(command(state, '--http-only', '--http-session-stdin'),
                            env=environment(), input=SECRET.encode() + b'\n',
                            capture_output=True, timeout=15, check=False)
    assert result.returncode == 2
    assert not result.stdout and not state.exists()
    assert json.loads(result.stderr) == {'state': 'invalid_http_session', 'automatic_retry': False}
    assert SECRET.encode() not in result.stderr


@pytest.mark.parametrize('fail', [False, True])
@pytest.mark.parametrize('concurrent', [False, True])
async def test_http_only_cli_owns_only_request_client_and_disposes_it(
        tmp_path, monkeypatch, fail, concurrent):
    import builtins

    import httpx

    from anywhere_computer import subchat_cli

    ledger = Ledger(tmp_path)
    store = SubchatSubmissions(ledger.connection)
    submission, payload = seed(store, receipt=False)
    ledger.close()
    client = Client(payload)
    events = []

    class ManagedClient:
        async def __aenter__(self):
            events.append('client')
            return client

        async def __aexit__(self, *_):
            events.append('client_closed')

        async def get(self, url, **kwargs):
            return await client.get(url, **kwargs)

    def new_client(**kwargs):
        assert kwargs['trust_env'] is False
        assert kwargs['follow_redirects'] is False
        assert kwargs['transport']._pool._retries == 0
        return ManagedClient()

    monkeypatch.setattr(httpx, 'AsyncClient', new_client)
    original_import = builtins.__import__

    def forbid_playwright(name, *args, **kwargs):
        if name == 'playwright' or name.startswith('playwright.'):
            raise AssertionError('HTTP-only run imported Playwright')
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, '__import__', forbid_playwright)
    commands = [{'action': 'catalog'},
                {'action': 'recover', 'operation_id': submission.operation_id}]
    output = StringIO()
    monkeypatch.setattr(subchat_cli.sys, 'stdin',
                        StringIO(''.join(json.dumps(item) + '\n' for item in commands)))
    monkeypatch.setattr(subchat_cli.sys, 'stdout', output)
    if concurrent or fail:
        async def controlled(service, source, destination, *, owner=None):
            if concurrent:
                results = await asyncio.gather(
                    service.backend.http_catalog(),
                    service.recover(submission.operation_id, owner=None))
                assert results[1].state == 'completed'
            else:
                await service.backend.http_catalog()
            if fail:
                raise RuntimeError('fixture observer failure')
        monkeypatch.setattr(subchat_cli, 'process_lines', controlled)
    if fail:
        with pytest.raises(RuntimeError, match='fixture observer failure'):
            await subchat_cli.run(None, tmp_path, http_only=True, http_session=credentials())
    else:
        await subchat_cli.run(None, tmp_path, http_only=True, http_session=credentials())
        if not concurrent:
            replies = list(map(json.loads, output.getvalue().splitlines()))
            assert replies[0]['generation_transport'] == 'unavailable'
            assert replies[1]['state'] == 'completed'
            assert replies[1]['answer'] == '日本語 result'
    assert events == ['client', 'client_closed']
    assert SECRET not in output.getvalue()


async def test_http_only_real_stdio_mcp_without_browser_or_credentials(tmp_path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    ledger = Ledger(tmp_path / 'state')
    try:
        store = SubchatSubmissions(ledger.connection)
        submission, _ = seed(store)
        store.complete(submission.operation_id, 'answer', 'saved answer', owner=None)
    finally:
        ledger.close()
    argv = command(tmp_path / 'state', '--http-only', '--mcp')
    params = StdioServerParameters(command=argv[0], args=argv[1:], env=environment())
    async with asyncio.timeout(20):
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as client:
                initialized = await client.initialize()
                assert ('Ordinary Chat recovery and explicit deletion over HTTPX'
                        in initialized.instructions)
                tools = await client.list_tools()
                assert 'subchat_capabilities' in {tool.name for tool in tools.tools}
                caps = await client.call_tool('subchat_capabilities', {})
                assert not caps.isError
                assert caps.structuredContent['data']['generation_transport'] == 'unavailable'
                refused = await client.call_tool('subchat_send', {
                    'request_id': 'b' * 32, 'prompt': 'fixture',
                    'model': 'model', 'effort': 'effort'})
                assert refused.isError
                assert refused.structuredContent['data'] == {
                    'error_code': 'http_generation_unavailable', 'dispatched': False,
                    'automatic_retry': False}
                unavailable = await client.call_tool('subchat_catalog', {'source': 'http'})
                assert unavailable.isError
                assert unavailable.structuredContent['data']['error_code'] == (
                    'http_session_required')
                ui = await client.call_tool('subchat_catalog', {'source': 'ui'})
                assert ui.isError and ui.structuredContent['data']['error_code'] == 'ui_unavailable'
                saved = await client.call_tool('subchat_recover', {
                    'operation_id': submission.operation_id})
                assert not saved.isError
                assert saved.structuredContent['data']['answer'] == 'saved answer'


@pytest.mark.parametrize('status', [401, 403])
async def test_read_only_plugin_starts_when_chrome_login_is_rejected(
        tmp_path, monkeypatch, status):
    from playwright import async_api

    from anywhere_computer import mcp_server, subchat_chrome_login, subchat_cli, subchat_mcp
    from anywhere_computer.subchat import SubchatAccessError

    events = []

    class Context:
        async def close(self):
            events.append('chrome_closed')

    class PlaywrightManager:
        async def __aenter__(self):
            async def launch(*_args, **_kwargs):
                events.append('chrome_opened')
                return Context()

            return SimpleNamespace(chromium=SimpleNamespace(
                launch_persistent_context=launch))

        async def __aexit__(self, *_args):
            events.append('playwright_closed')

    async def rejected(*_args, **_kwargs):
        raise SubchatAccessError(status)

    def session(service, **_kwargs):
        assert service.backend.capabilities()['authentication_state'] == (
            'authentication_required' if status == 401 else 'access_denied')
        async def close():
            events.append('mcp_closed')

        return SimpleNamespace(service=service, close=close)

    async def serve(server, *_args):
        with pytest.raises(SubchatAccessError) as error:
            await server.service.backend.http_catalog()
        assert error.value.status == status
        events.append('mcp_served')

    monkeypatch.setattr(async_api, 'async_playwright', PlaywrightManager)
    monkeypatch.setattr(subchat_chrome_login, 'chrome_http_session', rejected)
    monkeypatch.setattr(subchat_mcp, 'session', session)
    monkeypatch.setattr(mcp_server, 'serve_stdio', serve)
    monkeypatch.setattr(subchat_cli.sys, 'stdin', TextIOWrapper(BytesIO()))
    monkeypatch.setattr(subchat_cli.sys, 'stdout', TextIOWrapper(BytesIO()))
    await subchat_cli.run(None, tmp_path / 'state', mcp=True, http_only=True,
                          chrome_login_profile=tmp_path / 'profile', read_only_mcp=True)
    assert events == [
        'chrome_opened', 'chrome_closed', 'mcp_served', 'mcp_closed', 'playwright_closed',
    ]


async def test_read_only_plugin_explicit_refresh_after_startup_401(tmp_path, monkeypatch):
    from playwright import async_api
    from test_subchat_http_only import credentials

    from anywhere_computer import mcp_server, subchat_chrome_login, subchat_cli, subchat_mcp
    from anywhere_computer.subchat import SubchatAccessError

    events = []

    class Context:
        async def close(self):
            events.append('chrome_closed')

    class PlaywrightManager:
        async def __aenter__(self):
            async def launch(*_args, **kwargs):
                windows_login = subchat_cli.sys.platform == 'win32'
                assert kwargs['headless'] is not windows_login
                assert kwargs['args'] == (
                    ['--window-position=-32000,-32000', '--window-size=900,700']
                    if windows_login else [])
                events.append('chrome_opened')
                return Context()

            return SimpleNamespace(chromium=SimpleNamespace(
                launch_persistent_context=launch))

        async def __aexit__(self, *_args):
            events.append('playwright_closed')

    async def login(_context, _client, *, expected_account_id):
        events.append(('account_pin', expected_account_id))
        if events.count('chrome_opened') == 1:
            raise SubchatAccessError(401)
        return credentials()

    def session(service, **_kwargs):
        async def close():
            events.append('mcp_closed')

        return SimpleNamespace(service=service, close=close)

    async def serve(server, *_args):
        backend = server.service.backend
        assert backend.capabilities()['authentication_state'] == 'authentication_required'
        result = await backend.refresh_auth()
        assert result['authentication_state'] == 'authenticated'
        assert backend.capabilities()['authentication_state'] == 'authenticated'
        events.append('mcp_served')

    monkeypatch.setattr(async_api, 'async_playwright', PlaywrightManager)
    monkeypatch.setattr(subchat_chrome_login, 'chrome_http_session', login)
    monkeypatch.setattr(subchat_mcp, 'session', session)
    monkeypatch.setattr(mcp_server, 'serve_stdio', serve)
    monkeypatch.setattr(subchat_cli.sys, 'stdin', TextIOWrapper(BytesIO()))
    monkeypatch.setattr(subchat_cli.sys, 'stdout', TextIOWrapper(BytesIO()))
    await subchat_cli.run(None, tmp_path / 'state', mcp=True, http_only=True,
                          chrome_login_profile=tmp_path / 'profile', read_only_mcp=True)
    assert events == [
        'chrome_opened', ('account_pin', None), 'chrome_closed',
        'chrome_opened', ('account_pin', None), 'chrome_closed',
        'mcp_served', 'mcp_closed', 'playwright_closed',
    ]


@pytest.mark.parametrize('failure', ['missing_snapshot', 'locked_profile'])
async def test_read_only_plugin_keeps_saved_tools_when_chrome_profile_unavailable(
        tmp_path, monkeypatch, failure):
    from playwright import async_api

    from anywhere_computer import mcp_server, subchat_chrome_profile, subchat_cli
    from anywhere_computer.models import Request

    class Driver:
        async def __aenter__(self):
            async def launch(*_args, **_kwargs):
                raise async_api.Error('profile is locked')

            return SimpleNamespace(chromium=SimpleNamespace(
                launch_persistent_context=launch))

        async def __aexit__(self, *_args):
            pass

    @asynccontextmanager
    async def missing_snapshot(_source):
        raise ValueError('Selected Chrome profile has no Local State or Cookie database')
        yield tmp_path  # pragma: no cover

    monkeypatch.setattr(async_api, 'async_playwright', Driver)
    if failure == 'missing_snapshot':
        monkeypatch.setattr(subchat_chrome_profile, 'temporary_chrome_profile',
                            missing_snapshot)

    async def serve(server, *_args):
        capabilities_reply = await server.execute(Request(
            operation_id='a' * 32, tool='subchat_capabilities', arguments={}))
        capabilities = capabilities_reply.data
        assert capabilities['authentication_state'] == 'authentication_required'
        assert capabilities['generation_transport'] == 'unavailable'
        assert 'subchat_list' in {tool['name'] for tool in await server.catalog()}
        catalog_reply = await server.execute(Request(
            operation_id='b' * 32, tool='subchat_catalog', arguments={}))
        assert catalog_reply.state == 'failed'
        assert catalog_reply.data['error_code'] == 'authentication_required'

    monkeypatch.setattr(mcp_server, 'serve_stdio', serve)
    await subchat_cli.run(None, tmp_path / 'state', mcp=True, http_only=True,
                          chrome_login_source_profile=(tmp_path / 'Chrome/Default'
                                                       if failure == 'missing_snapshot' else None),
                          chrome_login_profile=(tmp_path / 'dedicated'
                                                if failure == 'locked_profile' else None),
                          read_only_mcp=True)


async def test_read_only_plugin_reports_pinned_account_mismatch_without_identity(
        tmp_path, monkeypatch):
    from playwright import async_api

    from anywhere_computer import mcp_server, subchat_chrome_login, subchat_cli
    from anywhere_computer.models import Request
    from anywhere_computer.subchat_state import SubchatAccountMismatch

    class Context:
        async def close(self):
            pass

    class Driver:
        async def __aenter__(self):
            async def launch(*_args, **_kwargs):
                return Context()

            return SimpleNamespace(chromium=SimpleNamespace(
                launch_persistent_context=launch))

        async def __aexit__(self, *_args):
            pass

    observed_pins = []

    async def wrong_account(_context, _client, *, expected_account_id):
        observed_pins.append(expected_account_id)
        assert expected_account_id == 'pinned-account'
        raise SubchatAccountMismatch('private observed account')

    async def serve(server, *_args):
        capabilities = await server.execute(Request(
            operation_id='a' * 32, tool='subchat_capabilities', arguments={}))
        assert capabilities.data['authentication_state'] == 'account_mismatch'
        assert 'private observed account' not in capabilities.model_dump_json()
        catalog = await server.execute(Request(
            operation_id='b' * 32, tool='subchat_catalog', arguments={}))
        assert catalog.state == 'failed'
        assert catalog.data['error_code'] == 'account_mismatch'
        assert 'private observed account' not in catalog.model_dump_json()
        assert 'subchat_list' in {tool['name'] for tool in await server.catalog()}
        refresh = await server.execute(Request(
            operation_id='c' * 32, tool='subchat_refresh_auth', arguments={}))
        assert refresh.state == 'failed'
        assert refresh.data['error_code'] == 'account_mismatch'
        assert 'private observed account' not in refresh.model_dump_json()
        assert observed_pins == ['pinned-account', 'pinned-account']

    monkeypatch.setattr(async_api, 'async_playwright', Driver)
    monkeypatch.setattr(subchat_chrome_login, 'chrome_http_session', wrong_account)
    monkeypatch.setattr(mcp_server, 'serve_stdio', serve)
    await subchat_cli.run(None, tmp_path / 'state', mcp=True, http_only=True,
                          chrome_login_profile=tmp_path / 'dedicated',
                          expected_account_id='pinned-account', read_only_mcp=True)


async def test_http_only_real_httpx_request_uses_no_browser(tmp_path, monkeypatch):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread

    import httpx

    from anywhere_computer.subchat import SubchatAccessError
    from anywhere_computer.subchat_http import HTTPOnlySubchatBackend

    submission, payload = sample()
    calls = []
    response_status = [200]

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append((self.path, self.headers.get('Authorization'), self.headers.get('Cookie')))
            if response_status[0] == 0:
                self.connection.close()
                return
            self.send_response(response_status[0])
            self.send_header('Content-Type', 'application/json')
            self.send_header('Location', '/must-not-follow')
            self.end_headers()
            body = catalog() if self.path.startswith('/backend-api/models') else payload
            self.wfile.write(json.dumps(body).encode())

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        client = httpx.AsyncClient(
            trust_env=False, follow_redirects=False,
            transport=httpx.AsyncHTTPTransport(retries=0))
        try:
            real_stream = client.stream

            @asynccontextmanager
            async def local_stream(method, url, **kwargs):
                # Test-only routing. Production exposes no host/proxy override.
                assert method == 'GET'
                assert url in {CATALOG_URL, 'https://chatgpt.com/backend-api/conversations/'
                               + submission.conversation_id}
                assert kwargs['follow_redirects'] is False
                async with real_stream(
                        method, f'http://127.0.0.1:{server.server_port}' +
                        url.removeprefix('https://chatgpt.com'), **kwargs) as response:
                    yield response

            monkeypatch.setattr(client, 'stream', local_stream)

            async def factory():
                return client

            adapter = HTTPOnlySubchatBackend(factory, credentials())
            assert (await adapter.http_catalog())['source'] == 'preauthenticated_http'
            assert (await adapter.read_answer(submission)).text == '日本語 result'
            for status in (302, 429, 500):
                response_status[0] = status
                before = len(calls)
                with pytest.raises(ConnectionError):
                    await adapter.read_answer(submission)
                assert len(calls) == before + 1
            response_status[0] = 0
            before = len(calls)
            with pytest.raises(httpx.RemoteProtocolError):
                await adapter.read_answer(submission)
            assert len(calls) == before + 1
            response_status[0] = 403
            before = len(calls)
            for _ in range(3):
                with pytest.raises(SubchatAccessError):
                    await adapter.read_answer(submission)
            assert len(calls) == before + 1
            assert all(auth == SECRET and cookie is None for _, auth, cookie in calls)
            assert not any(path == '/must-not-follow' for path, _, _ in calls)
        finally:
            await client.aclose()
    finally:
        await asyncio.to_thread(server.shutdown)
        server.server_close()
        worker.join(timeout=5)
        assert not worker.is_alive()


def test_mcp_session_prefix_preserves_buffered_initialize_packet(tmp_path):
    initialize = {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
        'protocolVersion': '2025-03-26', 'capabilities': {},
        'clientInfo': {'name': 'fixture', 'version': '1'}}}
    wire = ''.join(json.dumps(item) + '\n' for item in (session_payload(), initialize)).encode()
    result = subprocess.run(command(tmp_path / 'state', '--http-only', '--mcp',
                                    '--http-session-stdin'), env=environment(), input=wire,
                            capture_output=True, timeout=15, check=False)
    assert result.returncode == 0, result.stderr
    reply, = map(json.loads, result.stdout.splitlines())
    assert reply['id'] == 1 and 'error' not in reply
    assert ('Ordinary Chat recovery and explicit deletion over HTTPX'
            in reply['result']['instructions'])
    assert SECRET.encode() not in result.stdout + result.stderr
