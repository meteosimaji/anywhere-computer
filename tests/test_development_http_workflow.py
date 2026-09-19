"""Real Python execution and recovery through the authenticated Chat MCP wire.

Only temporary files and local processes are used. No Codex/browser peer is mocked
because this workflow does not request either; GUI/subchat need separate acceptance.
"""

import asyncio
import uuid

import httpx
from test_engine import python_command
from test_http_service import initialize

from anywhere_computer import __version__
from anywhere_computer.authorization import AuthorizationStore, pkce_s256
from anywhere_computer.authorized_http import AuthorizedDeviceMCP
from anywhere_computer.engine import Engine, runtime_identity
from anywhere_computer.http_mcp import HTTPMCP
from anywhere_computer.mcp_server import OPERATION_META


async def test_chat_creates_runs_code_and_recovers_after_engine_restart(tmp_path):
    tools = frozenset({
        'computer_status', 'files_write', 'files_read', 'files_edit',
        'terminal_start', 'terminal_output', 'terminal_stop', 'operations_get',
    })
    authority = AuthorizationStore(
        tmp_path / 'auth', resource='https://fixture.test/mcp', known_tools=tools,
    )
    redirect = 'https://fixture.test/callback'
    authority.register_client('chat', frozenset({redirect}))
    authority.enroll_device('owner', 'development', tools)
    code = authority.approve(
        owner='owner', device='development', client='chat', redirect=redirect,
        resource=authority.resource, tools=tools, challenge=pkce_s256('v' * 43),
    )
    token = authority.exchange_code(
        code=code, verifier='v' * 43, client='chat', redirect=redirect,
        resource=authority.resource,
    ).value
    engine = Engine(tmp_path / 'engine')

    async def serve(current):
        backend = AuthorizedDeviceMCP(
            authority, current, owner='owner', device='development', client='chat',
        )
        adapter = HTTPMCP(backend.authenticate, backend.session)
        port = await adapter.start()
        return adapter, port

    adapter, port = await serve(engine)
    expected_runtime = runtime_identity()
    run_id = uuid.uuid4().hex
    output_id = uuid.uuid4().hex
    path = tmp_path / '集計.py'
    source = (
        "from pathlib import Path\n"
        "values = [1200, 980, 800, 520]\n"
        "print(sum(values), flush=True)\n"
        "with Path('runs.txt').open('a', encoding='utf-8', newline='') as record:\n"
        "    record.write('executed\\n')\n"
    )
    command = python_command(f"import runpy; runpy.run_path({str(path)!r}, run_name='__main__')")
    run_args = {'command': command, 'cwd': str(tmp_path)}
    try:
        for phase in ('execute', 'recover'):
            async with httpx.AsyncClient(base_url=f'http://127.0.0.1:{port}', timeout=40) as http:
                headers = await initialize(http, token)
                discovered = await http.post('/mcp', headers=headers, json={
                    'jsonrpc': '2.0', 'id': 'catalog', 'method': 'tools/list',
                })
                assert discovered.status_code == 200
                assert {t['name'] for t in discovered.json()['result']['tools']} == tools

                async def call(name, args=None, operation=None, *, request_headers=headers):
                    response = await http.post('/mcp', headers=request_headers, json={
                        'jsonrpc': '2.0', 'id': uuid.uuid4().hex, 'method': 'tools/call',
                        'params': {'name': name, 'arguments': args or {},
                                   '_meta': {OPERATION_META: operation or uuid.uuid4().hex}},
                    })
                    assert response.status_code == 200
                    result = response.json()['result']['structuredContent']
                    assert result['state'] == 'completed', result
                    return result['data']

                status = await call('computer_status')
                assert status['version'] == __version__
                assert status['runtime_id'] == expected_runtime
                if phase == 'execute':
                    original_instance = status['instance_id']
                    original_session = headers['MCP-Session-Id']
                    await call('files_write', {'path': str(path), 'text': source})
                    read = await call('files_read', {'path': str(path)})
                    assert read['text'] == source
                    await call('files_edit', {
                        'path': str(path), 'old_text': '520', 'new_text': '521',
                        'expected_sha256': read['sha256'],
                    })
                    started = await call('terminal_start', run_args, run_id)
                    session = {'session_id': started['session_id']}
                    async with asyncio.timeout(20):
                        while True:
                            output = await call('terminal_output', {**session, 'wait_ms': 100})
                            if output['output_eof']:
                                break
                    assert output['exit_code'] == 0 and output['text'].strip() == '3501'
                    saved_output = await call('terminal_output', session, output_id)
                    assert saved_output['dropped_bytes'] == 0
                    await call('terminal_stop', session)
                else:
                    assert status['instance_id'] != original_instance
                    assert headers['MCP-Session-Id'] != original_session
                    assert status['active_sessions'] == 0
                    recovered = await call('operations_get', {'operation_id': output_id})
                    assert recovered['state'] == 'completed'
                    assert recovered['data'] == saved_output
                    replay = await call('terminal_start', run_args, run_id)
                    assert replay == started
                    assert (await call('computer_status'))['active_sessions'] == 0
                    recorded = await call('files_read', {'path': str(tmp_path / 'runs.txt')})
                    assert recorded['text'] == 'executed\n'
                assert (await http.delete('/mcp', headers=headers)).status_code == 200
            if phase == 'execute':
                await adapter.close()
                await engine.close()
                engine = Engine(tmp_path / 'engine')
                adapter, port = await serve(engine)
    finally:
        await adapter.close()
        await engine.close()
        authority.close()
