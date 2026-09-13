"""Additional audit checks for real spawn failure and conservative recovery."""

import asyncio
import os
import sys
from types import SimpleNamespace

import pytest

from anywhere_computer.direct_mcp_sessions import DirectMCPSessions
from anywhere_computer.engine import Engine
from anywhere_computer.models import Request, StartSession
from anywhere_computer.sessions import Session, Sessions


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable with missing interpreter")
async def test_real_os_spawn_rejection_releases_capacity(tmp_path):
    script = tmp_path / "unlaunchable"
    script.write_text("#!/anywhere-audit-nonexistent-interpreter-20260913\n", encoding="utf-8")
    script.chmod(0o700)
    pool = DirectMCPSessions()
    try:
        with pytest.raises(RuntimeError, match="did not initialize"):
            await pool.open([str(script)], tmp_path, owner="audit")
        assert pool.active_count == 0
        assert all(entry.context.cleanup_confirmed for entry in pool.entries.values())
    finally:
        await pool.close()


async def test_unexpected_spawn_helper_failure_keeps_cleanup_unknown(tmp_path, monkeypatch):
    import mcp.client.stdio

    async def unknown_spawn(*args, **kwargs):
        raise RuntimeError("synthetic unexpected helper failure")

    monkeypatch.setattr(mcp.client.stdio, "_create_platform_compatible_process", unknown_spawn)
    pool = DirectMCPSessions()
    try:
        with pytest.raises(RuntimeError, match="cleanup is incomplete"):
            await pool.open([sys.executable], tmp_path, owner="audit")
        assert pool.active_count == 1
        assert not next(iter(pool.entries.values())).context.cleanup_confirmed
    finally:
        await pool.close()


async def test_terminal_failed_spawn_releases_admission_lock(tmp_path, monkeypatch):
    manager = Sessions()
    calls = 0

    async def spawn(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("synthetic spawn rejection")
        return SimpleNamespace(returncode=None, pid=123456)

    async def no_reader(self, session):
        return None

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", spawn)
    monkeypatch.setattr(Sessions, "_read", no_reader)
    arguments = StartSession(command="never executed", cwd=str(tmp_path), shell=sys.executable)
    with pytest.raises(OSError, match="synthetic"):
        await manager.start(arguments)
    result = await asyncio.wait_for(manager.start(arguments), 2)
    assert result["state"] == "running"
    readers = [entry.reader for entry in manager.sessions.values() if entry.reader]
    await asyncio.gather(*readers)
    manager.sessions.clear()


async def test_terminal_observation_failure_after_write_is_unknown(tmp_path, monkeypatch):
    writes = []

    class InputStream:
        def write(self, payload):
            writes.append(payload)

        async def drain(self):
            return None

    async def observation_failure(*args, **kwargs):
        raise ValueError("synthetic private observation failure")

    engine = Engine(tmp_path / "engine")
    engine.sessions.sessions["audit-observe"] = Session(
        "audit-observe", SimpleNamespace(returncode=None, stdin=InputStream(), pid=123456), 0.0,
    )
    monkeypatch.setattr(engine.sessions, "_wait_response", observation_failure)
    request = Request(operation_id="f" * 32, tool="terminal_input", arguments={
        "session_id": "audit-observe", "text": "synthetic\n", "wait_ms": 1,
    })
    try:
        reply = await engine.execute(request)
        assert reply.state == "unknown"
        assert reply.data["bytes_attempted"] == len(b"synthetic\n")
        assert "private observation" not in (reply.error or "")
        assert await engine.execute(request) == reply
        assert writes == [b"synthetic\n"]
    finally:
        engine.sessions.sessions.clear()
        await engine.close()
