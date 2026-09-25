"""Retained clients recover an isolated shared agent, never replay dispatched writes.

The engine, sockets and ledger are real. Only the native credential store and the
child's credential provisioning are replaced with a disposable fixture value.
No installed service, user state directory or production credential is touched.
"""

import asyncio
import io
import subprocess
import sys
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from anywhere_computer import authorized_http, connection, mcp_server
from anywhere_computer.authorization import AuthorizationStore, pkce_s256
from anywhere_computer.authorized_http import AuthorizedDeviceMCP
from anywhere_computer.http_mcp import HTTPMCP

CREDENTIAL = "synthetic-shared-reconnect-credential"
PERMISSIONS = frozenset({"computer_status", "files_write", "files_read", "operations_get"})


@pytest.fixture
def isolated_agent(tmp_path, monkeypatch):
    directory = tmp_path / "shared-agent"
    children = []
    diagnostics = []
    spawn = subprocess.Popen
    source = Path(connection.__file__).resolve().parents[1]
    program = (
        "import asyncio,sys\n"
        "from pathlib import Path\n"
        "sys.path.insert(0,sys.argv[2])\n"
        "from anywhere_computer.connection import serve\n"
        f"asyncio.run(serve(Path(sys.argv[1]),credential={CREDENTIAL!r}))\n"
    )

    def launch(command, **kwargs):
        assert command[0] == sys.executable
        assert command[-3:] == ["serve", "--state-dir", str(directory)]
        log = tmp_path / f"agent-{len(children)}.stderr"
        with log.open('wb') as stderr:
            kwargs['stderr'] = stderr
            child = spawn(
                [sys.executable, "-I", "-u", "-c", program, str(directory), str(source)],
                **kwargs,
            )
        diagnostics.append(log)
        children.append(child)
        return child

    monkeypatch.setattr(connection, "local_credential", lambda *a, **kw: CREDENTIAL)
    monkeypatch.setattr(connection, "subprocess", SimpleNamespace(
        Popen=launch, DEVNULL=subprocess.DEVNULL,
    ))
    yield directory, children
    for child in children:
        if child.poll() is None:
            child.terminate()
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=10)
    # Pytest shows captured teardown output on failure. Only disposable fixture
    # processes write here; never collect installed-agent logs or credentials.
    for child, log in zip(children, diagnostics, strict=True):
        with log.open('rb') as stderr:
            stderr.seek(max(0, log.stat().st_size - 8192))
            tail = stderr.read().decode('utf-8', errors='replace')
        print(f'fixture agent pid={child.pid} exit={child.returncode}: {tail}')


def initialize_packet():
    return {
        "jsonrpc": "2.0", "id": "initialize", "method": "initialize",
        "params": {
            "protocolVersion": mcp_server.PROTOCOL_VERSION, "capabilities": {},
            "clientInfo": {"name": "isolated-reconnect-test", "version": "1"},
        },
    }


@asynccontextmanager
async def connected_client(directory, transport, monkeypatch):
    if transport == "stdio":
        ready = asyncio.get_running_loop().create_future()
        done = asyncio.Event()

        async def retain(session, source, destination):
            ready.set_result(session)
            await done.wait()

        with monkeypatch.context() as patch:
            patch.setattr(mcp_server, "serve_stdio", retain)
            patch.setattr(mcp_server, "sys", SimpleNamespace(
                stdin=SimpleNamespace(buffer=io.BytesIO()),
                stdout=SimpleNamespace(buffer=io.BytesIO()),
            ))
            task = asyncio.create_task(mcp_server.run_mcp(directory))
            try:
                session = await asyncio.wait_for(ready, 5)
                await session.handle(initialize_packet())
                await session.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
                yield session.handle, None, None
            finally:
                done.set()
                await asyncio.wait_for(task, 10)
        return

    resource = "https://reconnect.example/mcp"
    redirect = "https://client.example/callback"
    authority = AuthorizationStore(directory / "http-test-auth", resource=resource,
                                   known_tools=PERMISSIONS)
    authority.register_client("client", frozenset({redirect}))
    authority.enroll_device("owner", "device", PERMISSIONS)
    verifier = "v" * 43
    code = authority.approve(owner="owner", device="device", client="client",
                             redirect=redirect, resource=resource, tools=PERMISSIONS,
                             challenge=pkce_s256(verifier))
    token = authority.exchange_code(code=code, verifier=verifier, client="client",
                                    redirect=redirect, resource=resource).value
    grant = authority.verify(token, resource=resource)
    assert grant is not None
    backend = AuthorizedDeviceMCP(authority, agent_directory=directory,
                                  owner="owner", device="device")
    adapter = HTTPMCP(backend.authenticate, backend.session)
    port = await adapter.start()
    try:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", timeout=20,
            headers={"Authorization": f"Bearer {token}",
                     "Accept": "application/json, text/event-stream"},
        ) as http:
            started = await http.post("/mcp", json=initialize_packet())
            assert started.status_code == 200
            http.headers["MCP-Session-Id"] = started.headers["mcp-session-id"]
            await http.post("/mcp", json={"jsonrpc": "2.0",
                                         "method": "notifications/initialized"})

            async def send(packet):
                return (await http.post("/mcp", json=packet)).json()

            yield send, authority, grant.grant_id
    finally:
        await adapter.close()
        authority.close()


def tool_packet(name, arguments, request_id="a" * 32):
    return {
        "jsonrpc": "2.0", "id": name, "method": "tools/call",
        "params": {"name": name, "arguments": {**arguments, "request_id": request_id}},
    }


@pytest.mark.parametrize("transport", ["stdio", "http"])
@pytest.mark.parametrize("first_request", ["catalog", "operation"])
@pytest.mark.parametrize("crash", [False, True])
async def test_retained_client_recovers_engine_and_prior_result(
    isolated_agent, monkeypatch, transport, first_request, crash,
):
    directory, children = isolated_agent
    before = await asyncio.to_thread(connection.ensure_agent, directory)
    target = directory / "日本語-reconnect.txt"
    write = tool_packet("files_write", {"path": str(target), "text": "日本語 42 ✅"})
    async with connected_client(directory, transport, monkeypatch) as (send, _, _):
        written = (await send(write))["result"]["structuredContent"]
        assert written["state"] == "completed"
        assert len(children) == 1
        if crash:
            children[0].kill()  # Only this fixture's disposable engine, never a user service.
        else:
            stopped = await connection.exchange(directory, "__stop")
            assert stopped.state == "completed"
        await asyncio.to_thread(children[0].wait, timeout=10)
        assert (directory / "agent.json").exists() is crash
        lookup = tool_packet("operations_get", {"operation_id": written["operation_id"]},
                             request_id="b" * 32)
        if first_request == "catalog":
            listed = await send({"jsonrpc": "2.0", "id": "after-stop", "method": "tools/list"})
            assert "files_read" in {item["name"] for item in listed["result"]["tools"]}
        recovered = (await send(lookup))["result"]["structuredContent"]
        assert recovered["state"] == "completed"
        assert recovered["data"]["operation_id"] == written["operation_id"]
        assert recovered["data"]["state"] == "completed"
        assert recovered["data"]["data"]["sha256"] == written["data"]["sha256"]
        # An exact duplicate resolves to the durable result, not a second create.
        repeated = (await send(write))["result"]["structuredContent"]
        assert repeated == written
        assert target.read_text(encoding="utf-8") == "日本語 42 ✅"
        after = await connection.exchange(directory, "__status")
        assert after.data["instance_id"] != before["instance_id"]
        assert after.data["runtime_id"] == before["runtime_id"]
        assert len(children) == 2


async def test_stdio_response_loss_does_not_redispatch_write(isolated_agent, monkeypatch):
    directory, children = isolated_agent
    await asyncio.to_thread(connection.ensure_agent, directory)
    exchange = mcp_server.exchange
    writes = []

    async def lose_response(directory, tool, *args, **kwargs):
        result = await exchange(directory, tool, *args, **kwargs)
        if tool == "files_write":
            writes.append(kwargs["operation_id"])
            raise ConnectionError("synthetic lost response after a committed write")
        return result

    monkeypatch.setattr(mcp_server, "exchange", lose_response)
    target = directory / "once.txt"
    async with connected_client(directory, "stdio", monkeypatch) as (send, _, _):
        response = await send(tool_packet("files_write", {"path": str(target), "text": "once"}))
        assert response["result"]["structuredContent"]["state"] == "unknown"
        assert response["result"]["structuredContent"]["operation_id"] == "a" * 32
        recovered = await send(tool_packet("operations_get", {"operation_id": "a" * 32},
                                            request_id="b" * 32))
        assert recovered["result"]["structuredContent"]["data"]["state"] == "completed"
        assert writes == ["a" * 32]
        assert target.read_text() == "once"
        assert len(children) == 1


async def test_http_rechecks_grant_after_recovery(isolated_agent, monkeypatch):
    directory, children = isolated_agent
    await asyncio.to_thread(connection.ensure_agent, directory)
    async with connected_client(directory, "http", monkeypatch) as (send, authority, grant_id):
        loop = asyncio.get_running_loop()

        async def revoke():
            authority.revoke(owner="owner", grant=grant_id)

        def recover_then_revoke(*args, **kwargs):
            result = connection.ensure_agent(*args, **kwargs)
            asyncio.run_coroutine_threadsafe(revoke(), loop).result(timeout=5)
            return result

        monkeypatch.setattr(authorized_http, "ensure_agent", recover_then_revoke, raising=False)
        target = directory / "revoked.txt"
        response = await send(tool_packet("files_write", {"path": str(target), "text": "denied"}))
        assert "error" in response or response["result"]["isError"]
        assert not target.exists()
        assert len(children) == 1


@pytest.mark.parametrize("transport", ["stdio", "http"])
async def test_concurrent_catalog_recovery_launches_one_agent(
    isolated_agent, monkeypatch, transport,
):
    directory, children = isolated_agent
    await asyncio.to_thread(connection.ensure_agent, directory)
    async with connected_client(directory, transport, monkeypatch) as (send, _, _):
        children[0].kill()
        await asyncio.to_thread(children[0].wait, timeout=10)
        results = await asyncio.gather(*[
            send({"jsonrpc": "2.0", "id": index, "method": "tools/list"})
            for index in range(4)
        ])
        for result in results:
            assert 'result' in result, result
            assert "files_read" in {tool["name"] for tool in result["result"]["tools"]}
        assert len(children) == 2


async def test_concurrent_connectors_wait_through_slow_agent_start(
    isolated_agent, monkeypatch,
):
    directory, children = isolated_agent
    original = connection.exchange
    release_readiness = threading.Event()

    async def delayed_status(*args, **kwargs):
        if children and args[1] == "__status":
            release_readiness.wait(timeout=25)
        return await original(*args, **kwargs)

    monkeypatch.setattr(connection, "exchange", delayed_status)
    first = asyncio.create_task(asyncio.to_thread(connection.ensure_agent, directory))
    for _ in range(100):
        if children:
            break
        await asyncio.sleep(0.05)
    assert children
    second = asyncio.create_task(asyncio.to_thread(connection.ensure_agent, directory))
    try:
        await asyncio.sleep(16)
    finally:
        release_readiness.set()
    first_result, second_result = await asyncio.gather(first, second)
    assert first_result["instance_id"] == second_result["instance_id"]
    assert len(children) == 1


@pytest.mark.parametrize("transport", ["stdio", "http"])
async def test_recovery_identity_rejection_never_dispatches_or_restarts(
    isolated_agent, monkeypatch, transport,
):
    from anywhere_computer.owner_json_pipe import OwnerPipeIdentityError

    directory, children = isolated_agent
    await asyncio.to_thread(connection.ensure_agent, directory)

    def rejected(*args, **kwargs):
        raise OwnerPipeIdentityError("synthetic owner mismatch")

    module = mcp_server if transport == "stdio" else authorized_http
    monkeypatch.setattr(module, "ensure_agent", rejected)
    target = directory / "identity-rejected.txt"
    async with connected_client(directory, transport, monkeypatch) as (send, _, _):
        packet = tool_packet("files_write", {"path": str(target), "text": "must not write"})
        if transport == "stdio":
            with pytest.raises(OwnerPipeIdentityError):
                await send(packet)
        else:
            assert "error" in await send(packet)
        assert not target.exists()
        assert len(children) == 1 and children[0].poll() is None


@pytest.mark.parametrize('failure', ['OwnerPipeTimeout', 'OwnerPipeIdentityError',
                                   'OwnerPipeProtocolError'])
async def test_startup_retries_only_transient_owner_pipe_timeout(
        isolated_agent, monkeypatch, failure):
    from anywhere_computer import owner_json_pipe

    error_type = getattr(owner_json_pipe, failure)

    directory, children = isolated_agent
    original = connection.exchange
    injected = []

    async def transient(*args, **kwargs):
        if children and not injected:
            injected.append(True)
            raise error_type('synthetic readiness failure')
        return await original(*args, **kwargs)

    monkeypatch.setattr(connection, 'exchange', transient)
    if failure == 'OwnerPipeTimeout':
        result = await asyncio.to_thread(connection.ensure_agent, directory)
        assert result['runtime_id'] == connection.runtime_identity()
    else:
        with pytest.raises(error_type):
            await asyncio.to_thread(connection.ensure_agent, directory)
    assert injected == [True] and len(children) == 1
