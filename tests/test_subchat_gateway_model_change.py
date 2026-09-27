"""Authenticated HTTPS queued model changes preserve account and revision guards."""

import json
from contextlib import asynccontextmanager

import httpx
from test_http_service import initialize
from test_subchat_http_catalog import catalog

from anywhere_computer.authorization import AuthorizationStore, pkce_s256
from anywhere_computer.authorized_http import AuthorizedDeviceMCP
from anywhere_computer.engine import Engine
from anywhere_computer.http_mcp import HTTPMCP
from anywhere_computer.state import Ledger
from anywhere_computer.subchat import Subchats
from anywhere_computer.subchat_browser.catalog import project_http_catalog
from anywhere_computer.subchat_gateway import (
    SUBCHAT_GATEWAY_TOOLS,
    LazySubchatGateway,
    SubchatGateway,
    SubchatGatewayConfig,
    subchat_ledger_owner,
)
from anywhere_computer.subchat_mcp import session
from anywhere_computer.subchat_state import SubchatHTTPSelection, SubchatSubmissions


async def test_http_mcp_queue_model_change_scope_account_and_revision(tmp_path, monkeypatch):
    import anywhere_computer.subchat_gateway as gateway_module

    resource = "https://computer.example/mcp"
    account = "selected-account"
    engine = Engine(tmp_path / "engine")
    authority = AuthorizationStore(
        tmp_path / "auth", resource=resource,
        known_tools=frozenset(engine.tools) | SUBCHAT_GATEWAY_TOOLS)
    redirect = "https://client.example/callback"
    authority.register_client("client", frozenset({redirect}))
    scopes = frozenset({"subchat_queue_model_change", "subchat_status"})
    authority.enroll_device("owner", "device", scopes)

    def grant(tools):
        verifier = "v" * 43
        code = authority.approve(
            owner="owner", device="device", client="client", redirect=redirect,
            resource=resource, tools=frozenset(tools), challenge=pkce_s256(verifier))
        token = authority.exchange_code(
            code=code, verifier=verifier, client="client", redirect=redirect,
            resource=resource).value
        identity = authority.verify(token, resource=resource)
        assert identity is not None
        return token, identity

    token, identity = grant({"subchat_queue_model_change", "subchat_status"})
    status_token, _ = grant({"subchat_status"})
    owner = subchat_ledger_owner(identity, account)
    ledger_path = tmp_path / "ledger"
    ledger = Ledger(ledger_path)
    store = SubchatSubmissions(ledger.connection)
    projected = project_http_catalog(json.dumps(catalog()).encode())
    choice = projected["versions"][0]["choices"][0]
    selection = SubchatHTTPSelection.model_validate(choice["http_selection"])

    class Browser:
        sends = 0

        def validate_send_selection(self, selected):
            assert selected == selection

        async def http_catalog(self):
            return projected

    browser = Browser()

    def queued(parent, child, bound_account):
        store.prepare(parent, "parent", "Future Chat", "Future effort", owner=owner,
                      conversation_id="conversation-" + bound_account,
                      http_selection=selection)
        store.begin_send(parent, owner=owner, user_message_id="user-" + bound_account,
                         provider_account_id=bound_account)
        store.submitted(parent, "conversation-" + bound_account,
                        "user-" + bound_account, owner=owner)
        store.complete(parent, "answer-" + bound_account, "done", owner=owner)
        store.prepare(child, "child", "Future Chat", "Future effort", owner=owner,
                      conversation_id="conversation-" + bound_account,
                      after_operation_id=parent, http_selection=selection)

    child = "b" * 32
    other_child = "d" * 32
    queued("a" * 32, child, account)
    queued("c" * 32, other_child, "another-account")

    @asynccontextmanager
    async def open_gateway(config, *, owner):
        actual = SubchatGateway(
            lambda selected: session(Subchats(store, browser), owner=selected),
            owner=owner, account_id=config.account_id)
        try:
            yield actual
        finally:
            await actual.close()

    monkeypatch.setattr(gateway_module, "open_subchat_gateway", open_gateway)
    gateway = LazySubchatGateway(SubchatGatewayConfig(
        profile=str(tmp_path / "profile"), ledger=str(ledger_path), account_id=account,
        consent="ordinary-chat-browser-control-approved"), owner="owner")
    backend = AuthorizedDeviceMCP(
        authority, engine, owner="owner", device="device", client="client",
        subchat_gateway=gateway)
    adapter = HTTPMCP(backend.authenticate, backend.session)
    port = await adapter.start()
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}",
                                     trust_env=False) as http:
            headers = await initialize(http, token)
            listed = await http.post("/mcp", headers=headers, json={
                "jsonrpc": "2.0", "id": 1, "method": "tools/list"})
            assert {tool["name"] for tool in listed.json()["result"]["tools"]} == scopes

            async def change(request_id, target, revision, *, active_headers=headers):
                response = await http.post("/mcp", headers=active_headers, json={
                    "jsonrpc": "2.0", "id": request_id, "method": "tools/call",
                    "params": {"name": "subchat_queue_model_change", "arguments": {
                        "request_id": f"{request_id:032x}", "operation_id": target,
                        "expected_revision": revision, "choice_id": choice["choice_id"]}}})
                assert response.status_code == 200
                body = response.json()
                return body.get("result", {}).get("structuredContent", body)

            read_headers = await initialize(http, status_token)
            denied = await change(2, child, 0, active_headers=read_headers)
            assert "error" in denied
            assert store.queue_revision(child, owner=owner) == 0
            wrong_account = await change(3, other_child, 0)
            assert wrong_account["data"]["error_code"] == "account_mismatch"
            assert store.queue_revision(other_child, owner=owner) == 0
            changed = await change(4, child, 0)
            assert changed["state"] == "completed"
            assert changed["data"]["queue_revision"] == 1
            stale = await change(5, child, 0)
            assert stale["data"]["error_code"] == "queue_revision_conflict"
            assert store.queue_revision(child, owner=owner) == 1
            choice["available"] = False
            unavailable = await change(6, child, 1)
            assert unavailable["state"] == "failed"
            assert store.queue_revision(child, owner=owner) == 1
            assert store.get(child, owner=owner).state == "queued"
            assert browser.sends == 0
    finally:
        await adapter.close()
        await gateway.close()
        ledger.close()
        authority.close()
        await engine.close()
