"""CLI, stdio MCP and real HTTP-client tests; all credentials and HTTP data are synthetic."""
import asyncio
import json
import os
import subprocess
import sys
from contextlib import asynccontextmanager
from io import StringIO
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
async def test_http_only_cli_owns_only_request_client_and_disposes_it(tmp_path, monkeypatch, fail):
    import playwright.async_api

    from anywhere_computer import subchat_cli

    ledger = Ledger(tmp_path)
    store = SubchatSubmissions(ledger.connection)
    submission, payload = seed(store, receipt=False)
    ledger.close()
    client = Client(payload)
    events = []

    async def dispose():
        events.append('client_closed')

    client.dispose = dispose

    async def new_context(**kwargs):
        assert kwargs == {}  # No browser profile, cookies or storage_state imported.
        events.append('client')
        return client

    @asynccontextmanager
    async def runtime():
        events.append('runtime')
        try:
            # No chromium attribute: any attempt to launch a browser fails this test.
            yield SimpleNamespace(request=SimpleNamespace(new_context=new_context))
        finally:
            events.append('runtime_closed')

    monkeypatch.setattr(playwright.async_api, 'async_playwright', runtime)
    commands = [{'action': 'catalog'},
                {'action': 'recover', 'operation_id': submission.operation_id}]
    output = StringIO()
    monkeypatch.setattr(subchat_cli.sys, 'stdin',
                        StringIO(''.join(json.dumps(item) + '\n' for item in commands)))
    monkeypatch.setattr(subchat_cli.sys, 'stdout', output)
    if fail:
        async def broken(service, source, destination):
            await service.backend.http_catalog()
            raise RuntimeError('fixture observer failure')
        monkeypatch.setattr(subchat_cli, 'process_lines', broken)
        with pytest.raises(RuntimeError, match='fixture observer failure'):
            await subchat_cli.run(None, tmp_path, http_only=True, http_session=credentials())
    else:
        await subchat_cli.run(None, tmp_path, http_only=True, http_session=credentials())
        replies = list(map(json.loads, output.getvalue().splitlines()))
        assert replies[0]['generation_transport'] == 'unavailable'
        assert replies[1]['state'] == 'completed'
        assert replies[1]['answer'] == '日本語 result'
    assert events == ['runtime', 'client', 'client_closed', 'runtime_closed']
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
                assert 'Browser-free read-only' in initialized.instructions
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


async def test_http_only_real_api_request_context_uses_no_browser(tmp_path, monkeypatch):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread

    from playwright.async_api import BrowserType, async_playwright

    from anywhere_computer.subchat import SubchatAccessError
    from anywhere_computer.subchat_http import HTTPOnlySubchatBackend

    submission, payload = sample()
    calls = []
    response_status = [200]

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append((self.path, self.headers.get('Authorization'), self.headers.get('Cookie')))
            self.send_response(response_status[0])
            self.send_header('Content-Type', 'application/json')
            self.send_header('Location', '/must-not-follow')
            self.end_headers()
            body = catalog() if self.path.startswith('/backend-api/models') else payload
            self.wfile.write(json.dumps(body).encode())

        def log_message(self, *_):
            pass

    async def forbidden(*args, **kwargs):
        pytest.fail('Browser-free HTTP transport tried to launch a browser')

    monkeypatch.setattr(BrowserType, 'launch', forbidden)
    monkeypatch.setattr(BrowserType, 'launch_persistent_context', forbidden)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        async with async_playwright() as driver:
            client = await driver.request.new_context()
            try:
                real_get = client.get

                async def local_get(url, **kwargs):
                    # Test-only routing. Production exposes no host/proxy override.
                    assert url in {CATALOG_URL, 'https://chatgpt.com/backend-api/conversations/'
                                   + submission.conversation_id}
                    assert kwargs['max_redirects'] == kwargs['max_retries'] == 0
                    return await real_get(f'http://127.0.0.1:{server.server_port}' +
                                          url.removeprefix('https://chatgpt.com'), **kwargs)

                monkeypatch.setattr(client, 'get', local_get)

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
                response_status[0] = 403
                before = len(calls)
                for _ in range(3):
                    with pytest.raises(SubchatAccessError):
                        await adapter.read_answer(submission)
                assert len(calls) == before + 1
                assert all(auth == SECRET and cookie is None for _, auth, cookie in calls)
                assert not any(path == '/must-not-follow' for path, _, _ in calls)
            finally:
                await client.dispose()
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
    assert 'Browser-free read-only' in reply['result']['instructions']
    assert SECRET.encode() not in result.stdout + result.stderr
