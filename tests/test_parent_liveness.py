import json
import subprocess
import sys
import time

import psutil
import pytest


@pytest.mark.parametrize("finish", ["close", "data", "normal_exit"])
def test_parent_pipe_eof_data_and_normal_interpreter_exit(finish):
    code = (
        "from anywhere_computer.parent_liveness import watch_parent_pipe; "
        "stop=watch_parent_pipe(); "
        + ("raise SystemExit(0)" if finish == "normal_exit"
           else "raise SystemExit(0 if stop.wait(10) else 7)")
    )
    child = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE)
    try:
        assert child.stdin is not None
        if finish == "data":
            child.stdin.write(b"x")
            child.stdin.flush()
        elif finish == "close":
            child.stdin.close()
        assert child.wait(timeout=15) == 0
    finally:
        if child.stdin and not child.stdin.closed:
            child.stdin.close()
        if child.poll() is None:
            child.kill()
            child.wait(5)


def test_owner_hard_kill_closes_private_pipe_and_child_exits(tmp_path):
    ready = tmp_path / "ready.json"
    exited = tmp_path / "exited"
    child_code = (
        "from anywhere_computer.parent_liveness import watch_parent_pipe; "
        "import os,json,pathlib; stop=watch_parent_pipe(); "
        f"pathlib.Path({str(ready)!r}).write_text(json.dumps(os.getpid())); "
        "assert stop.wait(15); "
        f"pathlib.Path({str(exited)!r}).touch()"
    )
    owner_code = (
        "import subprocess,time; "
        f"child=subprocess.Popen({[sys.executable, '-c', child_code]!r},stdin=subprocess.PIPE); "
        "time.sleep(60)"
    )
    # No venv launcher/job around this stdlib-only owner: the test must exercise
    # pipe EOF, not a launcher's automatic descendant termination.
    owner = subprocess.Popen(
        [getattr(sys, "_base_executable", sys.executable), "-c", owner_code]
    )
    identity = None
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                identity = psutil.Process(json.loads(ready.read_text()))
                break
            except (FileNotFoundError, json.JSONDecodeError):
                time.sleep(0.01)
        assert identity is not None
        owner.kill()  # No Python finally, signal handler, or explicit stdin.close.
        owner.wait(5)
        identity.wait(timeout=10)
        assert exited.exists()
    finally:
        if owner.poll() is None:
            owner.kill()
            owner.wait(5)
        if identity and identity.is_running():
            identity.kill()
            identity.wait(5)


def test_supervised_crash_restarts_after_guarded_descendant_exits(tmp_path):
    from anywhere_computer.http_supervisor import supervise

    count = tmp_path / "count"
    ready = tmp_path / "connector-ready"
    exited = tmp_path / "connector-exited"
    descendant_code = (
        "from anywhere_computer.parent_liveness import watch_parent_pipe; "
        "from pathlib import Path; "
        "stop=watch_parent_pipe(); "
        f"Path({str(ready)!r}).touch(); assert stop.wait(10); Path({str(exited)!r}).touch()"
    )
    code = f'''
import os, subprocess, time
from pathlib import Path
counter = Path({str(count)!r})
number = int(counter.read_text()) + 1 if counter.exists() else 1
counter.write_text(str(number))
if number == 1:
    child = subprocess.Popen({[sys.executable, '-c', descendant_code]!r}, stdin=subprocess.PIPE)
    deadline = time.monotonic() + 8
    while not Path({str(ready)!r}).exists():
        if time.monotonic() >= deadline:
            raise SystemExit(9)
        time.sleep(0.01)
    os._exit(7)
deadline = time.monotonic() + 8
while not Path({str(exited)!r}).exists():
    if time.monotonic() >= deadline:
        raise SystemExit(9)
    time.sleep(0.01)
'''
    result = supervise(
        [getattr(sys, "_base_executable", sys.executable), "-c", code],
        parent_pipe=True, initial_delay=0, restart_limit=1,
    )
    assert result == 0 and count.read_text() == "2"
    assert exited.exists()
