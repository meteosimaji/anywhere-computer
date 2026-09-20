"""Two actual HTTP hops; disposable engines, credentials, files and processes."""

import asyncio
import uuid

import httpx
import pytest
from test_engine import python_command
from test_http_client import http_remote as http_remote
from test_http_service import initialize

from anywhere_computer.authorization import AuthorizationStore, pkce_s256
from anywhere_computer.authorized_http import AuthorizedDeviceMCP
from anywhere_computer.connection import exchange, serve
from anywhere_computer.device_router import ROUTER_TOOLS, DeviceRouter
from anywhere_computer.devices import DeviceStore
from anywhere_computer.engine import Engine
from anywhere_computer.http_client import HTTPBackend
from anywhere_computer.http_mcp import HTTPMCP

TARGET_TOOLS = frozenset({
    "computer_status", "files_read", "files_write", "operations_get",
    "terminal_start", "terminal_input", "terminal_stop",
})


@pytest.mark.parametrize("http_remote", [TARGET_TOOLS], indirect=True)
@pytest.mark.parametrize("shared", [False, True])
async def test_chat_http_routes_and_recovers_without_cross_grant_access(
    tmp_path, monkeypatch, http_remote, shared,
):
    target, target_adapter, _, remote, packets, faults = http_remote
    local = Engine(tmp_path / "gateway")
    shared_directory = tmp_path / "shared-agent"
    stopped = asyncio.Event()
    service = None
    local_instance = local.instance_id
    authority = None
    adapter = None
    try:
        if shared:
            monkeypatch.setattr("anywhere_computer.connection.local_credential",
                                lambda *a, **kw: "routing-fixture-credential")
            service = asyncio.create_task(serve(
                shared_directory, credential="routing-fixture-credential", shutdown=stopped,
            ))
            try:
                async with asyncio.timeout(5):
                    while not (shared_directory / "agent.json").exists():
                        if service.done():
                            await service
                        await asyncio.sleep(0.01)
            except TimeoutError as error:
                frames = [f"{frame.f_code.co_name}:{frame.f_lineno}"
                          for frame in service.get_stack()]
                raise TimeoutError(
                    f"Shared startup exceeded 5 seconds: done={service.done()}, "
                    f"endpoint_exists={(shared_directory / 'agent.json').exists()}, stack={frames}"
                ) from error
            local_instance = (await exchange(shared_directory, "__status")).data["instance_id"]
        registry = tmp_path / "devices"
        devices = DeviceStore(registry)
        remote_id = devices.add_http(
            "Windows fixture", target.tokens.resource, target.tokens.client, "vm",
        )["device_id"]
        devices.close()
        monkeypatch.setattr(DeviceRouter, "_backend", lambda self, device: HTTPBackend(
            target.tokens, wire=target.wire,
        ))
        permissions = ROUTER_TOOLS | {"computer_status", "operations_get"}
        authority = AuthorizationStore(
            tmp_path / "gateway-auth", resource="https://gateway.example/mcp",
            known_tools=frozenset(local.tools) | ROUTER_TOOLS,
        )
        redirect = "https://chat.example/callback"
        authority.register_client("chat", frozenset({redirect}))
        authority.enroll_device("owner", "gateway", permissions)

        def token(tools):
            code = authority.approve(
                owner="owner", device="gateway", client="chat", redirect=redirect,
                resource=authority.resource, tools=frozenset(tools), challenge=pkce_s256("v" * 43),
            )
            return authority.exchange_code(
                code=code, verifier="v" * 43, client="chat", redirect=redirect,
                resource=authority.resource,
            ).value

        first, second = token(permissions), token(permissions)
        restricted = token({"computer_status"})
        backend = AuthorizedDeviceMCP(
            authority, None if shared else local, owner="owner", device="gateway", client="chat",
            agent_directory=shared_directory if shared else None,
            device_directory=registry,
        )
        adapter = HTTPMCP(backend.authenticate, backend.session)
        port = await adapter.start()
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=15) as http:
            headers = await initialize(http, first)

            async def call(name, arguments=None, operation=None, expected="completed"):
                response = await http.post("/mcp", headers=headers, json={
                    "jsonrpc": "2.0", "id": uuid.uuid4().hex, "method": "tools/call",
                    "params": {"name": name, "arguments": arguments or {}, "_meta": {
                        "io.github.meteosimaji.anywhere-computer/operation_id":
                            operation or uuid.uuid4().hex,
                    }},
                })
                assert response.status_code == 200, response.text
                reply = response.json()["result"]["structuredContent"]
                assert reply["state"] == expected, reply
                return reply

            async def routed(tool, arguments=None, **options):
                return await call("devices_call", {
                    "device_id": remote_id, "tool": tool, "arguments": arguments or {},
                }, **options)

            listed = await call("devices_list")
            assert [row["device_id"] for row in listed["data"]["devices"]] == ["local", remote_id]
            catalog = await call("devices_tools", {"device_id": remote_id})
            assert {row["name"] for row in catalog["data"]["tools"]} == TARGET_TOOLS
            assert all('request_id' not in row['inputSchema'].get('properties', {})
                       for row in catalog['data']['tools'])
            for name, args in [('computer_status', {}),
                               ('operations_get', {'operation_id': uuid.uuid4().hex})]:
                before = len(packets)
                rejected = await routed(name, {**args, 'request_id': uuid.uuid4().hex},
                                        expected='failed')
                assert 'outer devices_call' in rejected['error']
                assert rejected['data']['dispatched'] is False
                assert len(packets) == before
            here = await call("devices_call", {"device_id": "local", "tool": "computer_status"})
            there = await routed("computer_status")
            assert here["data"]["result"]["instance_id"] == local_instance
            assert there["data"]["result"]["instance_id"] == remote.instance_id
            assert local_instance != remote.instance_id
            # A routing grant must not widen the gateway's ordinary local tool permissions.
            forbidden_path = tmp_path / "must-not-exist"
            await call("devices_call", {"device_id": "local", "tool": "files_write",
                "arguments": {"path": str(forbidden_path), "text": "denied"}}, expected="failed")
            assert not forbidden_path.exists()
            await routed("devices_list", expected="failed")

            original = uuid.uuid4().hex
            path = tmp_path / "日本語.txt"
            faults["lose_write_response"] = True
            await routed("files_write", {"path": str(path), "text": "継続 🚀"},
                         operation=original, expected="unknown")
            sent = len(packets)
            await routed("files_write", {"path": str(path), "text": "継続 🚀"},
                         operation=original, expected="unknown")
            assert not any(packet and packet.get("method") == "tools/call"
                           for packet in packets[sent:])
            await call("devices_call", {"device_id": "local", "tool": "computer_status"},
                       operation=original, expected="failed")
            # New HTTP session, same grant: original external ID still resolves on the target.
            await http.delete("/mcp", headers=headers)
            headers = await initialize(http, first)
            recovered = await routed("operations_get", {"operation_id": original})
            assert recovered["data"]["result"]["operation_id"] == original
            assert recovered["data"]["result"]["state"] == "completed"
            assert path.read_text(encoding="utf-8") == "継続 🚀"

            headers = await initialize(http, second)
            await routed("operations_get", {"operation_id": original}, expected="failed")
            # Same external ID in another grant is an independent operation, not a replay.
            await routed("computer_status", operation=original)
            headers = await initialize(http, first)
            started = await routed("terminal_start", {
                "cwd": str(tmp_path), "command": python_command(
                    "import sys\nfor line in sys.stdin:\n print(int(line)+2, flush=True)\n"
                    " print('READY>', flush=True)\n"),
            })
            session = started["data"]["result"]["session_id"]
            for value in (40, 50):
                reply = await routed("terminal_input", {
                    "session_id": session, "text": str(value) + "\n",
                    "wait_ms": 3000, "wait_for_prompt": "READY>",
                })
                assert str(value + 2) in reply["data"]["result"]["text"]
                assert reply["data"]["result"]["prompt_matched"]
            await routed("terminal_stop", {"session_id": session})
            assert not target_adapter.sessions
            headers = await initialize(http, restricted)
            denied = await http.post("/mcp", headers=headers, json={
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "devices_list", "arguments": {}},
            })
            assert denied.json()["error"]["code"] == -32602
    finally:
        try:
            if adapter is not None:
                await adapter.close()
        finally:
            if authority is not None:
                authority.close()
            try:
                await local.close()
            finally:
                if service is not None:
                    stopped.set()
                    await asyncio.wait_for(service, 10)


async def test_shared_startup_timeout_stops_service(tmp_path, monkeypatch, http_remote):
    cleaned = asyncio.Event()

    async def unpublished_service(directory, *, credential, shutdown):
        await shutdown.wait()
        cleaned.set()

    monkeypatch.setattr(__name__ + ".serve", unpublished_service)
    with pytest.raises(TimeoutError):
        await test_chat_http_routes_and_recovers_without_cross_grant_access(
            tmp_path, monkeypatch, http_remote, True,
        )
    assert cleaned.is_set()
