import asyncio

import pytest

from anywhere_computer.authorization import GrantIdentity
from anywhere_computer.authorized_http import AuthorizedDeviceMCP
from anywhere_computer.http_service import HTTPServiceConfig
from anywhere_computer.models import Reply, Request
from anywhere_computer.state import Ledger
from anywhere_computer.subchat import Subchats
from anywhere_computer.subchat_gateway import (
    SUBCHAT_GATEWAY_TOOLS,
    SubchatGateway,
    SubchatGatewayConfig,
    _background_account_session,
)
from anywhere_computer.subchat_mcp import session
from anywhere_computer.subchat_state import SubchatSubmissions

RESOURCE = "https://computer.example/mcp"


@pytest.mark.asyncio
async def test_gateway_account_checks_never_open_normal_chrome_pages(monkeypatch):
    import anywhere_computer.subchat_browser.background as background
    import anywhere_computer.subchat_chrome_login as login

    class Context:
        async def new_page(self):
            raise AssertionError("normal Chrome tab was opened")

    context = Context()
    page = object()
    created = []

    async def new_page(selected):
        assert selected is context
        created.append("background")
        return page

    async def check(selected, client, *, expected_account_id, page_factory):
        assert selected is context and expected_account_id == "selected-account"
        assert await page_factory() is page

    monkeypatch.setattr(background, "new_background_page", new_page)
    monkeypatch.setattr(login, "chrome_http_session", check)
    for _ in range(2):  # startup and each later account recheck
        await _background_account_session(context, object(), "selected-account")
    assert created == ["background", "background"]


def test_subchat_scopes_require_explicit_selection():
    options = dict(resource=RESOURCE, owner="owner", device="a" * 32,
                   client="native", port=12345,
                   scopes=frozenset({"subchat_send"}),
                   redirects=frozenset({"https://client.example/callback"}))
    with pytest.raises(ValueError, match="explicit gateway selection"):
        HTTPServiceConfig(**options)
    selected = SubchatGatewayConfig(
        profile="/selected/Default", ledger="/selected/ledger", account_id="account",
        consent="ordinary-chat-browser-control-approved")
    assert HTTPServiceConfig(**options, subchat=selected).subchat == selected
    assert HTTPServiceConfig(**{**options, "scopes": frozenset({"files_read"})}).subchat is None


@pytest.mark.asyncio
async def test_gateway_grant_catalog_and_disconnected_worker():
    gate = asyncio.Event()
    calls = []
    created = []

    class Core:
        def __init__(self, owner):
            self.owner = owner
            self.closed = False

        async def catalog(self):
            return [{"name": name, "inputSchema": {"type": "object"},
                     "annotations": {"readOnlyHint": name == "subchat_status",
                                     "openWorldHint": True}}
                    for name in (*SUBCHAT_GATEWAY_TOOLS, "subchat_delete")]

        async def execute(self, request):
            calls.append((self.owner, request))
            await gate.wait()
            return Reply(operation_id=request.operation_id, state="completed",
                         data={"submission_operation_id": request.operation_id})

        async def close(self):
            self.closed = True

    def factory(owner):
        core = Core(owner)
        created.append(core)
        return core

    gateway = SubchatGateway(factory, owner="owner")
    granted = frozenset({"subchat_send", "subchat_status", "subchat_catalog"})
    catalog = await gateway.catalog("grant-a", granted)
    assert {tool["name"] for tool in catalog} == granted
    assert all("inputSchema" in tool and "annotations" in tool for tool in catalog)
    catalog_tool = next(tool for tool in catalog if tool["name"] == "subchat_catalog")
    assert catalog_tool["inputSchema"]["properties"]["source"]["const"] == "http"
    assert catalog_tool["annotations"]["readOnlyHint"] is True
    assert len(created) == 1
    request = Request(operation_id="a" * 32, tool="subchat_send", arguments={"prompt": "hello"})
    disconnected = asyncio.create_task(gateway.execute("grant-a", request, granted))
    await asyncio.sleep(0)
    disconnected.cancel()
    with pytest.raises(asyncio.CancelledError):
        await disconnected
    assert not gateway.pending[("grant-a", request.operation_id)][2].cancelled()
    gate.set()
    answer = await gateway.execute("grant-a", request, granted)
    assert answer.state == "completed" and len(calls) == 1
    assert (await gateway.execute("grant-b", request, granted)).state == "completed"
    assert [owner for owner, _ in calls] == ["grant-a", "grant-b"]
    conflict = await gateway.execute("grant-a", request.model_copy(
        update={"arguments": {"prompt": "different"}}), granted)
    assert conflict.state == "failed" and conflict.data["dispatched"] is False
    denied = await gateway.execute("grant-a", request, frozenset())
    assert denied.state == "failed" and len(calls) == 2
    ui = await gateway.execute("grant-a", Request(operation_id="c" * 32,
        tool="subchat_catalog", arguments={"source": "ui"}), granted)
    assert ui.state == "failed" and ui.data["dispatched"] is False
    observed = await gateway.execute("grant-a", Request(operation_id="d" * 32,
        tool="subchat_catalog", arguments={}), granted)
    assert observed.state == "completed" and calls[-1][1].arguments == {"source": "http"}
    await gateway.close()
    assert all(core.closed for core in created)


@pytest.mark.asyncio
async def test_gateway_preserves_direct_tool_schema_and_annotations(tmp_path):
    class Backend:
        def capabilities(self):
            return {"generation_transport": "browser_prepared_httpx"}

    async def catalog(model):
        return {}

    ledger = Ledger(tmp_path)
    try:
        core = session(Subchats(SubchatSubmissions(ledger.connection), Backend()),
                       observe_catalog=catalog, owner="grant-a")
        gateway = SubchatGateway(lambda grant: core, owner="owner")
        granted = SUBCHAT_GATEWAY_TOOLS - {"subchat_catalog"}
        actual = await gateway.catalog("grant-a", granted)
        expected = [tool for tool in await core.catalog()
                    if tool["name"] in granted]
        assert actual == expected
        assert {tool["name"] for tool in actual} == granted
        await gateway.close()
    finally:
        ledger.close()


@pytest.mark.asyncio
async def test_authorized_view_rechecks_grant_before_subchat_dispatch():
    grant = GrantIdentity(grant_id="grant-a", owner="owner", device="device",
                          client="client", resource=RESOURCE,
                          tools=frozenset({"subchat_status"}))

    class Store:
        def current_grant(self, identity):
            return grant if identity == "grant-a" else None

    class Engine:
        def catalog(self, granted):
            return []

    called = []

    class Core:
        async def catalog(self):
            return [{"name": "subchat_status", "inputSchema": {"type": "object"},
                     "annotations": {"readOnlyHint": True}},
                    {"name": "subchat_send", "inputSchema": {"type": "object"},
                     "annotations": {"readOnlyHint": False}}]

        async def execute(self, request):
            called.append(request.tool)
            return Reply(operation_id=request.operation_id, state="completed")

        async def close(self):
            pass

    gateway = SubchatGateway(lambda owner: Core(), owner="owner")
    backend = AuthorizedDeviceMCP(Store(), Engine(), owner="owner", device="device",
                                  subchat_gateway=gateway)
    view = backend.session("grant-a")
    assert [tool["name"] for tool in await view.catalog()] == ["subchat_status"]
    sent = await view.execute(Request(operation_id="a" * 32, tool="subchat_send"))
    assert sent.state == "failed" and called == []
    status = await view.execute(Request(operation_id="b" * 32, tool="subchat_status"))
    assert status.state == "completed" and called == ["subchat_status"]
    no_id = await view.handle({"jsonrpc": "2.0", "id": "send", "method": "tools/call",
                               "params": {"name": "subchat_send", "arguments": {}}})
    assert no_id["error"]["code"] == -32602 and called == ["subchat_status"]
    grant = None
    with pytest.raises(ValueError, match="authorization is no longer available"):
        await view.catalog()
    with pytest.raises(ValueError, match="authorization is no longer available"):
        await view.execute(Request(operation_id="c" * 32, tool="subchat_status"))
    await gateway.close()


@pytest.mark.asyncio
async def test_gateway_evicts_only_durable_completed_send_receipts():
    class Core:
        async def catalog(self):
            return []

        async def execute(self, request):
            return Reply(operation_id=request.operation_id, state="completed",
                         data={"operation_id": request.operation_id})

        async def close(self):
            pass

    gateway = SubchatGateway(lambda owner: Core(), owner="owner")
    for number in range(129):
        identity = f"{number:032x}"
        result = await gateway.execute("grant-a", Request(
            operation_id=identity, tool="subchat_send", arguments={"prompt": identity}),
            frozenset({"subchat_send"}))
        assert result.state == "completed"
    assert len(gateway.pending) == 128
    assert ("grant-a", "0" * 32) not in gateway.pending
    await gateway.close()


@pytest.mark.asyncio
async def test_failed_preparation_can_retry_exact_id_and_does_not_block_recovery():
    attempts = []

    class Core:
        async def catalog(self):
            return []

        async def execute(self, request):
            attempts.append(request.tool)
            if request.tool == "subchat_send":
                return Reply(operation_id=request.operation_id, state="failed",
                             error="Preparation failed",
                             data={"error_code": "preparation_failed",
                                   "dispatched": False})
            return Reply(operation_id=request.operation_id, state="completed")

        async def close(self):
            pass

    gateway = SubchatGateway(lambda owner: Core(), owner="owner")
    first = Request(operation_id="a" * 32, tool="subchat_send",
                    arguments={"prompt": "same input"})
    for _ in range(2):
        assert (await gateway.execute("grant", first,
                frozenset({"subchat_send"}))).data["error_code"] == "preparation_failed"
    assert attempts == ["subchat_send", "subchat_send"]
    for number in range(129):
        await gateway.execute("grant", Request(operation_id=f"{number:032x}",
            tool="subchat_send", arguments={"prompt": str(number)}),
            frozenset({"subchat_send"}))
    recovered = await gateway.execute("grant", Request(operation_id="b" * 32,
        tool="subchat_recover", arguments={"operation_id": first.operation_id}),
        frozenset({"subchat_recover"}))
    assert recovered.state == "completed"
    assert len(gateway.pending) <= 129
    await gateway.close()


@pytest.mark.asyncio
async def test_uncertain_sends_keep_recovery_available_at_capacity():
    class Core:
        async def catalog(self):
            return []

        async def execute(self, request):
            if request.tool == "subchat_recover":
                return Reply(operation_id=request.operation_id, state="completed",
                             data={"state": "completed"})
            return Reply(operation_id=request.operation_id, state="unknown")

        async def close(self):
            pass

    gateway = SubchatGateway(lambda owner: Core(), owner="owner")
    scopes = frozenset({"subchat_send", "subchat_recover"})
    for number in range(128):
        result = await gateway.execute("grant", Request(operation_id=f"{number:032x}",
            tool="subchat_send", arguments={"prompt": str(number)}), scopes)
        assert result.state == "unknown"
    blocked = await gateway.execute("grant", Request(operation_id="a" * 32,
        tool="subchat_send", arguments={"prompt": "new"}), scopes)
    assert blocked.state == "failed" and blocked.data["dispatched"] is False
    recovered = await gateway.execute("grant", Request(operation_id="b" * 32,
        tool="subchat_recover", arguments={"operation_id": "0" * 32}), scopes)
    assert recovered.state == "completed"
    assert ("grant", "0" * 32) not in gateway.pending
    accepted = await gateway.execute("grant", Request(operation_id="a" * 32,
        tool="subchat_send", arguments={"prompt": "new"}), scopes)
    assert accepted.state == "unknown"
    assert len(gateway.pending) == 128
    await gateway.close()
