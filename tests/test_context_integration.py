import uuid

from anywhere_computer import codex_context, codex_plugins, skills_context
from anywhere_computer.engine import Engine
from anywhere_computer.models import Reply, Request
from anywhere_computer.remote_bridge import RemoteAgent


async def test_context_registry_dispatch_grants_and_no_inference(tmp_path, monkeypatch):
    calls = []

    async def threads(limit, cursor):
        calls.append(("threads", limit, cursor))
        return {"threads": [{"id": "selected", "title": "Selected conversation"}]}

    async def history(thread_id, limit, cursor):
        calls.append(("history", thread_id, limit, cursor))
        return {"messages": [{"role": "user", "text": "selected text"}]}

    async def skills(cwd, limit, after):
        calls.append(("skills", cwd, limit, after))
        return {"skills": [{"skill_id": "a" * 64, "name": "example"}]}

    async def skill(skill_id, cwd):
        calls.append(("skill", skill_id, cwd))
        return {"text": "selected skill"}

    monkeypatch.setattr(codex_context, "list_codex_threads", threads)
    monkeypatch.setattr(codex_context, "read_codex_thread", history)
    monkeypatch.setattr(skills_context, "list_codex_skills", skills)
    monkeypatch.setattr(skills_context, "read_codex_skill", skill)
    engine = Engine(tmp_path / "state")
    names = frozenset({"codex_threads_list", "codex_thread_read",
                       "codex_skills_list", "codex_skill_read"})
    bridge = RemoteAgent(engine, {"allowed": names, "existing": frozenset({"computer_status"})})

    async def remote(peer, name, arguments):
        request = Request(operation_id=uuid.uuid4().hex, tool=name, arguments=arguments)
        return Reply.model_validate_json(await bridge.dispatch(
            peer, request.model_dump_json().encode(),
        ))

    try:
        catalog = engine.catalog(names)
        assert {entry["name"] for entry in catalog} == names
        assert all(entry["annotations"]["readOnlyHint"] is True for entry in catalog)
        # A previously issued ordinary file/status grant does not acquire new context access.
        denied = await remote("existing", "codex_thread_read", {"thread_id": "selected"})
        assert denied.state == "failed" and not calls
        invalid = await remote("allowed", "codex_thread_read", {"thread_id": "../auth.json"})
        assert invalid.state == "failed" and not calls
        for name, arguments in [
            ("codex_threads_list", {"limit": 2}),
            ("codex_thread_read", {"thread_id": "selected", "limit": 1}),
            ("codex_skills_list", {"cwd": str(tmp_path), "limit": 1}),
            ("codex_skill_read", {"skill_id": "a" * 64, "cwd": str(tmp_path)}),
        ]:
            assert (await remote("allowed", name, arguments)).state == "completed"
        assert calls == [
            ("threads", 2, None), ("history", "selected", 1, None),
            ("skills", str(tmp_path), 1, None), ("skill", "a" * 64, str(tmp_path)),
        ]
        bridge.revoke("allowed")
        assert (await remote("allowed", "codex_threads_list", {})).state == "failed"
        assert len(calls) == 4
    finally:
        await engine.close()


async def test_plugin_unknown_is_durable_and_never_redispatched(tmp_path, monkeypatch):
    dispatched = []

    async def call(**arguments):
        dispatched.append(arguments)
        raise codex_plugins.PluginCallOutcomeUnknown("Plugin call outcome was not confirmed")

    monkeypatch.setattr(codex_plugins, "call_codex_plugin_tool", call)
    engine = Engine(tmp_path / "state")
    request = Request(operation_id=uuid.uuid4().hex, tool="codex_plugin_call", arguments={
        "cwd": str(tmp_path), "server": "fixture", "tool": "write",
        "arguments": {"text": "fixture"}, "catalog_sha256": "a" * 64,
    })
    try:
        tool = next(t for t in engine.catalog() if t["name"] == "codex_plugin_call")
        assert tool["annotations"]["readOnlyHint"] is False
        assert tool["annotations"]["destructiveHint"] is True
        assert tool["annotations"]["openWorldHint"] is True
        assert (await engine.execute(request)).state == "unknown"
        assert (await engine.execute(request)).state == "unknown"
        assert len(dispatched) == 1
    finally:
        await engine.close()
    engine = Engine(tmp_path / "state")
    try:
        assert (await engine.execute(request)).state == "unknown"
        assert len(dispatched) == 1
    finally:
        await engine.close()


async def test_plugin_execution_is_not_in_read_or_file_profile():
    from anywhere_computer.remote_setup import setup_scopes

    assert not {"codex_plugin_tools", "codex_plugin_call"} & await setup_scopes("read-only")
    assert not {"codex_plugin_tools", "codex_plugin_call"} & await setup_scopes("files")
    assert {"codex_plugin_tools", "codex_plugin_call"} <= await setup_scopes("all")
