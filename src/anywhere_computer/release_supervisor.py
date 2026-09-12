"""Run stable-update cycles without blocking the remote service's event loop."""

import asyncio
import json
import math
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import psutil

from .runtime_launch import python_module_command
from .state import prepare_directory
from .watch_status import lifecycle_print


def automatic_updates_enabled(directory: Path) -> bool:
    """Missing or invalid opt-in never enables background downloads."""
    path = directory / 'automatic-updates.json'
    try:
        if path.is_symlink() or not path.is_file():
            return False
        with path.open('rb') as source:
            raw = source.read(1025)
        if len(raw) > 1024:
            return False
        value = json.loads(raw)
        return (isinstance(value, dict) and set(value) == {'version', 'enabled'}
                and type(value['version']) is int and value['version'] == 1
                and value['enabled'] is True)
    except (OSError, ValueError):
        return False


def configure_automatic_updates(directory: Path, *, enabled: bool) -> dict[str, object]:
    """Store the explicit preference; the service reads it on its next start."""
    prepare_directory(directory)
    descriptor, name = tempfile.mkstemp(prefix='.automatic-updates-', dir=directory)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as target:
            json.dump({'version': 1, 'enabled': enabled}, target)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, directory / 'automatic-updates.json')
    finally:
        temporary.unlink(missing_ok=True)
    return {'automatic_updates': enabled, 'restart_required': True}


def stop_update_child(child: subprocess.Popen[bytes]) -> None:
    """Signal the updater group; selected engines start in a separate process group."""
    if sys.platform == 'win32':
        from .http_supervisor import _stop_child

        _stop_child(child)
        return
    if child.poll() is not None:
        return
    owned = []
    try:
        for process in psutil.Process(child.pid).children(recursive=True):
            try:
                if os.getpgid(process.pid) == child.pid:
                    owned.append(process)
            except ProcessLookupError:
                pass
    except psutil.NoSuchProcess:
        pass
    try:
        os.killpg(child.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        child.wait(timeout=5)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait(timeout=5)
    # Cached Process identities guard PID reuse after the launcher is reaped.
    for process in owned:
        try:
            if process.is_running():
                process.kill()
                process.wait(timeout=5)
        except psutil.NoSuchProcess:
            pass


async def monitor_release_updates(
    control: Path, *,
    check_interval: float = 3600, retry_interval: float = 60, cycle_timeout: float = 900,
) -> None:
    """The service owns the updater; cancellation stops its helper process tree.

    A selected engine owns its own lifetime and is deliberately not stopped when
    this HTTP entrance closes. Update errors never terminate the HTTP service.
    """
    if any(not math.isfinite(value) or value <= 0
           for value in (check_interval, retry_interval, cycle_timeout)):
        raise ValueError('Update monitor intervals must be positive')
    previous: str | None = None
    while True:
        state = 'verifier_unavailable'
        verifier = shutil.which('gh')
        if verifier is not None:
            child: subprocess.Popen[bytes] | None = None
            try:
                flags = 0
                if sys.platform == 'win32':
                    flags = subprocess.CREATE_NEW_PROCESS_GROUP
                child = subprocess.Popen(
                    python_module_command(
                        'anywhere_computer', 'update', '--state-dir', str(control),
                        '--verifier', str(Path(verifier).absolute()),
                    ),
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    start_new_session=os.name != 'nt',
                    creationflags=flags,
                )
                deadline = time.monotonic() + cycle_timeout
                while child.poll() is None and time.monotonic() < deadline:
                    await asyncio.sleep(0.1)
                state = 'failed'
                if child.poll() == 0 and child.stdout is not None:
                    raw = child.stdout.read(16385)
                    if len(raw) <= 16384:
                        result = json.loads(raw)
                        reported = result.get('state') if isinstance(result, dict) else None
                        if isinstance(reported, str) and reported in {
                            'current', 'applied', 'waiting', 'no_stable_release',
                        }:
                            state = reported
                elif child.poll() is None:
                    state = 'timed_out'
            except (OSError, ValueError, RuntimeError):
                state = 'failed'
            finally:
                if child is not None:
                    try:
                        stop_update_child(child)
                    finally:
                        if child.stdout is not None:
                            child.stdout.close()
        if state != previous:
            lifecycle_print({'release_update_state': state})
            previous = state
        await asyncio.sleep(retry_interval if state in {'waiting', 'failed', 'timed_out'}
                            else check_interval)
