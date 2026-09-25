"""Bounded plugin lifetimes, owner isolation and uncertain side effects."""

import asyncio
import json
import uuid
from pathlib import Path

import pytest

from anywhere_computer import codex_plugins
from anywhere_computer.codex_plugins import PluginCallOutcomeUnknown, PluginPreflightError
from anywhere_computer.engine import Engine
from anywhere_computer.models import Reply, Request
from anywhere_computer.plugin_sessions import PluginSessions
from anywhere_computer.remote_bridge import RemoteAgent


@pytest.fixture
def contexts(monkeypatch):
    instances = []

    class Context:
        def __init__(self, cwd):
            self.cwd = str(Path(cwd).resolve())
            self.alive = False
            self.value = 0
            self.calls = 0
            self.error = None
            self.gate = None
            self.entered = asyncio.Event()
            instances.append(self)

        async def open(self):
            self.alive = True

        async def close(self):
            self.alive = False

        async def inspect(self, **kwargs):
            return {"cwd": self.cwd, "servers": [{
                "server": "counter", "inspect_arguments": {"cwd": self.cwd},
                "tools": [{"name": "increment", "call_arguments": {
                    "cwd": self.cwd, "server": "counter", "tool": "increment",
                    "catalog_sha256": "1" * 64,
                }}],
            }]}

        async def call(self, server, tool, arguments, catalog_sha256):
            if catalog_sha256 != "1" * 64:
                raise PluginPreflightError("catalog_stale", "Inspect again")
            self.calls += 1
            self.entered.set()
            if self.gate is not None:
                await self.gate.wait()
            if self.error:
                raise self.error
            self.value += 1
            return {"content": [{"type": "text", "text": str(self.value)}],
                    "is_error": False, "truncated": False}

    monkeypatch.setattr(codex_plugins, "PluginContext", Context)
    return instances


async def invoke(pool, session_id, cwd, *, owner="peer-a", digest="1" * 64):
    return await pool.call(
        session_id, owner=owner, cwd=str(cwd), server="counter", tool="increment",
        arguments={}, catalog_sha256=digest,
    )


async def test_same_session_retains_state_and_inspection_arguments(contexts, tmp_path):
    pool = PluginSessions()
    try:
        opened = await pool.open(str(tmp_path), owner="peer-a")
        session_id = opened["session_id"]
        inspected = await pool.inspect(session_id, owner="peer-a", cwd=str(tmp_path))
        row = inspected["servers"][0]
        assert row["inspect_arguments"]["session_id"] == session_id
        assert row["tools"][0]["call_arguments"]["session_id"] == session_id
        first = await invoke(pool, session_id, tmp_path)
        second = await invoke(pool, session_id, tmp_path)
        assert first["content"][0]["text"] == "1"
        assert second["content"][0]["text"] == "2"
        assert len(contexts) == 1
        status = await pool.status(session_id, owner="peer-a")
        assert status["state"] == "open"
        assert status["inference_requested"] is False
    finally:
        await pool.close()
    assert not contexts[0].alive


async def test_other_owner_and_wrong_workspace_are_rejected(contexts, tmp_path):
    pool = PluginSessions()
    other = tmp_path / "other"
    other.mkdir()
    try:
        session_id = (await pool.open(str(tmp_path), owner="peer-a"))["session_id"]
        for action in (
            pool.status(session_id, owner="peer-b"),
            pool.stop(session_id, owner="peer-b"),
            invoke(pool, session_id, tmp_path, owner="peer-b"),
            invoke(pool, session_id, tmp_path, owner=None),
        ):
            with pytest.raises(PluginPreflightError, match="session_not_found"):
                await action
        with pytest.raises(PluginPreflightError, match="session_workspace_mismatch"):
            await invoke(pool, session_id, other)
        assert contexts[0].calls == 0 and contexts[0].alive
    finally:
        await pool.close()


async def test_closed_session_never_recreates_runtime(contexts, tmp_path):
    pool = PluginSessions()
    try:
        session_id = (await pool.open(str(tmp_path), owner="peer-a"))["session_id"]
        stopped = await pool.stop(session_id, owner="peer-a")
        assert stopped["state"] == "closed" and stopped["cleanup_confirmed"] is True
        assert (await pool.stop(session_id, owner="peer-a"))["state"] == "closed"
        with pytest.raises(PluginPreflightError, match="session_closed"):
            await invoke(pool, session_id, tmp_path)
        assert len(contexts) == 1 and not contexts[0].alive
    finally:
        await pool.close()


async def test_idle_expiry_and_status_polling_do_not_extend_lifetime(contexts, tmp_path):
    now = [0.0]
    pool = PluginSessions(clock=lambda: now[0])
    try:
        session_id = (await pool.open(str(tmp_path), owner="peer-a", idle_timeout=30))["session_id"]
        now[0] = 29
        assert (await pool.status(session_id, owner="peer-a"))["state"] == "open"
        now[0] = 31
        await pool.expire_idle()
        assert (await pool.status(session_id, owner="peer-a"))["state"] == "expired"
        with pytest.raises(PluginPreflightError, match="session_expired"):
            await invoke(pool, session_id, tmp_path)
        assert not contexts[0].alive
    finally:
        await pool.close()


async def test_unknown_call_invalidates_session_without_retry(contexts, tmp_path):
    pool = PluginSessions()
    try:
        session_id = (await pool.open(str(tmp_path), owner="peer-a"))["session_id"]
        contexts[0].error = PluginCallOutcomeUnknown("lost response")
        with pytest.raises(PluginCallOutcomeUnknown):
            await invoke(pool, session_id, tmp_path)
        assert (await pool.status(session_id, owner="peer-a"))["state"] == "unusable"
        with pytest.raises(PluginPreflightError, match="session_unusable"):
            await invoke(pool, session_id, tmp_path)
        assert contexts[0].calls == 1 and not contexts[0].alive
    finally:
        await pool.close()


async def test_stale_catalog_does_not_close_healthy_session(contexts, tmp_path):
    pool = PluginSessions()
    try:
        session_id = (await pool.open(str(tmp_path), owner="peer-a"))["session_id"]
        with pytest.raises(PluginPreflightError, match="catalog_stale"):
            await invoke(pool, session_id, tmp_path, digest="0" * 64)
        assert contexts[0].calls == 0 and contexts[0].alive
        assert (await invoke(pool, session_id, tmp_path))["content"][0]["text"] == "1"
    finally:
        await pool.close()


async def test_busy_session_cannot_be_closed_or_expired(contexts, tmp_path):
    now = [0.0]
    pool = PluginSessions(clock=lambda: now[0])
    try:
        session_id = (await pool.open(str(tmp_path), owner="peer-a", idle_timeout=30))["session_id"]
        contexts[0].gate = asyncio.Event()
        running = asyncio.create_task(invoke(pool, session_id, tmp_path))
        await contexts[0].entered.wait()
        now[0] = 40
        await pool.expire_idle()
        assert contexts[0].alive
        for action in (pool.stop(session_id, owner="peer-a"), invoke(pool, session_id, tmp_path)):
            with pytest.raises(PluginPreflightError, match="session_busy"):
                await action
        contexts[0].gate.set()
        await running
        assert (await pool.status(session_id, owner="peer-a"))["state"] == "open"
        assert contexts[0].calls == 1
    finally:
        await pool.close()


@pytest.mark.parametrize("tool", ["subchat_send", "subchat_queue_watch"])
async def test_idle_session_keeps_background_subchat_alive_until_work_ends(
    tmp_path, monkeypatch, tool,
):
    now = [0.0]

    class Context:
        def __init__(self, cwd):
            self.cwd = str(Path(cwd).resolve())
            self.alive = False
            self.active_count = 1
            self.activity_probes = 0

        async def open(self):
            self.alive = True

        async def close(self):
            self.alive = False

        async def inspect(self, **kwargs):
            assert kwargs == {"server": "chat-subchat", "tool": "subchat_activity"}
            return {"servers": [{"server": "chat-subchat", "tools": [{
                "name": "subchat_activity", "catalog_sha256": "1" * 64,
            }]}]}

        async def call(self, server, name, arguments, catalog_sha256):
            assert server == "chat-subchat" and catalog_sha256 == "1" * 64
            if name == "subchat_activity":
                self.activity_probes += 1
                return {"is_error": False, "structured_content": {
                    "state": "completed", "data": {"active_count": self.active_count},
                }}
            assert name == tool
            return {"is_error": False, "structured_content": {
                "state": "running" if tool == "subchat_send" else "completed",
                "data": {"state": "prepared" if tool == "subchat_send" else "watching"},
            }}

    monkeypatch.setattr(codex_plugins, "PluginContext", Context)
    pool = PluginSessions(clock=lambda: now[0])
    try:
        session_id = (await pool.open(str(tmp_path), owner="peer-a", idle_timeout=30))["session_id"]
        entry = pool.entries[session_id]
        await pool.call(session_id, owner="peer-a", cwd=str(tmp_path),
                        server="chat-subchat", tool=tool, arguments={},
                        catalog_sha256="1" * 64)
        now[0] = 31
        await pool.expire_idle()
        assert (await pool.status(session_id, owner="peer-a"))["state"] == "open"
        assert entry.context.alive and entry.context.activity_probes == 1
        now[0] = 62
        entry.context.active_count = 0
        await pool.expire_idle()
        assert (await pool.status(session_id, owner="peer-a"))["state"] == "expired"
        assert not entry.context.alive
    finally:
        await pool.close()


async def test_capacity_is_bounded_and_released(contexts, tmp_path):
    pool = PluginSessions(max_sessions=2)
    try:
        opened = await asyncio.gather(*[
            pool.open(str(tmp_path), owner="peer-a") for _ in range(3)
        ], return_exceptions=True)
        assert sum(isinstance(item, PluginPreflightError) for item in opened) == 1
        assert len(contexts) == 2
        await pool.stop(opened[0]["session_id"], owner="peer-a")
        await pool.open(str(tmp_path), owner="peer-a")
        assert sum(item.alive for item in contexts) == 2
    finally:
        await pool.close()
    assert not any(item.alive for item in contexts)


async def test_engine_deduplicates_effects_and_remote_owners(contexts, tmp_path):
    engine = Engine(tmp_path / "engine")
    tools = frozenset({"codex_plugin_session_open", "codex_plugin_session_status",
                       "codex_plugin_session_close", "codex_plugin_tools",
                       "codex_plugin_call", "operations_get"})
    remote = RemoteAgent(engine, {"peer-a": tools, "peer-b": tools})

    async def send(peer, tool, arguments, operation_id=None):
        request = Request(operation_id=operation_id or uuid.uuid4().hex,
                          tool=tool, arguments=arguments)
        payload = await remote.dispatch(peer, request.model_dump_json().encode())
        return Reply.model_validate_json(payload)

    try:
        opened = await send("peer-a", "codex_plugin_session_open", {"cwd": str(tmp_path)})
        assert opened.state == "completed"
        session_id = opened.data["session_id"]
        arguments = {"cwd": str(tmp_path), "session_id": session_id,
                     "server": "counter", "tool": "increment", "arguments": {},
                     "catalog_sha256": "1" * 64}
        operation_id = uuid.uuid4().hex
        first, repeated = await asyncio.gather(*[
            send("peer-a", "codex_plugin_call", arguments, operation_id) for _ in range(2)
        ])
        assert first == repeated and first.state == "completed"
        assert contexts[0].calls == 1
        recovered = await send("peer-a", "operations_get", {"operation_id": operation_id})
        assert recovered.data["operation_id"] == operation_id
        assert recovered.data["data"]["content"][0]["text"] == "1"
        other = await send("peer-b", "codex_plugin_call", arguments)
        assert other.data["error_code"] == "session_not_found"
        remote.revoke("peer-a")
        revoked = await send("peer-a", "codex_plugin_call", arguments)
        assert revoked.state == "failed"
        assert contexts[0].calls == 1
        assert "peer-a" not in json.dumps(first.data)
    finally:
        await engine.close()


async def test_engine_restart_does_not_reopen_old_session(contexts, tmp_path):
    directory = tmp_path / "engine"
    engine = Engine(directory)
    request = Request(operation_id=uuid.uuid4().hex, tool="codex_plugin_session_open",
                      arguments={"cwd": str(tmp_path)})
    opened = await engine.execute(request)
    await engine.close()
    restarted = Engine(directory)
    try:
        replay = await restarted.execute(request)
        assert replay == opened
        status = await restarted.execute(Request(
            operation_id=uuid.uuid4().hex, tool="codex_plugin_session_status",
            arguments={"session_id": opened.data["session_id"]},
        ))
        assert status.data["error_code"] == "session_not_found"
        assert len(contexts) == 1
    finally:
        await restarted.close()


async def test_reaper_expires_without_a_client_request(contexts, tmp_path, monkeypatch):
    from anywhere_computer import plugin_sessions

    monkeypatch.setattr(plugin_sessions, "REAPER_INTERVAL", 0.01)
    now = [0.0]
    pool = PluginSessions(clock=lambda: now[0])
    try:
        session_id = (await pool.open(str(tmp_path), owner=None, idle_timeout=30))["session_id"]
        now[0] = 31
        async with asyncio.timeout(1):
            while contexts[0].alive:
                await asyncio.sleep(0.01)
        assert pool.entries[session_id].state == "expired"
    finally:
        await pool.close()


async def test_dead_runtime_is_not_restarted(contexts, tmp_path):
    pool = PluginSessions()
    try:
        session_id = (await pool.open(str(tmp_path), owner="peer-a"))["session_id"]
        contexts[0].alive = False
        with pytest.raises(PluginPreflightError, match="session_unusable"):
            await invoke(pool, session_id, tmp_path)
        assert len(contexts) == 1 and contexts[0].calls == 0
    finally:
        await pool.close()


async def test_start_failure_cleans_up_and_releases_capacity(contexts, tmp_path, monkeypatch):
    async def fail(context):
        context.alive = True
        raise OSError("fixture startup failed")

    monkeypatch.setattr(codex_plugins.PluginContext, "open", fail)
    pool = PluginSessions(max_sessions=1)
    try:
        for _ in range(2):
            with pytest.raises(PluginPreflightError, match="session_start_failed") as caught:
                await pool.open(str(tmp_path), owner=None)
            assert caught.value.details["cleanup_confirmed"] is True
        assert len(contexts) == 2 and not any(item.alive for item in contexts)
    finally:
        await pool.close()


async def test_cleanup_failure_stays_in_capacity_budget(contexts, tmp_path):
    pool = PluginSessions(max_sessions=1)
    try:
        session_id = (await pool.open(str(tmp_path), owner=None))["session_id"]
        close = contexts[0].close

        async def fail():
            raise OSError("fixture cleanup failed")

        contexts[0].close = fail
        stopped = await pool.stop(session_id, owner=None)
        assert stopped["cleanup_confirmed"] is False
        with pytest.raises(PluginPreflightError, match="session_capacity"):
            await pool.open(str(tmp_path), owner=None)
        contexts[0].close = close
        assert (await pool.stop(session_id, owner=None))["cleanup_confirmed"] is True
        await pool.open(str(tmp_path), owner=None)
    finally:
        await pool.close()


async def test_cancelled_observer_does_not_cancel_owned_call(contexts, tmp_path):
    engine = Engine(tmp_path / "engine")
    try:
        opened = await engine.execute(Request(
            operation_id=uuid.uuid4().hex, tool="codex_plugin_session_open",
            arguments={"cwd": str(tmp_path)},
        ), peer="peer-a")
        contexts[0].gate = asyncio.Event()
        request = Request(operation_id=uuid.uuid4().hex, tool="codex_plugin_call", arguments={
            "session_id": opened.data["session_id"], "cwd": str(tmp_path), "server": "counter",
            "tool": "increment", "arguments": {}, "catalog_sha256": "1" * 64,
        })
        observer = asyncio.create_task(engine.execute(request, peer="peer-a"))
        await contexts[0].entered.wait()
        observer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await observer
        contexts[0].gate.set()
        result = await engine.execute(request, peer="peer-a")
        assert result.state == "completed" and contexts[0].calls == 1
        assert (await engine.plugin_sessions.status(
            opened.data["session_id"], owner="peer-a",
        ))["state"] == "open"
    finally:
        await engine.close()


@pytest.mark.parametrize("key", ["owner", "peer"])
async def test_tool_arguments_cannot_spoof_authenticated_owner(contexts, tmp_path, key):
    engine = Engine(tmp_path / "engine")
    try:
        result = await engine.execute(Request(
            operation_id=uuid.uuid4().hex, tool="codex_plugin_session_open",
            arguments={"cwd": str(tmp_path), key: "other-owner"},
        ))
        assert result.state == "failed" and not contexts
    finally:
        await engine.close()


async def test_closed_metadata_is_bounded(contexts, tmp_path, monkeypatch):
    from anywhere_computer import plugin_sessions

    monkeypatch.setattr(plugin_sessions, "MAX_RETIRED", 2)
    pool = PluginSessions()
    try:
        for _ in range(6):
            session_id = (await pool.open(str(tmp_path), owner=None))["session_id"]
            await pool.stop(session_id, owner=None)
        await pool.expire_idle()
        assert len(pool.entries) == 2 and not any(item.alive for item in contexts)
    finally:
        await pool.close()


@pytest.mark.parametrize("server,tool", [
    ("codex_apps", "anywhere_computer.codex_plugin_call"), ("cua_repl", "js"),
])
async def test_session_cannot_bypass_route_preflight(contexts, tmp_path, server, tool):
    pool = PluginSessions()
    try:
        session_id = (await pool.open(str(tmp_path), owner=None))["session_id"]
        with pytest.raises(ValueError):
            await pool.call(session_id, owner=None, cwd=str(tmp_path), server=server,
                            tool=tool, arguments={}, catalog_sha256="1" * 64)
        assert contexts[0].calls == 0
        assert (await pool.status(session_id, owner=None))["state"] == "open"
    finally:
        await pool.close()


async def test_classified_catalog_failure_invalidates_session(contexts, tmp_path):
    pool = PluginSessions()
    try:
        session_id = (await pool.open(str(tmp_path), owner=None))["session_id"]

        async def fail(**kwargs):
            raise PluginPreflightError("plugin_catalog_failed", "Check local runtime",
                                       details={"failure_stage": "catalog", "rpc_code": -32602})

        contexts[0].inspect = fail
        with pytest.raises(PluginPreflightError) as caught:
            await pool.inspect(session_id, owner=None, cwd=str(tmp_path))
        assert caught.value.details["rpc_code"] == -32602
        assert (await pool.status(session_id, owner=None))["state"] == "unusable"
        assert not contexts[0].alive and contexts[0].calls == 0
    finally:
        await pool.close()


async def test_idle_plugin_state_blocks_update_and_reports_resource(contexts, tmp_path):
    engine = Engine(tmp_path)
    try:
        opened = await engine.plugin_sessions.open(str(tmp_path), owner=None)
        status = engine.status()
        assert status["active_sessions"] == 1
        assert status["active_resources"]["plugin_sessions"] == 1
        assert status["update_blocked"] is True
        await engine.plugin_sessions.stop(opened["session_id"], owner=None)
        assert engine.status()["update_blocked"] is False
    finally:
        await engine.close()
