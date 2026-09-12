import asyncio
import io
import json
import os
import subprocess
import sys

import psutil
import pytest

from anywhere_computer import release_supervisor as supervisor


@pytest.mark.parametrize(('reported', 'expected', 'delay'), [
    ('waiting', 'waiting', 60), ('applied', 'applied', 3600),
    ('no_stable_release', 'no_stable_release', 3600), ({}, 'failed', 60),
])
async def test_monitor_bounded_cycle_and_delay(tmp_path, monkeypatch, reported, expected, delay):
    observed, cleaned = [], []
    class Child:
        stdout = io.BytesIO(json.dumps({'state': reported}).encode())
        def poll(self):
            return 0
    child = Child()
    def launch(command, **options):
        assert command[1:4] == ['-I', '-m', 'anywhere_computer']
        assert command[4] == 'update'
        return child
    async def sleep(seconds):
        assert seconds == delay
        raise asyncio.CancelledError
    monkeypatch.setattr(supervisor.shutil, 'which', lambda name: str(tmp_path / 'gh'))
    monkeypatch.setattr(supervisor.subprocess, 'Popen', launch)
    monkeypatch.setattr(supervisor, 'stop_update_child', lambda value: cleaned.append(value))
    monkeypatch.setattr(supervisor, 'lifecycle_print', observed.append)
    monkeypatch.setattr(supervisor.asyncio, 'sleep', sleep)
    with pytest.raises(asyncio.CancelledError):
        await supervisor.monitor_release_updates(tmp_path)
    assert observed == [{'release_update_state': expected}]
    assert cleaned == [child] and child.stdout.closed


async def test_cancellation_during_update_cleans_owned_child(tmp_path, monkeypatch):
    class Child:
        stdout = io.BytesIO()
        def poll(self):
            return None
    child, cleaned = Child(), []
    async def sleep(seconds):
        raise asyncio.CancelledError
    monkeypatch.setattr(supervisor.shutil, 'which', lambda name: str(tmp_path / 'gh'))
    monkeypatch.setattr(supervisor.subprocess, 'Popen', lambda *args, **kwargs: child)
    monkeypatch.setattr(supervisor, 'stop_update_child', lambda value: cleaned.append(value))
    monkeypatch.setattr(supervisor.asyncio, 'sleep', sleep)
    with pytest.raises(asyncio.CancelledError):
        await supervisor.monitor_release_updates(tmp_path)
    assert cleaned == [child] and child.stdout.closed


async def test_missing_verifier_does_not_attempt_unverified_update(tmp_path, monkeypatch):
    observed = []
    def launch(*args, **kwargs):
        pytest.fail('missing verifier must not launch an update')
    async def sleep(seconds):
        assert seconds == 3600
        raise asyncio.CancelledError
    monkeypatch.setattr(supervisor.shutil, 'which', lambda name: None)
    monkeypatch.setattr(supervisor.subprocess, 'Popen', launch)
    monkeypatch.setattr(supervisor, 'lifecycle_print', observed.append)
    monkeypatch.setattr(supervisor.asyncio, 'sleep', sleep)
    with pytest.raises(asyncio.CancelledError):
        await supervisor.monitor_release_updates(tmp_path)
    assert observed == [{'release_update_state': 'verifier_unavailable'}]


@pytest.mark.skipif(os.name == 'nt', reason='POSIX process-group ownership check')
def test_stopping_real_updater_preserves_independent_engine_group():
    script = (
        'import json, subprocess, sys, time; '
        'helper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], '
        'stdout=subprocess.DEVNULL); '
        'engine = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], '
        'stdout=subprocess.DEVNULL, start_new_session=True); '
        'print(json.dumps([helper.pid, engine.pid]), flush=True); time.sleep(60)'
    )
    child = subprocess.Popen([sys.executable, '-I', '-c', script], start_new_session=True,
                             stdout=subprocess.PIPE)
    helper = engine = None
    try:
        assert child.stdout is not None
        helper_pid, engine_pid = json.loads(child.stdout.readline())
        helper, engine = psutil.Process(helper_pid), psutil.Process(engine_pid)
        supervisor.stop_update_child(child)
        assert child.poll() is not None
        assert not helper.is_running() or helper.status() == psutil.STATUS_ZOMBIE
        assert engine.is_running() and engine.status() != psutil.STATUS_ZOMBIE
    finally:
        for process in (helper, engine):
            if process is not None:
                try:
                    process.kill()
                    process.wait(timeout=5)
                except psutil.NoSuchProcess:
                    pass
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)
        if child.stdout is not None:
            child.stdout.close()


def test_automatic_updates_require_explicit_opt_in(tmp_path):
    from anywhere_computer.release_supervisor import (
        automatic_updates_enabled,
        configure_automatic_updates,
    )

    assert not automatic_updates_enabled(tmp_path)
    assert configure_automatic_updates(tmp_path, enabled=True)['restart_required']
    assert automatic_updates_enabled(tmp_path)
    configure_automatic_updates(tmp_path, enabled=False)
    assert not automatic_updates_enabled(tmp_path)
    for raw in ['{}', '{"version":1,"enabled":1}', '{"version":true,"enabled":true}',
                'invalid', 'x' * 1025]:
        (tmp_path / 'automatic-updates.json').write_text(raw)
        assert not automatic_updates_enabled(tmp_path)
