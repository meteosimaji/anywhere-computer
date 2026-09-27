import asyncio
import os
import sys
import time
import uuid

import pytest

from anywhere_computer.authorization import AuthorizationStore, pkce_s256
from anywhere_computer.authorized_http import AuthorizedDeviceMCP
from anywhere_computer.delegated_tasks import DelegatedTaskGrant, DelegatedTaskStore
from anywhere_computer.engine import Engine
from anywhere_computer.models import Request
from anywhere_computer.ssh_client import SSHBackend


def test_empty_child_bearer_never_selects_ordinary_mcp():
    with pytest.raises(ValueError, match='must not be empty'):
        SSHBackend('fixture', child_bearer='')


@pytest.fixture
async def ssh_peer(tmp_path, monkeypatch):
    program = tmp_path / 'peer.py'
    program.write_text('''import asyncio, sys
from pathlib import Path
from anywhere_computer.engine import Engine
from anywhere_computer.mcp_server import MCPSession, serve_stdio
async def main():
    engine = Engine(Path(sys.argv[1]))
    async def catalog():
        return engine.catalog()
    try:
        await serve_stdio(MCPSession(catalog, engine.execute), sys.stdin.buffer, sys.stdout.buffer)
    finally:
        await engine.close()
asyncio.run(main())
''')
    monkeypatch.setattr('anywhere_computer.ssh_client.ssh_command',
                        lambda host: [sys.executable, '-I', str(program), str(tmp_path / 'agent')])
    backend = SSHBackend('fixture')
    try:
        yield backend
    finally:
        await backend.close()


async def test_actual_stdio_protocol_and_operation_replay(ssh_peer, tmp_path):
    tools = await ssh_peer.catalog()
    assert any(tool['name'] == 'files_write' for tool in tools)
    operation = Request(operation_id=uuid.uuid4().hex, tool='files_write',
                        arguments={'path': str(tmp_path / 'once.txt'), 'text': 'once'})
    first = await ssh_peer.execute(operation)
    second = await ssh_peer.execute(operation)
    assert first.state == 'completed' and second == first
    process = ssh_peer.process
    await ssh_peer.close()
    assert process.returncode is not None


@pytest.mark.skipif(os.name == 'nt', reason='requires POSIX dir_fd confinement')
async def test_child_ssh_stdio_scopes_replay_and_revocation(tmp_path, monkeypatch):
    engine = Engine(tmp_path / 'agent')
    authority = AuthorizationStore(tmp_path / 'authority',
                                   resource='https://fixture.example/mcp',
                                   known_tools=frozenset(engine.tools))
    authority.register_client('chat', frozenset({'https://chat.example/callback'}))
    scopes = frozenset({'files_read', 'files_write', 'operations_get',
                        'terminal_start', 'gui_click'})
    authority.enroll_device('owner', 'gateway', scopes)
    code = authority.approve(owner='owner', device='gateway', client='chat',
                             redirect='https://chat.example/callback',
                             resource=authority.resource, tools=scopes,
                             challenge=pkce_s256('v' * 43))
    parent_token = authority.exchange_code(
        code=code, verifier='v' * 43, client='chat',
        redirect='https://chat.example/callback', resource=authority.resource,
    ).value
    parent_id = await AuthorizedDeviceMCP(
        authority, engine, owner='owner', device='gateway').authenticate(parent_token)
    assert parent_id is not None
    allowed = tmp_path / 'allowed'
    allowed.mkdir()
    (allowed / 'source.txt').write_text('safe', encoding='utf-8')
    delegation = DelegatedTaskStore(tmp_path / 'delegation', authority)
    child_id = uuid.uuid4().hex
    bearer = delegation.issue(DelegatedTaskGrant(
        owner='owner', child_id=child_id, parent_grant_id=parent_id,
        device_id='local', tools=frozenset({'files_read', 'operations_get'}),
        read_roots=(str(allowed.resolve()),), expires_at=time.time() + 600,
    ))
    program = tmp_path / 'child-peer.py'
    program.write_text('''import asyncio, sys
from pathlib import Path
from anywhere_computer.authorization import AuthorizationStore
from anywhere_computer.authorized_http import AuthorizedDeviceMCP
from anywhere_computer.delegated_tasks import DelegatedTaskStore
from anywhere_computer.engine import Engine
from anywhere_computer.mcp_server import serve_stdio
from anywhere_computer.ssh_child import SSHChildSession
async def main():
    root = Path(sys.argv[1])
    engine = Engine(root / 'agent')
    authority = AuthorizationStore(root / 'authority',
        resource='https://fixture.example/mcp', known_tools=frozenset(engine.tools))
    delegated = DelegatedTaskStore(root / 'delegation', authority)
    backend = AuthorizedDeviceMCP(authority, engine, owner='owner',
                                  device='gateway', delegated_tasks=delegated)
    try:
        await serve_stdio(SSHChildSession(backend), sys.stdin.buffer, sys.stdout.buffer)
    finally:
        delegated.close()
        authority.close()
        await engine.close()
asyncio.run(main())
''')
    monkeypatch.setattr('anywhere_computer.ssh_client.ssh_command',
                        lambda host, command='mcp': [sys.executable, '-I',
                                                     str(program), str(tmp_path)])
    try:
        unauthenticated = SSHBackend('fixture', child_bearer='wrong')
        try:
            with pytest.raises(ConnectionError, match='response did not match'):
                await unauthenticated.catalog()
        finally:
            await unauthenticated.close()
        backend = SSHBackend('fixture', child_bearer=bearer)
        try:
            names = {item['name'] for item in await backend.catalog()}
            assert names == {'files_read', 'operations_get', 'delegated_identity'}
            identified = await backend.execute(Request(
                operation_id=uuid.uuid4().hex, tool='delegated_identity', arguments={}))
            assert identified.state == 'completed'
            assert identified.data == {'child_id': child_id, 'device_id': 'local'}
            read = Request(operation_id=uuid.uuid4().hex, tool='files_read',
                           arguments={'path': str(allowed / 'source.txt')})
            assert (await backend.execute(read)).state == 'completed'
            assert (await backend.execute(read)).state == 'completed'
            outside = tmp_path / 'private.txt'
            outside.write_text('private', encoding='utf-8')
            denied = await backend.execute(Request(operation_id=uuid.uuid4().hex,
                                                   tool='files_read',
                                                   arguments={'path': str(outside)}))
            assert denied.state == 'failed' and denied.data['dispatched'] is False
            delegation.revoke(child_id)
            with pytest.raises(ConnectionError):
                await backend.catalog()
        finally:
            await backend.close()
    finally:
        delegation.close()
        authority.close()
        await engine.close()


async def test_cancelled_initialization_reaps_child(tmp_path, monkeypatch):
    monkeypatch.setattr('anywhere_computer.ssh_client.ssh_command',
                        lambda host: [sys.executable, '-I', '-c', 'import time; time.sleep(60)'])
    backend = SSHBackend('fixture')
    waiting = asyncio.create_task(backend.catalog())
    try:
        async with asyncio.timeout(5):
            while backend.process is None:
                await asyncio.sleep(.01)
        process = backend.process
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
    finally:
        await backend.close()
    assert process.returncode is not None


async def test_peer_without_operation_ids_is_rejected_before_tool_dispatch(tmp_path, monkeypatch):
    program = tmp_path / 'old-peer.py'
    observed = tmp_path / 'received.jsonl'
    program.write_text('''import json, sys
from pathlib import Path
packet = json.loads(sys.stdin.readline())
Path(sys.argv[1]).write_text(packet['method'])
print(json.dumps({'jsonrpc': '2.0', 'id': packet['id'], 'result': {
    'protocolVersion': '2025-11-25', 'capabilities': {}}}), flush=True)
for line in sys.stdin:
    with Path(sys.argv[1]).open('a') as out:
        out.write(json.loads(line)['method'])
''')
    monkeypatch.setattr('anywhere_computer.ssh_client.ssh_command',
                        lambda host: [sys.executable, '-I', str(program), str(observed)])
    backend = SSHBackend('fixture')
    try:
        with pytest.raises(ConnectionError, match='operation IDs'):
            await backend.catalog()
    finally:
        await backend.close()
    assert observed.read_text() == 'initialize'
