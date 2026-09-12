"""Core execution must remain available when the optional Codex runtime is absent."""

import asyncio
import shlex
import sys
import uuid

from anywhere_computer.engine import Engine
from anywhere_computer.models import Request


async def test_missing_codex_does_not_disable_file_terminal_or_result_recovery(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("ANYWHERE_CODEX_EXECUTABLE", str(tmp_path / "missing-codex"))
    engine = Engine(tmp_path / "state")

    async def call(tool, **arguments):
        return await engine.execute(Request(
            operation_id=uuid.uuid4().hex, tool=tool, arguments=arguments,
        ))

    try:
        missing = await call("codex_plugin_session_open", cwd=str(tmp_path))
        assert missing.state == "failed"
        path = str(tmp_path / "independent.txt")
        written = await call("files_write", path=path, text="without Codex", mode="create")
        assert written.state == "completed"
        read = await call("files_read", path=path)
        assert read.data["text"] == "without Codex"
        recovered = await call("operations_get", operation_id=written.operation_id)
        assert recovered.data["data"] == written.data
        program = f'"{sys.executable}"' if sys.platform == "win32" else shlex.quote(sys.executable)
        started = await call("terminal_start", cwd=str(tmp_path),
                             command=program + ' -c "print(4242)"')
        assert started.state == "completed"
        identity = started.data["session_id"]
        await asyncio.wait_for(engine.sessions.get(identity).reader, 5)
        output = await call("terminal_output", session_id=identity)
        assert output.data["text"].strip() == "4242"
        assert output.data["exit_code"] == 0
    finally:
        await engine.close()
