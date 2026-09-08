import json
import os
import signal
import subprocess
import sys

import psutil
import pytest

from anywhere_computer.http_supervisor import supervise


def test_supervisor_restarts_owned_child_then_stops_on_success(tmp_path, capsys):
    counter = tmp_path / "counter"
    code = (
        "from pathlib import Path; import sys; "
        f"p=Path({str(counter)!r}); n=int(p.read_text())+1 if p.exists() else 1; "
        "p.write_text(str(n)); sys.exit(0 if n==3 else 7)"
    )
    assert supervise([sys.executable, "-c", code], initial_delay=0) == 0
    assert counter.read_text() == "3"
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [r["restart_attempt"] for r in records if "restart_attempt" in r] == [1, 2]


@pytest.mark.parametrize("exit_code,expected_starts", [(1, 3), (0, 1), (130, 1)])
def test_supervisor_retry_budget_and_intentional_exit(capsys, exit_code, expected_starts):
    outcome = supervise(
        [sys.executable, "-c", f"raise SystemExit({exit_code})"], initial_delay=0, restart_limit=2
    )
    assert outcome == exit_code
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert len([r for r in records if "http_child_started" in r]) == expected_starts
    for record in records:
        if "http_child_started" in record:
            assert not psutil.pid_exists(record["http_child_started"])


@pytest.mark.skipif(os.name == "nt", reason="POSIX terminal-signal integration")
def test_interrupt_stops_owned_child_without_restart(tmp_path):
    marker = tmp_path / "child-ready"
    child_code = (
        "import signal,time; from pathlib import Path; "
        f"signal.signal(signal.SIGINT, lambda *args: exit(0)); Path({str(marker)!r}).touch(); "
        "time.sleep(60)"
    )
    parent_code = (
        "from anywhere_computer.http_supervisor import supervise; import sys; "
        f"supervise({[sys.executable, '-c', child_code]!r})"
    )
    parent = subprocess.Popen(
        [sys.executable, "-c", parent_code],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        start_new_session=True,
    )
    owned_pid = None
    try:
        import time

        assert parent.stdout is not None
        owned_pid = json.loads(parent.stdout.readline())["http_child_started"]
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert marker.exists()
        parent.send_signal(signal.SIGINT)
        parent.wait(timeout=15)
        assert not psutil.pid_exists(owned_pid)
        assert "http_restart" not in parent.stdout.read()
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=5)
        if owned_pid is not None and psutil.pid_exists(owned_pid):
            psutil.Process(owned_pid).kill()
        if parent.stdout is not None:
            parent.stdout.close()


def test_supervisor_backoff_is_bounded(monkeypatch, capsys):
    delays = []
    monkeypatch.setattr("anywhere_computer.http_supervisor.time.sleep", delays.append)
    assert supervise([sys.executable, "-c", "raise SystemExit(1)"]) == 1
    assert delays == [1, 2, 4, 8, 16]


def test_stable_child_resets_failure_budget(tmp_path, capsys):
    counter = tmp_path / "stable-counter"
    code = (
        "from pathlib import Path; import sys,time; "
        f"p=Path({str(counter)!r}); n=int(p.read_text())+1 if p.exists() else 1; "
        "p.write_text(str(n)); time.sleep(0.02); sys.exit(0 if n==3 else 1)"
    )
    assert (
        supervise(
            [sys.executable, "-c", code], initial_delay=0, restart_limit=1, stable_seconds=0.01
        )
        == 0
    )
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [r["restart_attempt"] for r in records if "restart_attempt" in r] == [1, 1]
