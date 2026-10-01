"""A new delegated HTTP client must preserve the older strict loopback contract."""

import asyncio
import time
import uuid
from types import SimpleNamespace

import httpx
import pytest
from pydantic import Field

from anywhere_computer import authorized_http, connection, delegated_tasks
from anywhere_computer.authorization import AuthorizationStore, pkce_s256
from anywhere_computer.authorized_http import AuthorizedDeviceMCP
from anywhere_computer.delegated_tasks import DelegatedTaskGrant, DelegatedTaskStore
from anywhere_computer.engine import Engine
from anywhere_computer.http_mcp import HTTPMCP
from anywhere_computer.models import MAX_TOOL_SCOPES, Contract, Request

RECEIPT_FEATURE = "remote_receipt_metadata"


class LegacyGrantedRequest(Contract):
    """The beta.48 wire schema: extension fields remain forbidden."""

    identity: str = Field(min_length=1, max_length=128, pattern=r"^[\x21-\x7e]+$")
    tools: list[str] = Field(max_length=MAX_TOOL_SCOPES)
    authorization_database: str | None = Field(default=None, max_length=4096)
    request: Request

    @property
    def receipt_metadata(self):
        return False


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("interruption", [None, "child_revoke", "parent_revoke", "expiry"])
async def test_delegated_http_reuses_engine_and_negotiates_bound_receipts(
    tmp_path, monkeypatch, legacy, interruption,
):
    status_function = Engine.status
    if legacy:
        monkeypatch.setattr(connection.GrantedRequest, "model_validate", classmethod(
            lambda cls, payload: LegacyGrantedRequest.model_validate(payload)))

        def old_status(self, *args, **kwargs):
            data = status_function(self, *args, **kwargs)
            data.pop("engine_protocol_features", None)
            return data

        monkeypatch.setattr(Engine, "status", old_status)
    monkeypatch.setattr(connection, "local_credential", lambda *a, **kw: "fixture-only")
    shared = tmp_path / "shared"
    stop, ready = asyncio.Event(), asyncio.Event()
    service = asyncio.create_task(connection.serve(
        shared, credential="fixture-only", shutdown=stop, ready=ready,
    ))
    authority = delegation = backend = adapter = None
    try:
        await asyncio.wait_for(ready.wait(), 5)
        initial = await connection.exchange(shared, "__status")
        tools = frozenset({"files_read", "computer_status", "operations_get"})
        authority = AuthorizationStore(tmp_path / "authority",
            resource="https://fixture.example/mcp", known_tools=tools)
        authority.register_client("chat", frozenset({"https://chat.example/callback"}))
        authority.enroll_device("owner", "gateway", tools)
        code = authority.approve(owner="owner", device="gateway", client="chat",
            redirect="https://chat.example/callback", resource=authority.resource,
            tools=tools, challenge=pkce_s256("v" * 43))
        parent_token = authority.exchange_code(code=code, verifier="v" * 43, client="chat",
            redirect="https://chat.example/callback", resource=authority.resource).value
        delegation = DelegatedTaskStore(tmp_path / "delegation", authority)
        backend = AuthorizedDeviceMCP(authority, agent_directory=shared,
            owner="owner", device="gateway", delegated_tasks=delegation)
        parent_id = await backend.authenticate(parent_token)
        assert parent_id is not None
        source = tmp_path / "source.txt"
        source.write_text("child read\n", encoding="utf-8")
        forbidden = tmp_path / "forbidden.txt"
        forbidden.write_text("not granted", encoding="utf-8")
        child_id = uuid.uuid4().hex
        clock = time.time()
        monkeypatch.setattr(delegated_tasks, "time", SimpleNamespace(time=lambda: clock))
        token = delegation.issue(DelegatedTaskGrant(owner="owner", child_id=child_id,
            parent_grant_id=parent_id, device_id="local", tools=tools,
            read_files=(str(source.resolve()),), expires_at=clock + 600))
        adapter = HTTPMCP(backend.authenticate, backend.session)
        port = await adapter.start()
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", headers={
            "Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream",
        }) as http:
            started = await http.post("/mcp", json={"jsonrpc": "2.0", "id": 1,
                "method": "initialize", "params": {"protocolVersion": "2025-11-25",
                    "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}})
            http.headers["MCP-Session-Id"] = started.headers["mcp-session-id"]
            await http.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"})
            listed = (await http.post("/mcp", json={"jsonrpc": "2.0", "id": 2,
                                                     "method": "tools/list"})).json()
            assert {item["name"] for item in listed["result"]["tools"]} == (
                tools | {"delegated_identity"})

            async def call(name, arguments):
                operation_id = uuid.uuid4().hex
                response = (await http.post("/mcp", json={"jsonrpc": "2.0", "id": operation_id,
                    "method": "tools/call", "params": {"name": name,
                        "arguments": {**arguments, "request_id": operation_id}}})).json()
                result = response["result"]["structuredContent"]
                assert result["operation_id"] == operation_id
                return result

            status = await call("computer_status", {})
            assert status["state"] == "completed"
            assert status["data"]["instance_id"] == initial.data["instance_id"]
            read = await call("files_read", {"path": str(source.resolve())})
            assert read["state"] == "completed" and read["data"]["text"] == "child read\n"
            denied = await call("files_read", {"path": str(forbidden.resolve())})
            assert denied["state"] == "failed"
            assert denied["data"]["reason"] == "path_out_of_scope"
            receipt = await call("operations_get", {"operation_id": read["operation_id"]})
            assert receipt["state"] == "completed"
            assert receipt["data"]["operation_id"] == read["operation_id"]
            real_probe = authorized_http.exchange
            real_remote = authorized_http.exchange_remote
            forwarded_lookups = []

            async def probe(directory, tool, *args, **kwargs):
                nonlocal clock
                result = await real_probe(directory, tool, *args, **kwargs)
                if tool == "__status":
                    if interruption == "child_revoke":
                        delegation.revoke(child_id)
                    elif interruption == "parent_revoke":
                        authority.revoke(owner="owner", grant=parent_id)
                    elif interruption == "expiry":
                        clock += 601
                return result

            async def remote(directory, identity, allowed, request, **kwargs):
                if request.tool == "operations_get":
                    forwarded_lookups.append(request.operation_id)
                return await real_remote(directory, identity, allowed, request, **kwargs)

            monkeypatch.setattr(authorized_http, "exchange", probe)
            monkeypatch.setattr(authorized_http, "exchange_remote", remote)
            lookup = await call("operations_get", {"operation_id": status["operation_id"]})
            if interruption:
                assert lookup["state"] == "failed"
                assert lookup["data"]["dispatched"] is False
                assert lookup["data"]["reason"] != "engine_feature_unavailable"
                assert forwarded_lookups == []
                assert delegation.current(child_id) is None
            elif legacy:
                assert lookup["state"] == "failed"
                assert lookup["data"] == {
                    "dispatched": False, "reason": "engine_feature_unavailable",
                    "required_feature": RECEIPT_FEATURE,
                    "next_action": "update_engine", "automatic_retry": False,
                }
                assert "do not resend" in lookup["error"]
                assert forwarded_lookups == []
            else:
                assert lookup["state"] == "completed"
                assert lookup["data"]["operation_id"] == status["operation_id"]
                assert lookup["data"]["recorded_tool"] == "computer_status"
                assert lookup["data"]["request_digest"]
                assert len(forwarded_lookups) == 1
                # A retained client must not reuse a previously successful
                # feature probe after the live declaration changes.
                with monkeypatch.context() as changed:
                    def unavailable_status(self, *args, **kwargs):
                        data = status_function(self, *args, **kwargs)
                        data["engine_protocol_features"] = []
                        return data

                    changed.setattr(Engine, "status", unavailable_status)
                    unavailable = await call("operations_get", {
                        "operation_id": status["operation_id"],
                    })
                    assert unavailable["state"] == "failed"
                    assert unavailable["data"]["reason"] == "engine_feature_unavailable"
                    assert len(forwarded_lookups) == 1
                # Transport failure is not evidence that a legacy fallback is
                # safe. It must not issue the __remote receipt request.
                with monkeypatch.context() as disconnected:
                    async def failed_probe(*args, **kwargs):
                        raise ConnectionError("fixture feature probe disconnected")

                    disconnected.setattr(authorized_http, "exchange", failed_probe)
                    failed_id = uuid.uuid4().hex
                    failed = (await http.post("/mcp", json={"jsonrpc": "2.0", "id": 3,
                        "method": "tools/call", "params": {"name": "operations_get",
                            "arguments": {"operation_id": status["operation_id"],
                                          "request_id": failed_id}}})).json()
                    interrupted = failed["result"]["structuredContent"]
                    assert interrupted["operation_id"] == failed_id
                    assert interrupted["state"] == "unknown"
                    assert "Do not repeat a write" in interrupted["error"]
                    assert len(forwarded_lookups) == 1
            still = await connection.exchange(shared, "__status")
            assert still.data["instance_id"] == initial.data["instance_id"]
            assert not service.done()
    finally:
        if adapter is not None:
            await adapter.close()
        if backend is not None:
            await backend.close()
        if delegation is not None:
            delegation.close()
        if authority is not None:
            authority.close()
        stop.set()
        await asyncio.wait_for(service, 10)
