import asyncio
import os
import shlex
import sys
import uuid
from types import SimpleNamespace

import pytest

from anywhere_computer.engine import Engine
from anywhere_computer.files import Files, sha256
from anywhere_computer.models import EditFile, ReadFile, Request, WriteFile
from anywhere_computer.state import Ledger, prepare_directory


def request(tool, **arguments):
    return Request(operation_id=uuid.uuid4().hex, tool=tool, arguments=arguments)


def python_command(script):
    if os.name == "nt":
        import subprocess

        # Keep source quotes, newlines and shell metacharacters out of cmd.exe parsing.
        source = script.encode("utf-8").hex()
        return subprocess.list2cmdline([
            sys.executable, "-u", "-c", f"exec(bytes.fromhex('{source}'))",
        ])
    return shlex.join([sys.executable, "-u", "-c", script])


def test_python_command_preserves_multiline_source_and_shell_characters():
    import subprocess

    text = "日本語 'quoted' \"double\" READY> < & | %PATH% !value!\nnext"
    script = "import sys\nsys.stdout.reconfigure(encoding='utf-8')\n" + f"print({text!r})"
    output = subprocess.check_output(python_command(script), shell=True, timeout=5)
    assert output.decode("utf-8").replace("\r\n", "\n") == text + "\n"


def test_status_separates_capability_evidence_without_claiming_acceptance(tmp_path):
    engine = Engine(tmp_path / "state")
    try:
        status = engine.status()
        diagnostics = status["capability_diagnostics"]
        assert diagnostics["skills"] == {
            "running_implementation": "present",
            "runtime_available": "unknown",
            "connection_authorization": "not_observed",
            "helper": "not_required",
            "os_permission": "not_required",
            "acceptance": "not_verified",
        }
        assert diagnostics["gui_native"]["running_implementation"] == "present"
        assert diagnostics["gui_native"]["helper"] in {
            "verified_available", "unavailable", "unsupported_platform", "verification_failed",
        }
        assert diagnostics["gui_native"]["os_permission"] == "not_checked"
        assert diagnostics["gui_native"]["acceptance"] == "not_verified"
    finally:
        asyncio.run(engine.close())


async def test_status_lists_only_current_owners_update_blockers(engine):
    current = "a" * 32
    other = "b" * 32
    own_operation = "c" * 32
    other_operation = "d" * 32
    busy_lock = asyncio.Lock()
    await busy_lock.acquire()
    engine.plugin_sessions.entries[current] = SimpleNamespace(
        owner="owner-a", state="open", lock=busy_lock, cleanup_confirmed=False,
    )
    engine.plugin_sessions.entries[other] = SimpleNamespace(
        owner="owner-b", state="open", lock=asyncio.Lock(), cleanup_confirmed=False,
    )
    release = asyncio.Event()
    own_task = asyncio.create_task(release.wait(), name="files_write")
    other_task = asyncio.create_task(release.wait(), name="terminal_input")
    engine.inflight.update({own_operation: own_task, other_operation: other_task})
    engine.inflight_owners.update({own_operation: "owner-a", other_operation: "owner-b"})
    try:
        result = engine.status(owner="owner-a")
        assert result["update_blocked"] is True
        assert result["update_blockers"] == [
            "plugin_sessions", "operations", "other_active_resources",
        ]
        assert result["update_blocker_details"] == [
            {
                "resource": "plugin_session", "id": current, "state": "busy",
                "stop_tool": "codex_plugin_session_close", "stop_available": False,
            },
            {
                "resource": "operation", "id": own_operation, "state": "running",
                "inspect_tool": "operations_get", "stop_available": False,
            },
        ]
        assert other not in repr(result["update_blocker_details"])
        assert other_operation not in repr(result["update_blocker_details"])
        unrelated = engine.status(owner="owner-c")
        assert unrelated["update_blocker_details"] == []
        assert unrelated["active_resources"] == {
            name: 0 for name in result["active_resources"]
        }
        assert unrelated["active_sessions"] == 0
        assert unrelated["active_operations"] == 0
        assert unrelated["update_blocked"] is True
        assert unrelated["update_blockers"] == ["other_active_resources"]
        assert result["active_resources"]["plugin_sessions"] == 1
        assert result["active_resources"]["operations"] == 1
        assert "other_active_resources" in result["update_blockers"]
    finally:
        release.set()
        await asyncio.gather(own_task, other_task)
        engine.inflight.clear()
        engine.inflight_owners.clear()
        busy_lock.release()
        engine.plugin_sessions.entries.clear()


async def test_remote_terminal_status_lists_only_owned_live_blockers(engine, tmp_path):
    started = []
    try:
        for owner in ("owner-a", "owner-b"):
            reply = await engine.execute(request(
                "terminal_start", command=python_command("import time; time.sleep(30)"),
                cwd=str(tmp_path),
            ), peer=owner)
            assert reply.state == "completed"
            started.append(reply.data["session_id"])

        owned = await engine.execute(request("computer_status"), peer="owner-a")
        assert owned.state == "completed"
        assert owned.data["active_resources"]["terminal_sessions"] == 1
        assert owned.data["active_sessions"] == 1
        assert owned.data["update_blocked"] is True
        assert owned.data["update_blocker_details"] == [{
            "resource": "terminal_session", "id": started[0], "state": "running",
            "stop_tool": "terminal_stop", "stop_available": True,
        }]
        assert started[1] not in repr(owned.data)
        assert engine.status()["active_resources"]["terminal_sessions"] == 2

        await engine.sessions.stop(started[0])
        unrelated = await engine.execute(request("computer_status"), peer="owner-a")
        assert unrelated.data["active_resources"]["terminal_sessions"] == 0
        assert unrelated.data["update_blockers"] == ["other_active_resources"]
        assert unrelated.data["update_blocker_details"] == []
    finally:
        for session_id in started:
            await engine.sessions.stop(session_id)


async def test_remote_status_counts_only_owned_searches(engine, tmp_path, monkeypatch):
    release = asyncio.Event()

    async def paused_search(*_args):
        await release.wait()

    monkeypatch.setattr(engine.searches, "_run_with_deadline", paused_search)
    try:
        for owner in ("owner-a", "owner-b"):
            reply = await engine.execute(request(
                "search_start", path=str(tmp_path), pattern="needle",
            ), peer=owner)
            assert reply.state == "completed"
        assert engine.status()["active_resources"]["searches"] == 2
        assert engine.status(owner="owner-a")["active_resources"]["searches"] == 1
        unrelated = engine.status(owner="owner-c")
        assert unrelated["active_resources"]["searches"] == 0
        assert unrelated["update_blockers"] == ["other_active_resources"]
    finally:
        release.set()
        await engine.searches.close()


async def test_capacity_failure_has_fixed_code_and_safe_action(engine, tmp_path, monkeypatch):
    async def at_capacity(*args, **kwargs):
        raise RuntimeError("Direct MCP capacity reached; close an existing session")

    monkeypatch.setattr(engine.direct_mcp_sessions, "open", at_capacity)
    reply = await engine.execute(request(
        "mcp_session_open", command=[sys.executable], cwd=str(tmp_path),
    ))
    assert reply.state == "failed"
    assert reply.error == "Operation was not dispatched."
    assert reply.data["error_code"] == "session_capacity"
    assert reply.data["dispatched"] is False
    assert reply.data["next_action"]


async def test_status_shows_owned_live_watch_stop_contract(engine, monkeypatch):
    session_id = "e" * 32
    operation_id = "f" * 32
    context = SimpleNamespace(cleanup_confirmed=False)
    watch = SimpleNamespace(state="watching")
    engine.direct_mcp_sessions.entries[session_id] = SimpleNamespace(
        owner="owner-a", context=context, lock=asyncio.Lock(), state="open",
        watches={operation_id: watch},
    )
    monkeypatch.setattr(
        engine.direct_mcp_sessions, "watch_history",
        lambda *, owner: [{"session_id": session_id, "operation_id": operation_id,
                           "state": "watching", "reason": None}] if owner == "owner-a" else [],
    )
    try:
        status = engine.status(owner="owner-a")
        assert status["active_resources"]["subchat_queue_watches"] == 1
        detail = next(
            row for row in status["update_blocker_details"]
            if isinstance(row, dict) and row.get("resource") == "subchat_queue_watch"
        )
        assert detail == {
            "resource": "subchat_queue_watch", "id": operation_id,
            "session_id": session_id, "state": "watching", "reason": None,
            "stop_tool": "mcp_call",
            "stop_arguments": {
                "session_id": session_id, "name": "subchat_queue_watch",
                "arguments": {"operation_id": operation_id, "enabled": False},
            },
            "inspect_tool": "mcp_watch_list", "stop_available": True,
        }
        assert engine.status(owner="owner-b")["update_blocker_details"] == []
    finally:
        engine.direct_mcp_sessions.entries.clear()


@pytest.fixture
async def engine(tmp_path):
    result = Engine(tmp_path / "state")
    yield result
    await result.close()


def test_file_crlf_tail_and_conflict(tmp_path):
    prepare_directory(tmp_path / "state")
    files = Files(tmp_path / "state")
    path = tmp_path / "日本語.txt"
    files.write(WriteFile(path=str(path), text="一\r\n二\r\n"))
    read = files.read(ReadFile(path=str(path), offset=-1))
    assert read["text"] == "二\r\n"
    files.edit(
        EditFile(path=str(path), old_text="二", new_text="三", expected_sha256=read["sha256"])
    )
    assert path.read_bytes() == "一\r\n三\r\n".encode()
    assert (tmp_path / "state/backups" / read["sha256"]).read_bytes() == "一\r\n二\r\n".encode()
    with pytest.raises(ValueError, match="changed"):
        files.write(
            WriteFile(path=str(path), text="lost", mode="replace", expected_sha256=read["sha256"])
        )
    assert path.read_bytes() == "一\r\n三\r\n".encode()


async def test_concurrent_compare_and_swap_has_one_winner(engine, tmp_path):
    path = tmp_path / "edit.txt"
    path.write_text("before")
    results = await asyncio.gather(
        *[
            engine.execute(
                request(
                    "files_write",
                    path=str(path),
                    mode="replace",
                    text=value,
                    expected_sha256=sha256(b"before"),
                )
            )
            for value in ("one", "two")
        ]
    )
    assert sorted(reply.state for reply in results) == ["completed", "failed"]
    assert path.read_text(encoding="utf-8") in ("one", "two")


async def test_duplicate_write_and_conflicting_operation_id(engine, tmp_path):
    path = tmp_path / "once.txt"
    call = request("files_write", path=str(path), text="once")
    results = await asyncio.gather(engine.execute(call), engine.execute(call))
    assert all(result.state == "completed" for result in results)
    assert path.read_text(encoding="utf-8") == "once"
    conflicting = call.model_copy(update={"arguments": {"path": str(path), "text": "twice"}})
    assert (await engine.execute(conflicting)).state == "failed"
    assert path.read_text(encoding="utf-8") == "once"


def test_restart_marks_only_unfinished_unknown(tmp_path):
    ledger = Ledger(tmp_path)
    call = request("files_write", path="unexecuted", text="data")
    assert ledger.claim(call) is None
    ledger.close()
    restored = Ledger(tmp_path)
    assert restored.get(call.operation_id).state == "unknown"
    assert restored.claim(call).state == "unknown"
    restored.close()


async def test_client_cancellation_does_not_cancel_effect(engine):
    from anywhere_computer.models import Empty

    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed(_):
        entered.set()
        await release.wait()
        return {"done": True}

    engine.register("test_delayed", "Test cancellation", Empty, delayed)
    call = request("test_delayed")
    client = asyncio.create_task(engine.execute(call))
    await entered.wait()
    client.cancel()
    with pytest.raises(asyncio.CancelledError):
        await client
    release.set()
    result = await engine.execute(call)
    assert result.state == "completed" and result.data["done"]


async def test_terminal_survives_calls_and_interactive_input(engine, tmp_path):
    command = python_command("import sys; print('ready', flush=True); print(input(), flush=True)")
    started = await engine.execute(request("terminal_start", command=command, cwd=str(tmp_path)))
    assert started.state == "completed"
    session_id = started.data["session_id"]
    deadline = asyncio.get_running_loop().time() + 5
    while asyncio.get_running_loop().time() < deadline:
        page = await engine.execute(request("terminal_output", session_id=session_id))
        if "ready" in page.data["text"]:
            break
        await asyncio.sleep(0.02)
    else:
        pytest.fail(f"Process never became ready: {page.data}")
    assert (
        await engine.execute(request("terminal_input", session_id=session_id, text="hello\n"))
    ).state == "completed"
    session = engine.sessions.get(session_id)
    await asyncio.wait_for(session.reader, 5)
    page = await engine.execute(request("terminal_output", session_id=session_id))
    assert "hello" in page.data["text"]
    assert page.data["exit_code"] == 0


async def test_literal_search_pagination(engine, tmp_path):
    (tmp_path / "a.txt").write_text("Match\nmatch\nignore\n")
    (tmp_path / ".hidden").write_text("match")
    started = await engine.execute(
        request("search_start", path=str(tmp_path), pattern="match", kind="text")
    )
    search = engine.searches.get(started.data["search_id"])
    await search.task
    first = await engine.execute(request("search_results", search_id=search.search_id, limit=1))
    second = await engine.execute(
        request(
            "search_results", search_id=search.search_id, cursor=first.data["next_cursor"], limit=1
        )
    )
    assert first.data["total"] == 2
    assert first.data["results"][0]["line"] == 1
    assert second.data["results"][0]["line"] == 2


async def test_registry_schemas_validation_and_duplicate_guard(engine):
    from anywhere_computer.models import Empty

    assert len(engine.tools) == 70
    capture = engine.tools["audio_capture"]
    assert capture.destructive and capture.open_world and not capture.read_only
    press = engine.tools["gui_native_press"]
    assert press.destructive and press.open_world and not press.read_only
    for name, tool in engine.tools.items():
        assert name == tool.name
        assert tool.schema.model_json_schema()["additionalProperties"] is False
    with pytest.raises(ValueError, match="Duplicate"):
        engine.register(
            "computer_status", "duplicate", Empty, engine.tools["computer_status"].handler
        )
    invalid = await engine.execute(request("files_read", path="x", limit=-1))
    assert invalid.state == "failed"


async def test_recent_history_does_not_expose_content(engine, tmp_path):
    await engine.execute(
        request("files_write", path=str(tmp_path / "private"), text="not-in-history")
    )
    recent = await engine.execute(request("operations_recent"))
    assert "not-in-history" not in recent.model_dump_json()
    assert "private" not in recent.model_dump_json()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX named pipe test")
def test_read_named_pipe_rejected_without_waiting(tmp_path):
    from anywhere_computer.files import read_bytes

    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    with pytest.raises(ValueError, match="regular files"):
        read_bytes(fifo)


async def test_restore_retains_redo_and_preserves_newer_changes(engine, tmp_path):
    path = tmp_path / "recover.txt"
    original = b"before\r\n"
    path.write_bytes(original)
    changed = await engine.execute(
        request(
            "files_write",
            path=str(path),
            text="after\n",
            mode="replace",
            expected_sha256=sha256(original),
        )
    )
    backup_id = changed.data["backup_id"]
    path.write_bytes(b"newer")
    conflict = await engine.execute(
        request(
            "files_restore",
            path=str(path),
            backup_id=backup_id,
            expected_sha256=changed.data["sha256"],
        )
    )
    assert conflict.state == "failed" and path.read_bytes() == b"newer"
    restored = await engine.execute(
        request(
            "files_restore", path=str(path), backup_id=backup_id, expected_sha256=sha256(b"newer")
        )
    )
    assert restored.state == "completed" and path.read_bytes() == original
    assert restored.data["backup_id"] == sha256(b"newer")
    path.unlink()
    recreated = await engine.execute(request("files_restore", path=str(path), backup_id=backup_id))
    assert recreated.state == "completed" and path.read_bytes() == original


async def test_restore_rejects_corrupt_backup_without_modifying_target(engine, tmp_path):
    backup_id = sha256(b"expected")
    (engine.files.backups / backup_id).write_bytes(b"corrupted")
    path = tmp_path / "target"
    path.write_bytes(b"keep")
    result = await engine.execute(
        request(
            "files_restore", path=str(path), backup_id=backup_id, expected_sha256=sha256(b"keep")
        )
    )
    assert result.state == "failed" and path.read_bytes() == b"keep"
