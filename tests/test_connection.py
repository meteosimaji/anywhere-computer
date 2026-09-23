import asyncio
import secrets
from pathlib import Path

import pytest

from anywhere_computer.connection import exchange, serve


async def test_partial_unauthenticated_connections_have_an_admission_bound(agent, monkeypatch):
    import anywhere_computer.connection as connection

    directory, credential = agent
    monkeypatch.setattr(connection, "MAX_CONNECTIONS", 2, raising=False)
    port = connection.load_endpoint(directory)["port"]
    clients = []
    try:
        for _ in range(2):
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            clients.append((reader, writer))
            writer.write(b'{')
            await writer.drain()
        extra_reader, extra_writer = await asyncio.open_connection("127.0.0.1", port)
        clients.append((extra_reader, extra_writer))
        assert await asyncio.wait_for(extra_reader.read(1), 0.5) == b""
    finally:
        for _, writer in clients:
            writer.close()
            await writer.wait_closed()
    # Let the server release the two partial frames before the authenticated call.
    await asyncio.sleep(0.02)
    assert (await exchange(directory, "__status", credential=credential)).state == "completed"


@pytest.fixture
async def agent(tmp_path):
    credential = secrets.token_urlsafe(32)
    shutdown = asyncio.Event()
    task = asyncio.create_task(serve(tmp_path, credential=credential, shutdown=shutdown))
    for _ in range(300):
        if (tmp_path / "agent.json").exists():
            break
        if task.done():
            await task
        await asyncio.sleep(0.01)
    else:
        pytest.fail("Agent failed to publish endpoint")
    yield tmp_path, credential
    shutdown.set()
    await asyncio.wait_for(task, 10)
    # Windows mandatory locks prevent reading agent.lock while it is held.
    # Inspect every file after shutdown, including the released lock file.
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert credential.encode() not in path.read_bytes()


async def test_authenticated_rpc_and_catalog(agent):
    directory, credential = agent
    status = await exchange(directory, "__status", credential=credential)
    assert status.data["state"] == "ready"
    assert status.data["remote_ready"] is False
    catalog = await exchange(directory, "__catalog", credential=credential)
    assert len(catalog.data["tools"]) == 71
    assert "gui_native_press" in {tool["name"] for tool in catalog.data["tools"]}
    assert "outputSchema" in catalog.data["tools"][0]
    with pytest.raises(ConnectionError):
        await exchange(directory, "__status", credential="wrong")
    again = await exchange(directory, "__status", credential=credential)
    assert again.data["instance_id"] == status.data["instance_id"]


async def test_local_transport_does_not_persist_credential(agent):
    directory, credential = agent
    await exchange(directory, "computer_status", credential=credential)
    for path in Path(directory).rglob("*"):
        if path.is_file() and path.name != "agent.lock":
            assert credential.encode() not in path.read_bytes()


async def test_service_refuses_shutdown_while_session_active(agent):
    import os
    import shlex
    import subprocess
    import sys

    directory, credential = agent
    argv = [sys.executable, "-u", "-c", "import time; time.sleep(20)"]
    command = subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)
    session = await exchange(
        directory,
        "terminal_start",
        {"command": command, "cwd": str(directory)},
        credential=credential,
    )
    stopped = await exchange(directory, "__stop", credential=credential)
    assert stopped.state == "failed"
    await exchange(
        directory,
        "terminal_stop",
        {"session_id": session.data["session_id"]},
        credential=credential,
    )


async def test_shutdown_rejects_requests_already_waiting_on_a_connection(agent):
    import json

    from anywhere_computer.connection import load_endpoint
    from anywhere_computer.models import Reply

    directory, credential = agent
    port = load_endpoint(directory)["port"]
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    target = directory / "after-stop.txt"
    packet = json.dumps({
        "credential": credential,
        "request": {
            "operation_id": "b" * 32,
            "tool": "files_write",
            "arguments": {"path": str(target), "text": "must not execute"},
        },
    }).encode()
    try:
        # The accepted connection is retained while shutdown drains existing clients.
        writer.write(packet)
        await writer.drain()
        stopped = await exchange(directory, "__stop", credential=credential)
        assert stopped.state == "completed"
        writer.write(b"\n")
        await writer.drain()
        reply = Reply.model_validate_json(await asyncio.wait_for(reader.readline(), 2))
        assert reply.state == "failed"
        assert "stopping" in reply.error.lower()
        assert not target.exists()
    finally:
        writer.close()
        await writer.wait_closed()


async def test_older_connector_keeps_real_compatible_engine_and_operation(agent, monkeypatch):
    import anywhere_computer.connection as connection

    directory, credential = agent
    before = await exchange(directory, '__status', credential=credential)
    identity = 'd' * 32
    written = await exchange(directory, 'files_write',
        {'path': str(directory / 'keep.txt'), 'text': 'preserve'},
        operation_id=identity, credential=credential)
    assert written.state == 'completed'
    monkeypatch.setattr(connection, 'runtime_identity', lambda: 'older-connector-build')
    monkeypatch.setattr(connection, 'local_credential', lambda *a, **kw: credential)
    connected = await asyncio.to_thread(connection.ensure_agent, directory)
    assert connected['instance_id'] == before.data['instance_id']
    recovered = await exchange(directory, 'operations_get',
        {'operation_id': identity}, credential=credential)
    assert recovered.data['operation_id'] == identity
    assert recovered.data['data']['sha256'] == written.data['sha256']


async def test_shared_gateway_requires_local_authentication_and_granted_tool(agent):
    from anywhere_computer.connection import exchange_remote
    from anywhere_computer.models import Request

    directory, credential = agent
    target = directory / 'unauthorized.txt'
    request = Request(operation_id='e' * 32, tool='files_write',
                      arguments={'path': str(target), 'text': 'denied'})
    with pytest.raises(ConnectionError):
        await exchange_remote(directory, 'grant', frozenset({'files_write'}), request,
                              credential='invalid')
    assert not target.exists()
    denied = await exchange_remote(directory, 'grant', frozenset({'files_read'}), request,
                                   credential=credential)
    assert denied.state == 'failed'
    assert not target.exists()
    catalog = await exchange(directory, '__catalog', credential=credential)
    assert '__remote' not in {entry['name'] for entry in catalog.data['tools']}


async def test_owner_pipe_route_does_not_read_logon_credentials(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace

    from anywhere_computer import connection
    from anywhere_computer.models import Reply, Request
    from anywhere_computer.owner_json_pipe import OwnerPipeIdentityError

    endpoint = {
        'pipe_name': r'\\.\pipe\anywhere-owner-json-' + 'a' * 48,
        'server_id': 'b' * 64, 'server_pid': 123, 'server_creation_time': 1.0,
        'owner_sid': 'S-1-5-21-123', 'server_executable': str(tmp_path / 'python'),
        'max_payload_bytes': connection.WIRE_LIMIT, 'protocol': 'anywhere-owner-json-v1',
    }
    (tmp_path / 'agent.json').write_text(json.dumps({'port': 1234, 'owner_pipe': endpoint}))
    monkeypatch.setattr(connection, 'os', SimpleNamespace(name='nt'))

    def forbidden(*args, **kwargs):
        pytest.fail('Pipe route must not access the credential store or fallback to TCP')

    monkeypatch.setattr(connection, 'local_credential', forbidden)
    monkeypatch.setattr(connection.asyncio, 'open_connection', forbidden)
    calls = []

    async def pipe_call(selected, payload, *, timeout):
        request = Request.model_validate_json(payload)
        calls.append(request.operation_id)
        assert selected.to_dict() == endpoint and timeout == 7
        return Reply(operation_id=request.operation_id, state='completed',
                     data={'value': 42}).model_dump_json().encode()

    monkeypatch.setattr(connection, 'request_owner_json_pipe_async', pipe_call)
    result = await connection.exchange(tmp_path, '__status', operation_id='c' * 32, timeout=7)
    assert result.data == {'value': 42} and calls == ['c' * 32]

    async def wrong_owner(*args, **kwargs):
        raise OwnerPipeIdentityError('synthetic wrong owner')

    monkeypatch.setattr(connection, 'request_owner_json_pipe_async', wrong_owner)
    with pytest.raises(OwnerPipeIdentityError):
        await connection.exchange(tmp_path, '__status')


async def test_endpoint_publication_failure_closes_started_server(tmp_path, monkeypatch):
    from anywhere_computer import connection

    original_start = asyncio.start_server
    original_write = Path.write_text
    started = []

    async def track_start(*args, **kwargs):
        server = await original_start(*args, **kwargs)
        started.append(server)
        return server

    def fail_publication(path, *args, **kwargs):
        if path.name == 'agent.pending.json':
            raise OSError('synthetic endpoint publication failure')
        return original_write(path, *args, **kwargs)

    monkeypatch.setattr(asyncio, 'start_server', track_start)
    monkeypatch.setattr(Path, 'write_text', fail_publication)
    try:
        with pytest.raises(OSError, match='synthetic endpoint publication failure'):
            await connection.serve(tmp_path, credential='synthetic-test-credential')
        assert len(started) == 1
        assert not started[0].is_serving()
    finally:
        for server in started:
            server.close()
            await server.wait_closed()


@pytest.mark.skipif(__import__('sys').platform != 'win32', reason='Windows native owner pipe')
async def test_native_owner_pipe_files_and_operation_recovery(agent, monkeypatch):
    from anywhere_computer import connection

    directory, _ = agent

    def forbidden(*args, **kwargs):
        pytest.fail('Native pipe route must not read the logon credential store')

    monkeypatch.setattr(connection, 'local_credential', forbidden)
    status = await exchange(directory, '__status')
    target = directory / 'owner-pipe-日本語.txt'
    written = await exchange(directory, 'files_write',
                             {'path': str(target), 'text': '日本語 🚀 42'})
    assert written.state == 'completed'
    read = await exchange(directory, 'files_read', {'path': str(target)})
    assert read.state == 'completed' and read.data['text'] == '日本語 🚀 42'
    recovered = await exchange(directory, 'operations_get',
                               {'operation_id': written.operation_id})
    assert recovered.state == 'completed'
    assert recovered.data['operation_id'] == written.operation_id
    assert (await exchange(directory, '__status')).data['instance_id'] == status.data['instance_id']


async def test_shutdown_transport_error_still_closes_engine_and_metadata(tmp_path, monkeypatch):
    from anywhere_computer import connection

    start = asyncio.start_server
    close = connection.Engine.close
    closed = []

    async def start_with_close_error(*args, **kwargs):
        server = await start(*args, **kwargs)
        wait_closed = server.wait_closed

        async def failing_wait():
            await wait_closed()
            raise OSError('synthetic transport cleanup failure')

        monkeypatch.setattr(server, 'wait_closed', failing_wait)
        return server

    async def track_close(engine):
        await close(engine)
        closed.append(True)

    monkeypatch.setattr(asyncio, 'start_server', start_with_close_error)
    monkeypatch.setattr(connection.Engine, 'close', track_close)
    shutdown = asyncio.Event()
    shutdown.set()
    with pytest.raises(OSError, match='synthetic transport cleanup failure'):
        await connection.serve(tmp_path, credential='synthetic-test', shutdown=shutdown)
    assert closed == [True]
    assert not (tmp_path / 'agent.json').exists()
