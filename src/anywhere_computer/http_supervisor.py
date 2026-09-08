"""Foreground supervision of one owned HTTP child, with bounded crash retries."""

import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from .http_service import load_http_config
from .locking import ProcessLock
from .owner_credentials import OwnerCredentials


def _stop_child(child: subprocess.Popen[bytes]) -> None:
    if child.poll() is not None:
        return
    try:
        if sys.platform == "win32":
            child.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            child.send_signal(signal.SIGINT)
        child.wait(timeout=10)
    except (OSError, subprocess.TimeoutExpired, KeyboardInterrupt):
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)


def supervise(
    command: Sequence[str],
    *,
    restart_limit: int = 5,
    initial_delay: float = 1,
    stable_seconds: float = 300,
    parent_pipe: bool = False,
) -> int:
    if not command or not 0 <= restart_limit <= 5 or initial_delay < 0 or stable_seconds <= 0:
        raise ValueError("Invalid supervisor policy")
    failures = 0
    while True:
        started = time.monotonic()
        flags = 0
        if sys.platform == "win32":
            flags = subprocess.CREATE_NEW_PROCESS_GROUP
        child = subprocess.Popen(
            command,
            stdin=subprocess.PIPE if parent_pipe else subprocess.DEVNULL,
            start_new_session=os.name != "nt",
            creationflags=flags,
        )
        try:
            print(json.dumps({"http_child_started": child.pid}), flush=True)
            code = child.wait()
        finally:
            try:
                if child.stdin is not None:
                    child.stdin.close()
            finally:
                _stop_child(child)
        lifetime = time.monotonic() - started
        if code in {0, 130}:
            return code
        if lifetime >= stable_seconds:
            failures = 0
        if failures >= restart_limit:
            print(
                json.dumps({"http_watch_stopped": "restart_limit", "exit_code": code}), flush=True
            )
            return 1
        delay = min(initial_delay * 2**failures, 30)
        failures += 1
        print(
            json.dumps(
                {
                    "http_restart_in_seconds": delay,
                    "restart_attempt": failures,
                    "exit_code": code,
                }
            ),
            flush=True,
        )
        time.sleep(delay)


def watch_http(directory: Path) -> int:
    config = load_http_config(directory)
    owner = OwnerCredentials(directory, resource=config.resource, owner=config.owner)
    owner.ensure_initialized()
    # Separate lock protects the watcher; its child still owns the server lock.
    with ProcessLock(directory / "http-watch.lock"):
        with ProcessLock(directory / "http-server.lock"):
            pass  # Refuse a server that was already running; never take it over.
        prior = signal.getsignal(signal.SIGTERM)

        def interrupted(signum: int, frame: object) -> None:
            raise KeyboardInterrupt

        signal.signal(signal.SIGTERM, interrupted)
        try:
            return supervise(
                [
                    sys.executable,
                    "-m",
                    "anywhere_computer.cli",
                    "http-serve",
                    "--state-dir",
                    str(directory.resolve()),
                ]
            )
        finally:
            signal.signal(signal.SIGTERM, prior)
