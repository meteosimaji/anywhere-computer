import json

import pytest

from anywhere_computer import cli
from anywhere_computer import release_update as update
from anywhere_computer.github_releases import ReleaseCandidate
from anywhere_computer.models import Reply
from anywhere_computer.release_record import PreparedRelease, save_prepared_release


@pytest.mark.parametrize(('current', 'target', 'busy', 'expected'), [
    ('0.1.0a6', '0.1.0', False, 'applied'),
    ('0.1.0', '0.1.0', False, 'current'),
    ('0.2.0', '0.1.0', False, 'current'),
    ('0.1.0a6', None, False, 'no_stable_release'),
    ('0.1.0a6', '0.1.0', True, 'waiting'),
])
def test_update_cycle_only_installs_newer_idle_stable(
    tmp_path, monkeypatch, current, target, busy, expected,
):
    events = []
    async def status(*args, **kwargs):
        return Reply(operation_id='receipt', state='completed',
                     data={'version': current, 'update_blocked': busy})
    def candidate(platform):
        events.append('lookup')
        return (ReleaseCandidate('v' + target, 'a' * 40, 1, 'fixture.zip', 1, 'b' * 64, 2)
                if target else None)
    def install(selected, parent, control, **kwargs):
        assert selected.tag == 'v' + target and parent == tmp_path / 'portable'
        events.append('install')
        return {'state': 'applied'}
    monkeypatch.setattr(update, 'exchange', status)
    monkeypatch.setattr(update, 'stable_candidate', candidate)
    monkeypatch.setattr(update, 'install_release', install)
    assert update.update_once(tmp_path, verifier=tmp_path / 'gh')['state'] == expected
    assert ('lookup' in events) is not busy
    assert ('install' in events) == (expected == 'applied')


def test_resume_does_not_require_github_or_running_engine(tmp_path, monkeypatch):
    candidate = ReleaseCandidate('v1.2.3', 'a' * 40, 1, 'fixture.zip', 1, 'b' * 64, 2)
    save_prepared_release(tmp_path, PreparedRelease(candidate=candidate,
                                                   installation=str(tmp_path / 'app')))
    def forbidden(*args, **kwargs):
        pytest.fail('resume must not require discovery or a previously running engine')
    def install(selected, *args, **kwargs):
        assert selected == candidate
        return {'state': 'applied'}
    monkeypatch.setattr(update, 'exchange', forbidden)
    monkeypatch.setattr(update, 'stable_candidate', forbidden)
    monkeypatch.setattr(update, 'install_release', install)
    assert update.update_once(tmp_path, verifier=tmp_path / 'gh')['state'] == 'applied'


def test_release_version_ordering_and_unknown_version_rejection():
    versions = ['0.1.0a6', '0.1.0a10', '0.1.0b1', '0.1.0rc1', '0.1.0', '0.2.0', '1.0.0']
    assert sorted(reversed(versions), key=update.release_version_key) == versions
    with pytest.raises(ValueError, match='unsupported'):
        update.release_version_key('local-development')


def test_update_cli_emits_cycle_result(tmp_path, monkeypatch, capsys):
    verifier = tmp_path / 'gh'
    monkeypatch.setattr('sys.argv', ['anywhere', 'update', '--state-dir', str(tmp_path),
                                     '--verifier', str(verifier)])
    def once(control, **kwargs):
        assert control == tmp_path and kwargs['verifier'] == verifier
        return {'state': 'no_stable_release', 'version': '0.1.0a6'}
    monkeypatch.setattr(update, 'update_once', once)
    cli.main()
    assert json.loads(capsys.readouterr().out)['state'] == 'no_stable_release'


def test_connector_recovers_recorded_switch_before_startup_lock(tmp_path, monkeypatch):
    import os
    import subprocess

    from anywhere_computer import connection, release_activation
    from anywhere_computer.locking import ProcessLock
    from anywhere_computer.runtime_selection import (
        RuntimeSelection,
        begin_runtime_update,
        save_runtime_selection,
    )

    app = tmp_path / 'app'
    executable = app / ('runtime/python.exe' if os.name == 'nt' else 'runtime/bin/python3')
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b'fixture runtime; execution is intercepted')
    executable.chmod(0o700)
    runtime_id = 'a' * 64
    selected = RuntimeSelection(executable=str(executable), runtime_id=runtime_id)
    candidate = ReleaseCandidate('v1.2.3', 'b' * 40, 1, 'fixture.zip', 1, 'c' * 64, 2)
    save_prepared_release(tmp_path, PreparedRelease(candidate=candidate, installation=str(app)))
    begin_runtime_update(tmp_path, selected)
    started = []
    async def status(*args, **kwargs):
        if not started:
            raise FileNotFoundError('previous engine has stopped')
        return Reply(operation_id='receipt', state='completed', data={
            'runtime_id': runtime_id, 'version': '1.2.3', 'engine_api_version': 1,
        })
    def start(command, **kwargs):
        assert command[0] == str(executable)
        # This is the candidate's explicit start transaction. The connecting
        # client must not already hold this lock while waiting for the candidate.
        with ProcessLock(tmp_path / 'startup.lock'):
            save_runtime_selection(tmp_path, selected)
        started.append(True)
        return subprocess.CompletedProcess(command, 0)
    def forbidden(*args, **kwargs):
        pytest.fail('recovery must reuse prepared bytes without discovery or download')
    monkeypatch.setattr(release_activation, 'probe_release_runtime', lambda *args: runtime_id)
    monkeypatch.setattr(release_activation, 'exchange', status)
    monkeypatch.setattr(connection, 'exchange', status)
    monkeypatch.setattr(connection, 'local_credential', lambda *args, **kwargs: 'fixture')
    monkeypatch.setattr(connection, 'runtime_identity', lambda: 'd' * 64)
    monkeypatch.setattr(release_activation.subprocess, 'run', start)
    monkeypatch.setattr(release_activation, 'prepare_release', forbidden)
    monkeypatch.setattr(update, 'stable_candidate', forbidden)
    assert connection.ensure_agent(tmp_path)['runtime_id'] == runtime_id
    assert started == [True]
    assert not (tmp_path / 'runtime-update.pending.json').exists()


def test_orphan_pending_record_is_not_silently_replaced(tmp_path, monkeypatch):
    (tmp_path / 'runtime-update.pending.json').write_text('{}', encoding='utf-8')
    def forbidden(*args, **kwargs):
        pytest.fail('unrecorded candidate must not be installed')
    monkeypatch.setattr(update, 'install_release', forbidden)
    with pytest.raises(RuntimeError, match='no prepared release'):
        update.resume_pending_release(tmp_path)
