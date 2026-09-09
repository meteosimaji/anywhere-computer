import asyncio
import sys
import uuid

import pytest

from anywhere_computer.models import Request
from anywhere_computer.ssh_client import SSHBackend


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
