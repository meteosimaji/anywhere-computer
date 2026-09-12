import asyncio

import httpx
import pytest

from anywhere_computer.authorization import AuthorizationStore, pkce_s256
from anywhere_computer.authorized_http import AuthorizedDeviceMCP
from anywhere_computer.connection import exchange, serve
from anywhere_computer.engine import Engine
from anywhere_computer.http_mcp import HTTPMCP


@pytest.mark.parametrize("shared", [False, True])
async def test_real_http_enforces_device_scope_and_revocation(tmp_path, monkeypatch, shared):
    engine = Engine(tmp_path / "agent")
    shared_directory = tmp_path / "shared-agent"
    stopped = asyncio.Event()
    service = None
    if shared:
        monkeypatch.setattr("anywhere_computer.connection.local_credential",
                            lambda *a, **kw: "shared-agent-fixture")
        service = asyncio.create_task(serve(
            shared_directory, credential="shared-agent-fixture", shutdown=stopped,
        ))
        async with asyncio.timeout(5):
            while not (shared_directory / "agent.json").exists():
                if service.done():
                    await service
                await asyncio.sleep(0.01)
    resource = "https://computer.example/mcp"
    redirect = "https://client.example/callback"
    authority = AuthorizationStore(
        tmp_path / "auth", resource=resource, known_tools=frozenset(engine.tools)
    )
    authority.register_client("client", frozenset({redirect}))
    permissions = frozenset({"computer_status", "files_read", "files_write", "operations_get"})
    authority.enroll_device("owner", "device", permissions)
    authority.enroll_device("owner", "other-device", permissions)

    def issue(device, tools):
        verifier = "x" * 43
        code = authority.approve(
            owner="owner",
            device=device,
            client="client",
            redirect=redirect,
            resource=resource,
            tools=frozenset(tools),
            challenge=pkce_s256(verifier),
        )
        return authority.exchange_code(
            code=code,
            verifier=verifier,
            client="client",
            redirect=redirect,
            resource=resource,
        ).value

    read_token = issue("device", {"computer_status", "files_read", "operations_get"})
    wrong_device_token = issue("other-device", permissions)
    write_token = issue("device", permissions)
    limited = AuthorizedDeviceMCP(
        authority,
        engine,
        owner="owner",
        device="device",
        client="client",
        allowed_tools=frozenset({"computer_status", "files_read", "operations_get"}),
    )
    assert await limited.authenticate(read_token) is not None
    assert await limited.authenticate(write_token) is None
    another_client = AuthorizedDeviceMCP(
        authority, engine, owner="owner", device="device", client="other"
    )
    assert await another_client.authenticate(read_token) is None
    backend = AuthorizedDeviceMCP(
        authority, None if shared else engine,
        agent_directory=shared_directory if shared else None, owner="owner", device="device",
    )
    adapter = HTTPMCP(backend.authenticate, backend.session)
    port = await adapter.start()
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1"},
        },
    }
    try:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}",
            headers={
                "Authorization": f"Bearer {read_token}",
                "Accept": "application/json, text/event-stream",
            },
        ) as http:
            denied = await http.post(
                "/mcp",
                json=initialize,
                headers={
                    "Authorization": f"Bearer {wrong_device_token}",
                },
            )
            assert denied.status_code == 401
            started = await http.post("/mcp", json=initialize)
            read_session = started.headers["mcp-session-id"]
            http.headers["MCP-Session-Id"] = read_session
            await http.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"})
            catalog = (
                await http.post(
                    "/mcp",
                    json={
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/list",
                    },
                )
            ).json()
            assert {tool["name"] for tool in catalog["result"]["tools"]} == {
                "computer_status",
                "files_read",
                "operations_get",
            }
            if shared:
                local = await exchange(shared_directory, "__status")
                remote = (await http.post("/mcp", json={
                    "jsonrpc": "2.0", "id": "same-engine", "method": "tools/call",
                    "params": {"name": "computer_status", "arguments": {}},
                })).json()["result"]["structuredContent"]
                assert remote["data"]["instance_id"] == local.data["instance_id"]
                assert remote["data"]["transport"] == "http"
            target = tmp_path / "scope.txt"
            write = {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "files_write",
                    "arguments": {"path": str(target), "text": "approved"},
                },
            }
            denied = (await http.post("/mcp", json=write)).json()
            assert denied["error"]["code"] == -32602 and not target.exists()
            binary_denied = (
                await http.post(
                    "/mcp",
                    json={
                        "jsonrpc": "2.0",
                        "id": "binary",
                        "method": "tools/call",
                        "params": {
                            "name": "files_write_binary",
                            "arguments": {"path": str(target), "data_base64": "AA=="},
                        },
                    },
                )
            ).json()
            assert binary_denied["error"]["code"] == -32602 and not target.exists()
            http.headers.pop("MCP-Session-Id")
            http.headers["Authorization"] = f"Bearer {write_token}"
            started = await http.post("/mcp", json=initialize)
            write_session = started.headers["mcp-session-id"]
            http.headers["MCP-Session-Id"] = write_session
            await http.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"})
            written = (await http.post("/mcp", json=write)).json()["result"]["structuredContent"]
            assert written["state"] == "completed" and target.read_text() == "approved"
            # Another grant cannot inspect the writer's operation, even for the same owner.
            http.headers["Authorization"] = f"Bearer {read_token}"
            http.headers["MCP-Session-Id"] = read_session
            lookup = (
                await http.post(
                    "/mcp",
                    json={
                        "jsonrpc": "2.0",
                        "id": 4,
                        "method": "tools/call",
                        "params": {
                            "name": "operations_get",
                            "arguments": {"operation_id": written["operation_id"]},
                        },
                    },
                )
            ).json()["result"]["structuredContent"]
            assert lookup["state"] == "failed"
            read_grant = authority.verify(read_token, resource=resource)
            authority.revoke(owner="owner", grant=read_grant.grant_id)
            assert (await http.post("/mcp", json=write)).status_code == 401
            http.headers["Authorization"] = f"Bearer {write_token}"
            http.headers["MCP-Session-Id"] = write_session
            authority.revoke_device(owner="owner", device="device")
            assert (await http.post("/mcp", json=write)).status_code == 401
    finally:
        await adapter.close()
        authority.close()
        await engine.close()
        if service is not None:
            stopped.set()
            await asyncio.wait_for(service, 10)
