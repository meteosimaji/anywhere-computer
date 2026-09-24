import asyncio
from contextlib import asynccontextmanager

import pytest

from anywhere_computer.authorization import GrantIdentity
from anywhere_computer.authorized_http import AuthorizedDeviceMCP
from anywhere_computer.http_service import HTTPServiceConfig
from anywhere_computer.models import Reply, Request
from anywhere_computer.state import Ledger
from anywhere_computer.subchat import Subchats
from anywhere_computer.subchat_gateway import (
    SUBCHAT_GATEWAY_TOOLS,
    LazySubchatGateway,
    SubchatGateway,
    SubchatGatewayConfig,
    _background_account_session,
    subchat_ledger_owner,
)
from anywhere_computer.subchat_mcp import session
from anywhere_computer.subchat_state import SubchatSubmissions

RESOURCE = "https://computer.example/mcp"


@pytest.mark.asyncio
async def test_lazy_gateway_discovery_is_static_and_execute_retries_login(monkeypatch, tmp_path):
    from types import SimpleNamespace

    import anywhere_computer.subchat_gateway as gateway_module

    clock = [100.0]
    monkeypatch.setattr(gateway_module, "time",
                        SimpleNamespace(monotonic=lambda: clock[0]))

    selected = SubchatGatewayConfig(
        profile=str(tmp_path / "selected" / "Default"),
        ledger=str(tmp_path / "selected" / "ledger"), account_id="account",
        consent="ordinary-chat-browser-control-approved")
    ready = False
    entered = 0
    exited = 0

    class Core:
        def has_live_work(self):
            return False

        async def catalog(self, grant_id, granted):
            return [{"name": "subchat_status", "inputSchema": {"type": "object"}}]

        async def execute(self, grant_id, request, granted):
            return Reply(operation_id=request.operation_id, state="completed")

    @asynccontextmanager
    async def open_gateway(config, *, owner):
        nonlocal entered, exited
        assert config is selected and owner == "owner"
        entered += 1
        if not ready:
            raise ConnectionError("Chrome login unavailable")
        try:
            yield Core()
        finally:
            exited += 1

    monkeypatch.setattr(gateway_module, "open_subchat_gateway", open_gateway)
    gateway = LazySubchatGateway(selected, owner="owner")
    granted = frozenset({"subchat_recover"})
    assert await gateway.catalog("grant", frozenset()) == []
    assert entered == 0
    assert [item["name"] for item in await gateway.catalog("grant", granted)] == [
        "subchat_recover"]
    assert entered == 0
    request = Request(operation_id="a" * 32, tool="subchat_recover")
    denied = await gateway.execute("grant", request, frozenset())
    assert denied.state == "failed" and denied.error == "Subchat tool is not granted"
    assert entered == 0
    unknown = await gateway.execute("grant", request.model_copy(
        update={"tool": "unknown_tool"}), granted)
    assert unknown.state == "failed" and entered == 0
    assert (await gateway.execute("grant", request, granted)).state == "failed"
    assert entered == 1
    ready = True
    assert (await gateway.execute("grant", request, granted)).state == "failed"
    assert entered == 1
    clock[0] += 11
    replies = await asyncio.gather(*(gateway.execute("grant", Request(
        operation_id=f"{index:032x}", tool="subchat_recover"), granted)
        for index in range(8)))
    assert all(reply.state == "completed" for reply in replies)
    assert entered == 2
    await gateway.close()
    await gateway.close()
    assert exited == 1


@pytest.mark.asyncio
async def test_lazy_gateway_idle_closes_and_reopens_without_closing_active_calls(
    monkeypatch, tmp_path,
):
    import anywhere_computer.subchat_gateway as gateway_module

    selected = SubchatGatewayConfig(
        profile=str(tmp_path / "selected" / "Default"),
        ledger=str(tmp_path / "selected" / "ledger"), account_id="account",
        consent="ordinary-chat-browser-control-approved")
    entered = 0
    exited = 0
    execute_started = asyncio.Event()
    finish_execute = asyncio.Event()

    class Core:
        def has_live_work(self):
            return False

        async def execute(self, grant_id, request, granted):
            execute_started.set()
            await finish_execute.wait()
            return Reply(operation_id=request.operation_id, state="completed")

    @asynccontextmanager
    async def open_gateway(config, *, owner):
        nonlocal entered, exited
        assert config is selected and owner == "owner"
        entered += 1
        try:
            yield Core()
        finally:
            exited += 1

    monkeypatch.setattr(gateway_module, "open_subchat_gateway", open_gateway)
    gateway = LazySubchatGateway(selected, owner="owner", idle_close_seconds=.01)
    granted = frozenset({"subchat_recover"})
    try:
        execute = asyncio.create_task(gateway.execute(
            "grant", Request(operation_id="a" * 32, tool="subchat_recover"), granted))
        await execute_started.wait()
        await asyncio.sleep(.03)
        assert entered == 1 and exited == 0
        assert (await gateway.catalog("grant", granted))[0]["name"] == "subchat_recover"
        assert exited == 0
        finish_execute.set()
        assert (await execute).state == "completed"
        await asyncio.wait_for(_until(lambda: exited == 1), timeout=1)
        assert (await gateway.catalog("grant", granted))[0]["name"] == "subchat_recover"
        assert entered == 1  # Catalog alone never reopens Chrome.
        assert (await gateway.execute("grant", Request(
            operation_id="b" * 32, tool="subchat_recover"), granted)).state == "completed"
        assert entered == 2
    finally:
        finish_execute.set()
        await gateway.close()


@pytest.mark.asyncio
async def test_lazy_gateway_idle_waits_for_detached_work_and_service_close(monkeypatch, tmp_path):
    import anywhere_computer.subchat_gateway as gateway_module

    selected = SubchatGatewayConfig(
        profile=str(tmp_path / "selected" / "Default"),
        ledger=str(tmp_path / "selected" / "ledger"), account_id="account",
        consent="ordinary-chat-browser-control-approved")
    work = asyncio.Event()
    exited = 0

    class Core:
        def __init__(self):
            self.calls = {}
            self.recoveries = {}
            self.queue_watches = {}

        async def catalog(self):
            return [{"name": "subchat_status"}]

        async def execute(self, request):
            if work.is_set():
                return Reply(operation_id=request.operation_id, state="completed")
            async def recover():
                await work.wait()

            task = asyncio.create_task(recover())
            self.recoveries[request.operation_id] = task
            task.add_done_callback(lambda _: self.recoveries.pop(request.operation_id))
            return Reply(operation_id=request.operation_id, state="running")

        async def close(self):
            assert not self.recoveries

    @asynccontextmanager
    async def open_gateway(config, *, owner):
        nonlocal exited
        actual = SubchatGateway(lambda _grant: Core(), owner=owner)
        try:
            yield actual
        finally:
            await actual.close()
            exited += 1

    monkeypatch.setattr(gateway_module, "open_subchat_gateway", open_gateway)
    gateway = LazySubchatGateway(selected, owner="owner", idle_close_seconds=.01)
    granted = frozenset({"subchat_recover"})
    pending = await gateway.execute(
        "grant", Request(operation_id="b" * 32, tool="subchat_recover"), granted)
    assert pending.state == "running"
    await asyncio.sleep(.04)
    assert exited == 0
    work.set()
    await asyncio.wait_for(_until(lambda: exited == 1), timeout=1)
    again = await gateway.execute(
        "grant", Request(operation_id="c" * 32, tool="subchat_recover"), granted)
    assert again.state == "completed"
    await gateway.close()
    assert exited == 2
    await asyncio.sleep(.03)
    assert exited == 2
    assert await gateway.catalog("grant", granted) == []


@pytest.mark.asyncio
async def test_lazy_gateway_keeps_generation_running_after_send_ack(monkeypatch, tmp_path):
    import anywhere_computer.subchat_gateway as gateway_module

    selected = SubchatGatewayConfig(
        profile=str(tmp_path / "profile"), ledger=str(tmp_path / "ledger"),
        account_id="account", consent="ordinary-chat-browser-control-approved")
    finish = asyncio.Event()
    closed = 0

    class Core:
        def __init__(self):
            self.calls = {}
            self.recoveries = {}
            self.queue_watches = {}
            self.sends = {}

        async def execute(self, request):
            async def generate():
                await finish.wait()

            self.sends[request.operation_id] = asyncio.create_task(generate())
            return Reply(operation_id=request.operation_id, state="running")

        async def close(self):
            assert all(task.done() for task in self.sends.values())

    @asynccontextmanager
    async def open_gateway(config, *, owner):
        nonlocal closed
        actual = SubchatGateway(lambda _grant: Core(), owner=owner)
        try:
            yield actual
        finally:
            await actual.close()
            closed += 1

    monkeypatch.setattr(gateway_module, "open_subchat_gateway", open_gateway)
    gateway = LazySubchatGateway(selected, owner="owner", idle_close_seconds=.01)
    try:
        response = await gateway.execute(
            "grant", Request(operation_id="a" * 32, tool="subchat_send"),
            frozenset({"subchat_send"}))
        assert response.state == "running"
        await asyncio.sleep(.04)
        assert closed == 0
        finish.set()
        await asyncio.wait_for(_until(lambda: closed == 1), timeout=1)
    finally:
        finish.set()
        await gateway.close()


@pytest.mark.asyncio
async def test_idle_keeps_completed_recovery_failure_until_explicit_observation(
    monkeypatch, tmp_path,
):
    import anywhere_computer.subchat_gateway as gateway_module

    selected = SubchatGatewayConfig(
        profile=str(tmp_path / "selected" / "Default"),
        ledger=str(tmp_path / "selected" / "ledger"), account_id="account",
        consent="ordinary-chat-browser-control-approved")
    finish_recovery = asyncio.Event()
    opened = 0
    closed = 0
    sends = 0
    operation_id = "e" * 32

    class Core:
        def __init__(self):
            self.calls = {}
            self.recoveries = {}
            self.queue_watches = {}

        async def execute(self, request):
            nonlocal sends
            if request.tool == "subchat_send":
                sends += 1

                async def recovery():
                    await finish_recovery.wait()
                    raise ConnectionError("late recovery failure")

                task = asyncio.create_task(recovery())
                self.recoveries[operation_id] = task
                task.add_done_callback(lambda done: done.exception())
                return Reply(operation_id=request.operation_id, state="running")
            task = self.recoveries[operation_id]
            try:
                await task
            except ConnectionError:
                self.recoveries.pop(operation_id)
                return Reply(operation_id=request.operation_id, state="failed",
                             data={"error_type": "ConnectionError"})
            raise AssertionError("Recovery unexpectedly succeeded")

        async def close(self):
            assert not self.recoveries

    @asynccontextmanager
    async def open_gateway(config, *, owner):
        nonlocal opened, closed
        opened += 1
        actual = SubchatGateway(lambda _grant: Core(), owner=owner)
        try:
            yield actual
        finally:
            await actual.close()
            closed += 1

    monkeypatch.setattr(gateway_module, "open_subchat_gateway", open_gateway)
    gateway = LazySubchatGateway(selected, owner="owner", idle_close_seconds=.01)
    granted = frozenset({"subchat_send", "subchat_recover"})
    try:
        sent = await gateway.execute("grant", Request(
            operation_id=operation_id, tool="subchat_send"), granted)
        assert sent.state == "running" and sends == 1
        finish_recovery.set()
        await asyncio.sleep(.04)
        assert opened == 1 and closed == 0
        observed = await gateway.execute("grant", Request(
            operation_id="f" * 32, tool="subchat_recover",
            arguments={"operation_id": operation_id}), granted)
        assert observed.data["error_type"] == "ConnectionError"
        assert sends == 1 and opened == 1
        await asyncio.wait_for(_until(lambda: closed == 1), timeout=1)
    finally:
        finish_recovery.set()
        await gateway.close()


@pytest.mark.asyncio
async def test_lazy_gateway_service_close_waits_for_active_execute(monkeypatch, tmp_path):
    import anywhere_computer.subchat_gateway as gateway_module

    selected = SubchatGatewayConfig(
        profile=str(tmp_path / "selected" / "Default"),
        ledger=str(tmp_path / "selected" / "ledger"), account_id="account",
        consent="ordinary-chat-browser-control-approved")
    execute_started = asyncio.Event()
    finish_execute = asyncio.Event()
    exited = 0

    class Core:
        async def execute(self, grant_id, request, granted):
            execute_started.set()
            await finish_execute.wait()
            return Reply(operation_id=request.operation_id, state="completed")

    @asynccontextmanager
    async def open_gateway(config, *, owner):
        nonlocal exited
        try:
            yield Core()
        finally:
            exited += 1

    monkeypatch.setattr(gateway_module, "open_subchat_gateway", open_gateway)
    gateway = LazySubchatGateway(selected, owner="owner", idle_close_seconds=.01)
    granted = frozenset({"subchat_recover"})
    execute = asyncio.create_task(gateway.execute(
        "grant", Request(operation_id="d" * 32, tool="subchat_recover"), granted))
    await execute_started.wait()
    closing = asyncio.create_task(gateway.close())
    await asyncio.sleep(.03)
    assert not closing.done() and exited == 0
    assert await gateway.catalog("grant", granted) == []
    finish_execute.set()
    assert (await execute).state == "completed"
    await closing
    assert exited == 1


async def _until(predicate):
    while not predicate():
        await asyncio.sleep(.005)


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


def test_subchat_scopes_require_explicit_selection(tmp_path):
    options = dict(resource=RESOURCE, owner="owner", device="a" * 32,
                   client="native", port=12345,
                   scopes=frozenset({"subchat_send"}),
                   redirects=frozenset({"https://client.example/callback"}))
    with pytest.raises(ValueError, match="explicit gateway selection"):
        HTTPServiceConfig(**options)
    selected = SubchatGatewayConfig(
        profile=str(tmp_path / "selected" / "Default"),
        ledger=str(tmp_path / "selected" / "ledger"), account_id="account",
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
async def test_static_discovery_matches_real_gateway_catalog_without_chrome(tmp_path, monkeypatch):
    import anywhere_computer.subchat_gateway as gateway_module

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
        granted = SUBCHAT_GATEWAY_TOOLS
        actual = await gateway.catalog("grant-a", granted)
        async def forbidden_open(config, *, owner):
            raise AssertionError("Discovery opened Chrome")

        monkeypatch.setattr(gateway_module, "open_subchat_gateway", forbidden_open)
        selected = SubchatGatewayConfig(
            profile=str(tmp_path / "selected" / "Default"),
        ledger=str(tmp_path / "selected" / "ledger"), account_id="account",
            consent="ordinary-chat-browser-control-approved")
        lazy = LazySubchatGateway(selected, owner="owner")
        assert await lazy.catalog("grant-a", granted) == actual
        assert await lazy.catalog("grant-a", frozenset({"subchat_catalog"})) == [
            tool for tool in actual if tool["name"] == "subchat_catalog"]
        assert {tool["name"] for tool in actual} == granted
        await lazy.close()
        await gateway.close()
    finally:
        ledger.close()


@pytest.mark.asyncio
async def test_local_status_and_capabilities_match_session_without_opening_chrome(
    tmp_path, monkeypatch,
):
    import anywhere_computer.subchat_gateway as gateway_module
    from anywhere_computer.subchat_browser.backend import BrowserSubchatBackend

    ledger_path = tmp_path / "ledger"
    ledger = Ledger(ledger_path)
    store = SubchatSubmissions(ledger.connection)
    operation_id = "9" * 32
    store.prepare(operation_id, "saved prompt", "model", "effort", owner="grant-a")
    store.record_http_event(operation_id, "prepare_request", owner="grant-a")
    bound_id = "8" * 32
    store.prepare(bound_id, "bound prompt", "model", "effort", owner="grant-a")
    store.begin_send(bound_id, owner="grant-a", user_message_id="user-bound",
                     provider_account_id="account")
    foreign_id = "7" * 32
    store.prepare(foreign_id, "foreign prompt", "model", "effort", owner="grant-a")
    store.begin_send(foreign_id, owner="grant-a", user_message_id="user-foreign",
                     provider_account_id="another-account")

    async def forbidden_browser():
        raise AssertionError("Browser was opened")

    backend = BrowserSubchatBackend(forbidden_browser, http_read=True,
                                    httpx_generation=True, background_pages=True)
    real = session(Subchats(store, backend), owner="grant-a")
    direct = SubchatGateway(lambda _grant: real, owner="owner")

    @asynccontextmanager
    async def forbidden_gateway(config, *, owner):
        raise AssertionError("Gateway was opened")
        yield  # pragma: no cover

    monkeypatch.setattr(gateway_module, "open_subchat_gateway", forbidden_gateway)
    selected = SubchatGatewayConfig(
        profile=str(tmp_path / "selected" / "Default"), ledger=str(ledger_path),
        account_id="account",
        consent="ordinary-chat-browser-control-approved")
    lazy = LazySubchatGateway(selected, owner="owner")
    try:
        for index, (tool, arguments) in enumerate((
            ("subchat_status", {"operation_id": operation_id}),
            ("subchat_status", {"operation_id": bound_id}),
            ("subchat_capabilities", {}),
        )):
            request = Request(operation_id=f"{index + 10:032x}", tool=tool,
                              arguments=arguments)
            actual = await lazy.execute("grant-a", request, frozenset({tool}))
            expected = await direct.execute("grant-a", request, frozenset({tool}))
            assert actual == expected
        private = await lazy.execute("grant-b", Request(
            operation_id="b" * 32, tool="subchat_status",
            arguments={"operation_id": operation_id}), frozenset({"subchat_status"}))
        assert private.state == "failed" and private.data["error_code"] == "unknown_operation"
        mismatch = await lazy.execute("grant-a", Request(
            operation_id="d" * 32, tool="subchat_status",
            arguments={"operation_id": foreign_id}), frozenset({"subchat_status"}))
        assert mismatch.state == "failed" and mismatch.data["error_code"] == "account_mismatch"
        denied = await lazy.execute("grant-a", Request(
            operation_id="c" * 32, tool="subchat_status",
            arguments={"operation_id": operation_id}), frozenset())
        assert denied.state == "failed" and denied.error == "Subchat tool is not granted"
    finally:
        await lazy.close()
        await direct.close()
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
async def test_reconsent_reuses_subchat_ledger_without_cross_client_or_account_access(
    tmp_path, monkeypatch,
):
    import anywhere_computer.subchat_gateway as gateway_module

    grants = {
        grant_id: GrantIdentity(grant_id=grant_id, owner="owner", device="device",
                                client=client, resource=RESOURCE,
                                tools=frozenset({"subchat_status", "subchat_recover"}))
        for grant_id, client in (("old-grant", "client"), ("new-grant", "client"),
                                 ("other-client", "other"))
    }

    class Store:
        def current_grant(self, identity):
            return grants.get(identity)

    class Engine:
        def catalog(self, granted):
            return []

    ledger_path = tmp_path / "ledger"
    ledger = Ledger(ledger_path)
    operation_id = "a" * 32
    owner = subchat_ledger_owner(grants["old-grant"], "account")
    assert owner == subchat_ledger_owner(grants["new-grant"], "account")
    assert owner != subchat_ledger_owner(grants["other-client"], "account")
    assert owner != subchat_ledger_owner(grants["new-grant"], "another-account")
    store = SubchatSubmissions(ledger.connection)
    store.prepare(operation_id, "prompt", "model", "effort", owner=owner,
                  conversation_id="conversation")
    store.begin_send(operation_id, owner=owner, conversation_id="conversation",
                     user_message_id="message", provider_account_id="account")
    legacy_id = "e" * 32
    store.prepare(legacy_id, "legacy", "model", "effort", owner="old-grant")
    store.begin_send(legacy_id, owner="old-grant", user_message_id="legacy-message",
                     provider_account_id="account")
    wrong_account_id = "f" * 32
    store.prepare(wrong_account_id, "other", "model", "effort", owner="old-grant")
    store.begin_send(wrong_account_id, owner="old-grant", user_message_id="other-message",
                     provider_account_id="another-account")
    gateway = LazySubchatGateway(SubchatGatewayConfig(
        profile=str(tmp_path / "profile"), ledger=str(ledger_path), account_id="account",
        consent="ordinary-chat-browser-control-approved"), owner="owner")
    for tool, operation, arguments in (
        ("subchat_send", legacy_id, {}),
        ("subchat_message", "6" * 32, {"target_operation_id": legacy_id}),
        ("subchat_wait", "7" * 32, {"operation_id": legacy_id}),
        ("subchat_recover", "8" * 32, {"operation_id": legacy_id}),
        ("subchat_status", "9" * 32, {"operation_id": legacy_id}),
    ):
        assert gateway.owner_for_request(Request(
            operation_id=operation, tool=tool, arguments=arguments),
            stable_owner=owner, legacy_grant_id="old-grant") == "old-grant"
    backend = AuthorizedDeviceMCP(Store(), Engine(), owner="owner", device="device",
                                  subchat_gateway=gateway)

    class Core:
        async def execute(self, request):
            return Reply(operation_id=request.operation_id, state="completed",
                         data={"operation_id": request.arguments["operation_id"]})

        async def close(self):
            pass

    core_owners = []

    def core_factory(actual_owner):
        core_owners.append(actual_owner)
        return Core()

    @asynccontextmanager
    async def open_gateway(config, *, owner):
        actual = SubchatGateway(core_factory, owner=owner,
                                account_id=config.account_id)
        try:
            yield actual
        finally:
            await actual.close()

    monkeypatch.setattr(gateway_module, "open_subchat_gateway", open_gateway)

    async def status(grant_id):
        return await backend.session(grant_id).execute(Request(
            operation_id="b" * 32, tool="subchat_status",
            arguments={"operation_id": operation_id}))

    try:
        assert (await status("old-grant")).data["state"] == "sending"
        legacy_status = await backend.session("old-grant").execute(Request(
            operation_id="1" * 32, tool="subchat_status",
            arguments={"operation_id": legacy_id}))
        assert legacy_status.data["state"] == "sending"
        wrong_account = await backend.session("old-grant").execute(Request(
            operation_id="2" * 32, tool="subchat_status",
            arguments={"operation_id": wrong_account_id}))
        assert wrong_account.data["error_code"] == "unknown_operation"
        recovered = await backend.session("old-grant").execute(Request(
            operation_id="3" * 32, tool="subchat_recover",
            arguments={"operation_id": legacy_id}))
        assert recovered.data["operation_id"] == legacy_id
        assert core_owners == ["old-grant"]
        del grants["old-grant"]
        assert (await status("new-grant")).data["state"] == "sending"
        legacy_from_new = await backend.session("new-grant").execute(Request(
            operation_id="4" * 32, tool="subchat_status",
            arguments={"operation_id": legacy_id}))
        assert legacy_from_new.data["error_code"] == "unknown_operation"
        private = await status("other-client")
        assert private.state == "failed" and private.data["error_code"] == "unknown_operation"
        private_legacy = await backend.session("other-client").execute(Request(
            operation_id="5" * 32, tool="subchat_status",
            arguments={"operation_id": legacy_id}))
        assert private_legacy.data["error_code"] == "unknown_operation"
        # A second submission for the same conversation still sees the first
        # grant's in-flight send after re-consent.
        next_id = "c" * 32
        store.prepare(next_id, "next", "model", "effort", owner=(
            subchat_ledger_owner(grants["new-grant"], "account")),
            conversation_id="conversation")
        from anywhere_computer.subchat_state import SubchatConcurrentSend

        with pytest.raises(SubchatConcurrentSend):
            store.begin_send(next_id, owner=owner, conversation_id="conversation")
        different_account_gateway = LazySubchatGateway(SubchatGatewayConfig(
            profile=str(tmp_path / "profile"), ledger=str(ledger_path),
            account_id="another-account",
            consent="ordinary-chat-browser-control-approved"), owner="owner")
        other_backend = AuthorizedDeviceMCP(Store(), Engine(), owner="owner", device="device",
                                            subchat_gateway=different_account_gateway)
        another_account = await other_backend.session("new-grant").execute(Request(
            operation_id="d" * 32, tool="subchat_status",
            arguments={"operation_id": operation_id}))
        assert another_account.state == "failed"
        assert another_account.data["error_code"] == "unknown_operation"
        await different_account_gateway.close()
    finally:
        await gateway.close()
        ledger.close()


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
