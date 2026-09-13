"""Real orphaned children remain owned by a persistent terminal worker."""

import asyncio
import json
import os

import psutil
import pytest
from test_engine import python_command

from anywhere_computer.engine import Engine
from anywhere_computer.models import SessionOutput, StartSession


@pytest.mark.parametrize("ignore_term", [False, True])
async def test_exited_shell_child_blocks_update_and_is_stopped(tmp_path, ignore_term):
    engine = Engine(tmp_path / "state")
    descendant = None
    child_code = "import time; time.sleep(20)"
    if ignore_term:
        child_code = (
            "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(20)"
        )
    source = (
        "import json,os,subprocess,sys\n"
        f"code = {child_code!r}\n"
        "child = subprocess.Popen([sys.executable, '-c', code], "
        "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        "print(json.dumps([os.getpid(), child.pid]), flush=True)\n"
    )
    try:
        started = await engine.sessions.start(StartSession(
            command=python_command(source), cwd=str(tmp_path),
        ))
        identity = started["session_id"]
        async with asyncio.timeout(5):
            while True:
                page = await engine.sessions.wait_output(SessionOutput(
                    session_id=identity, wait_ms=100,
                ))
                if "\n" in page["text"]:
                    break
                if page['state'] == 'exited':
                    pytest.fail(f"Fixture exited before its PID record: {page['text']!r}")
                # wait_output can complete without yielding after EOF. Keep the deadline live.
                await asyncio.sleep(0.01)
        parent_pid, child_pid = json.loads(page["text"])
        parent = psutil.Process(parent_pid) if psutil.pid_exists(parent_pid) else None
        descendant = psutil.Process(child_pid)
        if parent:
            await asyncio.to_thread(parent.wait, timeout=5)
        # Give the child time to install its signal handler before the stop request.
        await asyncio.sleep(0.1)
        assert descendant.is_running()
        assert engine.status()["active_resources"]["terminal_sessions"] == 1
        assert engine.status()["update_blocked"] is True
        assert engine.sessions.describe(engine.sessions.get(identity))["state"] == "running"
        stopped = await engine.sessions.stop(identity)
        assert stopped["state"] == "exited"
        async with asyncio.timeout(5):
            while descendant.is_running() and descendant.status() != psutil.STATUS_ZOMBIE:
                await asyncio.sleep(0.02)
        assert engine.status()["active_resources"]["terminal_sessions"] == 0
        assert engine.status()["update_blocked"] is False
    finally:
        if descendant and descendant.is_running():
            try:
                descendant.kill()
            except psutil.NoSuchProcess:
                pass
        await engine.close()


@pytest.mark.skipif(os.name == "nt", reason="POSIX process group signalling")
async def test_completed_worker_never_signals_a_reused_group_id(tmp_path, monkeypatch):
    engine = Engine(tmp_path / "state")
    try:
        started = await engine.sessions.start(StartSession(
            command=python_command("print('done')"), cwd=str(tmp_path),
        ))
        session = engine.sessions.get(started["session_id"])
        await asyncio.wait_for(session.reader, 5)

        def unexpected_signal(*args):
            pytest.fail("An exited ownership anchor must never signal its former PGID")

        monkeypatch.setattr(os, "killpg", unexpected_signal)
        assert (await engine.sessions.stop(session.session_id))["state"] == "exited"
    finally:
        await engine.close()
