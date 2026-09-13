"""2026-09-13 audit regressions. No GUI, live services or real shell effects."""

import asyncio
import sys
from types import SimpleNamespace

import pytest

from anywhere_computer.direct_mcp_sessions import DirectMCPSessions
from anywhere_computer.engine import Engine
from anywhere_computer.models import Request, StartSession
from anywhere_computer.sessions import Session, Sessions


@pytest.mark.parametrize("failure_type", [OSError, FileNotFoundError, PermissionError])
async def test_failed_spawn_does_not_consume_direct_mcp_capacity(
    tmp_path, monkeypatch, failure_type,
):
    import mcp.client.stdio

    async def fail_before_process_exists(*args, **kwargs):
        raise failure_type("synthetic spawn failure; no child was created")

    monkeypatch.setattr(mcp.client.stdio, "_create_platform_compatible_process",
                        fail_before_process_exists)
    pool = DirectMCPSessions()
    failures = []
    try:
        for _ in range(5):
            with pytest.raises(RuntimeError) as caught:
                await pool.open([sys.executable], tmp_path, owner="audit")
            failures.append(str(caught.value))
        assert pool.active_count == 0, failures
        assert all("did not initialize" in message for message in failures), failures
    finally:
        await pool.close()


async def test_concurrent_terminal_starts_respect_capacity(tmp_path, monkeypatch):
    """Use fake processes and a spawn barrier; never create 33 OS processes."""
    manager = Sessions()
    entered = asyncio.Event()
    release = asyncio.Event()
    spawn_calls = 0

    async def delayed_spawn(*args, **kwargs):
        nonlocal spawn_calls
        spawn_calls += 1
        entered.set()
        await release.wait()
        return SimpleNamespace(returncode=None, pid=100000 + spawn_calls)

    async def no_reader(self, session):
        return None

    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed_spawn)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", delayed_spawn)
    monkeypatch.setattr(Sessions, "_read", no_reader)
    arguments = StartSession(command="synthetic; never executed", cwd=str(tmp_path),
                             shell=sys.executable)
    pending = [asyncio.create_task(manager.start(arguments)) for _ in range(33)]
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await asyncio.sleep(0)
        release.set()
        results = await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), 5)
        successes = [value for value in results if isinstance(value, dict)]
        rejections = [value for value in results if isinstance(value, ValueError)]
        assert len(successes) == 32, (len(successes), spawn_calls)
        assert len(rejections) == 1
        assert "32 active sessions" in str(rejections[0])
        assert spawn_calls == 32
    finally:
        release.set()
        for task in pending:
            if not task.done():
                task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        readers = [session.reader for session in manager.sessions.values() if session.reader]
        await asyncio.gather(*readers, return_exceptions=True)
        manager.sessions.clear()


@pytest.mark.parametrize("failure_type", [BrokenPipeError, TimeoutError])
async def test_terminal_input_delivery_failure_is_unknown(tmp_path, failure_type):
    """A write occurred before drain failed; failed is not a safe execution outcome."""
    writes = []

    class InputStream:
        def write(self, payload):
            writes.append(payload)

        async def drain(self):
            raise failure_type("synthetic stream failure; private details must not escape")

    engine = Engine(tmp_path / "engine")
    engine.sessions.sessions["audit-input"] = Session(
        "audit-input", SimpleNamespace(returncode=None, stdin=InputStream(), pid=123456), 0.0,
    )
    request = Request(operation_id="d" * 32, tool="terminal_input", arguments={
        "session_id": "audit-input", "text": "synthetic-command\n",
    })
    try:
        reply = await engine.execute(request)
        assert writes == [b"synthetic-command\n"]
        assert reply.state == "unknown", reply.model_dump()
        assert reply.data["error_code"] == "terminal_input_outcome_unknown"
        assert "private details" not in (reply.error or "")
        assert engine.ledger.get(request.operation_id) == reply
        assert await engine.execute(request) == reply
        assert writes == [b"synthetic-command\n"]
    finally:
        engine.sessions.sessions.clear()
        await engine.close()


async def test_terminal_input_rejected_before_write_stays_failed(tmp_path):
    engine = Engine(tmp_path / "engine")
    engine.sessions.sessions["audit-closed"] = Session(
        "audit-closed", SimpleNamespace(returncode=0, stdin=None, pid=123456), 0.0,
    )
    try:
        reply = await engine.execute(Request(operation_id="e" * 32, tool="terminal_input",
            arguments={"session_id": "audit-closed", "text": "not sent\n"}))
        assert reply.state == "failed"
        assert reply.error == "Session is not accepting input"
    finally:
        engine.sessions.sessions.clear()
        await engine.close()
