import asyncio
import json
import sqlite3
import uuid

import pytest

from anywhere_computer.device_router import DeviceRouter
from anywhere_computer.engine import Engine
from anywhere_computer.http_service import HTTPServiceConfig
from anywhere_computer.mcp_server import MCPSession
from anywhere_computer.models import Request
from anywhere_computer.setup_connector import SetupConnector


def operation(outer_tool: str, **arguments: object) -> Request:
    return Request(operation_id=uuid.uuid4().hex, tool=outer_tool, arguments=arguments)


@pytest.fixture
def connector(tmp_path):
    async def catalog():
        return [{"name": "computer_status"}]

    async def execute(request):
        raise AssertionError(f"setup request leaked to local engine: {request.tool}")

    value = SetupConnector(tmp_path, catalog, execute)
    try:
        yield value
    finally:
        value.close()


async def test_setup_catalog_is_local_only(connector):
    tools = await connector.catalog()
    assert {tool["name"] for tool in tools} == {
        "computer_status", "connection_setup_status", "connection_setup_plan",
        "connection_setup_confirm",
    }


async def test_status_persists_binding_and_receipt_in_one_transaction(connector):
    statements = []
    connector.db.set_trace_callback(statements.append)
    request = operation('connection_setup_status')
    reply = await connector.execute(request)
    assert reply.state == 'completed' and reply.data['phase'] == 'new'
    row = connector.db.execute(
        'SELECT digest, dispatched, reply FROM setup_operations WHERE id=?',
        (request.operation_id,),
    ).fetchone()
    assert row == (connector._digest(request), 1, reply.model_dump_json())
    assert sum(line == 'COMMIT' for line in statements) == 1
    assert not connector.db.in_transaction
    assert await connector.execute(request) == reply


async def test_status_inspects_pending_save_without_waiting_or_repeating_it(
    connector, monkeypatch,
):
    import anywhere_computer.setup_controller as controller_module

    plan = HTTPServiceConfig(
        resource='https://fixture.example/mcp', owner='owner', device='a' * 32,
        client='anywhere-native', port=8768, scopes=frozenset({'computer_status'}),
        redirects=frozenset({'http://127.0.0.1/oauth/callback'}),
    )

    async def make_plan(**_):
        return plan

    entered, release = asyncio.Event(), asyncio.Event()
    original_save = controller_module.save_http_config
    saves = 0

    async def held_save(directory, configuration):
        nonlocal saves
        saves += 1
        entered.set()
        await release.wait()
        await original_save(directory, configuration)

    monkeypatch.setattr('anywhere_computer.setup_connector.plan_remote_setup', make_plan)
    monkeypatch.setattr(controller_module, 'save_http_config', held_save)
    reviewed = await connector.execute(operation(
        'connection_setup_plan', resource=plan.resource))
    confirm = operation('connection_setup_confirm', plan_id=reviewed.data['plan_id'])
    saving = asyncio.create_task(connector.execute(confirm))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        status = await asyncio.wait_for(connector.execute(
            operation('connection_setup_status')), .3)
        assert status.state == 'completed' and status.data['phase'] == 'saving'
        assert not saving.done() and saves == 1
        release.set()
        saved = await saving
        assert saved.data['phase'] == 'configured'
        assert await connector.execute(confirm) == saved
        assert saves == 1
    finally:
        release.set()
        await saving


async def test_failed_status_keeps_exact_request_binding(connector, monkeypatch):
    request = operation('connection_setup_status')

    def unreadable():
        raise ValueError('synthetic private detail')

    monkeypatch.setattr(connector.controller, 'progress', unreadable)
    reply = await connector.execute(request)
    assert reply.state == 'failed' and 'private' not in json.dumps(reply.model_dump())
    changed = await connector.execute(Request(
        operation_id=request.operation_id, tool='computer_status', arguments={}))
    assert changed.state == 'failed'  # Never reaches the forbidden local execute fixture.
    assert connector.db.execute(
        'SELECT digest FROM setup_operations WHERE id=?', (request.operation_id,),
    ).fetchone() == (connector._digest(request),)


async def test_status_commit_failure_never_returns_success(connector):
    def deny_commit(action, first, *_):
        if action == sqlite3.SQLITE_TRANSACTION and first == 'COMMIT':
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    connector.db.set_authorizer(deny_commit)
    request = operation('connection_setup_status')
    reply = await connector.execute(request)
    assert reply.state == 'failed'
    assert not connector.db.in_transaction
    assert connector.db.execute(
        'SELECT id FROM setup_operations WHERE id=?', (request.operation_id,),
    ).fetchone() is None
    connector.db.set_authorizer(None)
    assert (await connector.execute(request)).state == 'completed'


async def test_invalid_status_keeps_binding_without_claiming(connector):
    request = operation('connection_setup_status', unexpected='value')
    assert (await connector.execute(request)).state == 'failed'
    changed = Request(operation_id=request.operation_id, tool='computer_status', arguments={})
    assert (await connector.execute(changed)).state == 'failed'
    assert connector.db.execute(
        'SELECT digest, dispatched FROM setup_operations WHERE id=?', (request.operation_id,),
    ).fetchone() == (connector._digest(request), 0)


async def test_plan_replay_keeps_generated_device_id_after_restart(connector, monkeypatch):
    plan = HTTPServiceConfig(
        resource="https://fixture.example/mcp", owner="owner", device="a" * 32,
        client="anywhere-native", port=8768, scopes=frozenset({"computer_status"}),
        redirects=frozenset({"http://127.0.0.1/oauth/callback"}),
    )
    async def make_plan(**_: object):
        return plan
    monkeypatch.setattr("anywhere_computer.setup_connector.plan_remote_setup", make_plan)
    request = operation("connection_setup_plan", resource="https://fixture.example/mcp")
    first = await connector.execute(request)
    second = await connector.execute(request)
    assert first == second

    connector.close()
    restored = SetupConnector(connector.controller.directory, connector.local_catalog,
                              connector.local_execute)
    try:
        recovered = await restored.execute(request)
        assert recovered.state == "unknown"
        assert "connection_setup_status" in (recovered.error or "")
    finally:
        restored.close()


async def test_canceled_confirm_is_not_dispatched_again(connector, monkeypatch):
    request = operation("connection_setup_confirm", plan_id="b" * 64)
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def confirm(_: str):
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        raise AssertionError("the canceled save should not be retried")

    monkeypatch.setattr(connector.controller, "confirm", confirm)
    pending = asyncio.create_task(connector.execute(request))
    await started.wait()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    release.set()
    replay = await connector.execute(request)
    assert replay.state == "unknown"
    assert calls == 1


async def test_engine_router_setup_catalog_and_workspace_metadata(tmp_path):
    engine = Engine(tmp_path / "engine")

    async def catalog():
        return engine.catalog()

    router = DeviceRouter(tmp_path / "registry", catalog, engine.execute)
    setup = SetupConnector(tmp_path / "registry", router.catalog, router.execute)
    try:
        assert len(engine.catalog()) == 102
        session = MCPSession(setup.catalog, setup.execute)
        await session.handle({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-11-25", "capabilities": {
                "extensions": {"io.modelcontextprotocol/ui": {
                    "mimeTypes": ["text/html;profile=mcp-app"]
                }}
            }, "clientInfo": {"name": "test", "version": "1"}},
        })
        await session.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        result = await session.handle({
            "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {},
        })
        tools = result["result"]["tools"]
        assert len(tools) == 109
        assert {tool["name"] for tool in tools} >= set({
            "connection_setup_status", "connection_setup_plan", "connection_setup_confirm",
        })
        workspace = next(tool for tool in tools if tool["name"] == "workspace_open")
        assert workspace["_meta"]["ui"]["visibility"] == ["model", "app"]
        assert workspace["_meta"]["openai/outputTemplate"].startswith("ui://")
        opened = await session.handle({
            "jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
                "name": "workspace_open", "arguments": {"view": "connection"},
            },
        })
        assert opened["result"]["structuredContent"]["data"]["workspace"]["view"] == "connection"
        assert set(opened["result"]["_meta"]["workspaceTools"]) >= {
            "connection_setup_status", "connection_setup_plan", "connection_setup_confirm",
        }
    finally:
        setup.close()
        router.close()
        await engine.close()


async def test_bare_engine_catalog_has_no_setup_tools(tmp_path):
    engine = Engine(tmp_path)
    try:
        assert len(engine.catalog()) == 102
        assert not {tool["name"] for tool in engine.catalog()} & {
            "connection_setup_status", "connection_setup_plan", "connection_setup_confirm",
        }
    finally:
        await engine.close()


async def test_devices_call_rejects_setup_prefix_before_local_or_remote_forwarding(tmp_path):
    engine = Engine(tmp_path / "engine")
    forwarded = []

    class Backend:
        async def catalog(self):
            raise AssertionError("Setup routing must be rejected before contacting the backend")

        async def execute(self, request):
            forwarded.append(request)
            return await engine.execute(request)

        async def close(self):
            pass

    async def catalog():
        return engine.catalog()

    router = DeviceRouter(
        tmp_path / "registry", catalog, engine.execute,
        backend_factory=lambda _: Backend(),
    )
    remote = router.store.add("remote", "host")["device_id"]
    try:
        for target in ("local", remote):
            for name in ("connection_setup_status", "connection_setup_plan",
                         "connection_setup_confirm", "connection_setup_spoof"):
                reply = await router.execute(operation(
                    "devices_call", device_id=target, tool=name,
                ))
                assert reply.state == "failed"
        assert not forwarded
        assert router._remote_tools([
            {"name": "connection_setup_spoof"}, {"name": "computer_status"},
        ]) == [{"name": "computer_status"}]
    finally:
        router.close()
        await engine.close()


async def test_setup_and_normal_tool_cannot_reuse_operation_id(connector):
    operation_id = uuid.uuid4().hex
    first = await connector.execute(Request(
        operation_id=operation_id, tool="connection_setup_status", arguments={}
    ))
    second = await connector.execute(Request(
        operation_id=operation_id, tool="computer_status", arguments={}
    ))
    assert first.state == "completed"
    assert second.state == "failed"


async def test_superseded_plan_replays_unknown_after_new_plan_and_restart(connector, monkeypatch):
    plan = HTTPServiceConfig(
        resource="https://fixture.example/mcp", owner="owner", device="c" * 32,
        client="anywhere-native", port=8768, scopes=frozenset({"computer_status"}),
        redirects=frozenset({"http://127.0.0.1/oauth/callback"}),
    )
    async def make_plan(**_: object):
        return plan
    monkeypatch.setattr("anywhere_computer.setup_connector.plan_remote_setup", make_plan)
    first = operation("connection_setup_plan", resource="https://one.example/mcp")
    second = operation("connection_setup_plan", resource="https://two.example/mcp")
    assert (await connector.execute(first)).state == "completed"
    assert (await connector.execute(second)).state == "completed"
    assert (await connector.execute(first)).state == "unknown"
    connector.close()
    restored = SetupConnector(connector.controller.directory, connector.local_catalog,
                              connector.local_execute)
    try:
        assert (await restored.execute(first)).state == "unknown"
    finally:
        restored.close()


async def test_two_connectors_share_setup_operation_claim(tmp_path, monkeypatch):
    calls = 0
    plan = HTTPServiceConfig(
        resource="https://fixture.example/mcp", owner="owner", device="d" * 32,
        client="anywhere-native", port=8768, scopes=frozenset({"computer_status"}),
        redirects=frozenset({"http://127.0.0.1/oauth/callback"}),
    )
    async def make_plan(**_: object):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)
        return plan
    monkeypatch.setattr("anywhere_computer.setup_connector.plan_remote_setup", make_plan)
    async def catalog():
        return []
    async def execute(request):
        raise AssertionError(request)
    first = SetupConnector(tmp_path, catalog, execute)
    second = SetupConnector(tmp_path, catalog, execute)
    request_value = operation("connection_setup_plan", resource="https://fixture.example/mcp")
    try:
        replies = await asyncio.gather(first.execute(request_value), second.execute(request_value))
        assert sorted(reply.state for reply in replies) == ["completed", "unknown"]
        assert calls == 1
    finally:
        first.close()
        second.close()


async def test_chatgpt_preset_reviews_and_saves_canonical_oauth_settings(connector):
    from anywhere_computer.http_service import load_http_config
    from anywhere_computer.remote_setup import CHATGPT_CLIENT, CHATGPT_REDIRECT

    reply = await connector.execute(operation(
        "connection_setup_plan", resource="https://fixture.example/mcp",
        client_kind="chatgpt", mode="all",
    ))
    assert reply.state == "completed" and reply.data["phase"] == "review"
    config = reply.data["configuration"]
    assert config["client"] == CHATGPT_CLIENT
    assert config["redirects"] == [CHATGPT_REDIRECT]
    assert "codex_plugin_call" in config["scopes"] and "terminal_start" in config["scopes"]
    assert not (connector.controller.directory / "http-server").exists()
    saved = await connector.execute(operation(
        "connection_setup_confirm", plan_id=reply.data["plan_id"],
    ))
    assert saved.state == "completed" and saved.data["phase"] == "configured"
    assert load_http_config(connector.controller.directory).client == CHATGPT_CLIENT


@pytest.mark.parametrize("conflict", [
    {"client": "anywhere-native"},
    {"redirects": ["https://another.example/callback"]},
    {"client_kind": "unknown"},
])
async def test_chatgpt_preset_rejects_conflicting_fields_before_review(connector, conflict):
    arguments = {"resource": "https://fixture.example/mcp", "client_kind": "chatgpt", **conflict}
    reply = await connector.execute(operation("connection_setup_plan", **arguments))
    assert reply.state == "failed"
    assert connector.controller.progress().phase == "new"
    assert not (connector.controller.directory / "http-server").exists()
