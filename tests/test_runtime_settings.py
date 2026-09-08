import asyncio
import sys
import uuid

from anywhere_computer.engine import Engine
from anywhere_computer.models import Request


async def call(engine, tool, **arguments):
    return await engine.execute(Request(operation_id=uuid.uuid4().hex,
                                        tool=tool, arguments=arguments))


async def test_settings_persist_apply_and_invalid_update_rolls_back(tmp_path):
    engine = Engine(tmp_path)
    path = tmp_path / "sample"
    path.write_text("one\ntwo\nthree\n")
    try:
        changed = await call(engine, "settings_update", key="file_read_line_limit", value=1)
        assert changed.state == "completed"
        read = await call(engine, "files_read", path=str(path), limit=100)
        assert read.data["text"] == "one\n"
        invalid = await call(engine, "settings_update", key="file_read_line_limit", value="2")
        assert invalid.state == "failed"
        assert engine.settings().file_read_line_limit == 1
        await call(engine, "settings_update", key="file_write_line_limit", value=1)
        blocked = await call(engine, "files_write", path=str(tmp_path / "new"), text="a\nb")
        assert blocked.state == "failed" and not (tmp_path / "new").exists()
    finally:
        await engine.close()
    restarted = Engine(tmp_path)
    try:
        settings = await call(restarted, "settings_get")
        assert settings.data["file_read_line_limit"] == 1
        assert settings.data["file_write_line_limit"] == 1
    finally:
        await restarted.close()


async def test_saved_shell_is_used_by_terminal_start(tmp_path):
    engine = Engine(tmp_path)
    try:
        changed = await call(engine, "settings_update", key="default_shell", value=sys.executable)
        assert changed.state == "completed"
        started = await call(engine, "terminal_start", cwd=str(tmp_path),
                             command="print('configured-shell')")
        assert started.state == "completed"
        session = engine.sessions.get(started.data["session_id"])
        await asyncio.wait_for(session.reader, 5)
        output = await call(engine, "terminal_output", session_id=session.session_id)
        assert "configured-shell" in output.data["text"]
    finally:
        await engine.close()
