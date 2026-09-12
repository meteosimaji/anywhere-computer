import asyncio
import os
import shlex
import sys
import uuid

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

        return subprocess.list2cmdline([sys.executable, "-u", "-c", script])
    return shlex.join([sys.executable, "-u", "-c", script])


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

    assert len(engine.tools) == 55
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
