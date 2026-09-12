import subprocess

import pytest

from anywhere_computer import release_activation as activation
from anywhere_computer.github_releases import ReleaseCandidate
from anywhere_computer.models import Reply
from anywhere_computer.release_record import read_prepared_release


@pytest.mark.parametrize('scenario', ['busy', 'current', 'applied', 'failed', 'timeout', 'wrong'])
def test_activation_observes_engine_and_preserves_installation(tmp_path, monkeypatch, scenario):
    app, control = tmp_path / 'prepared', tmp_path / 'control'
    app.mkdir()
    control.mkdir()
    marker = app / 'retain-on-uncertain-result'
    marker.write_bytes(b'candidate')
    runtime_id = 'a' * 64
    monkeypatch.setattr(activation, 'probe_release_runtime', lambda *args: runtime_id)
    calls = []
    async def exchange(directory, tool, **kwargs):
        assert directory == control and tool == '__status'
        calls.append('status')
        after = 'start' in calls
        return Reply(operation_id='b' * 32, state='completed', data={
            'runtime_id': runtime_id if scenario == 'current' or after and scenario != 'wrong'
            else 'c' * 64,
            'version': '1.2.3', 'instance_id': 'new' if after else 'old',
            'update_blocked': scenario == 'busy',
        })
    def run(command, **kwargs):
        calls.append('start')
        assert command[1:] == ['-I', '-m', 'anywhere_computer', 'start',
                               '--state-dir', str(control)]
        assert kwargs['timeout'] == 45 and 'shell' not in kwargs
        if scenario == 'timeout':
            raise subprocess.TimeoutExpired(command, 45)
        return subprocess.CompletedProcess(command, 1 if scenario == 'failed' else 0)
    monkeypatch.setattr(activation, 'exchange', exchange)
    monkeypatch.setattr(activation.subprocess, 'run', run)
    if scenario in {'failed', 'timeout', 'wrong'}:
        with pytest.raises(RuntimeError):
            activation.activate_release(app, control, '1.2.3')
    else:
        result = activation.activate_release(app, control, '1.2.3')
        assert result['state'] == ('waiting' if scenario == 'busy' else scenario)
    assert marker.read_bytes() == b'candidate'
    assert calls == (['status'] if scenario in {'busy', 'current'} else
                     ['status', 'start'] if scenario in {'failed', 'timeout'} else
                     ['status', 'start', 'status'])


@pytest.mark.parametrize('verified', [False, True])
def test_installation_activates_only_after_preparation(tmp_path, monkeypatch, verified):
    candidate = ReleaseCandidate('v1.2.3', 'a' * 40, 1, 'fixture.zip', 1, 'b' * 64, 2)
    app = tmp_path / 'prepared'
    app.mkdir()
    events = []
    def prepare(selected, parent, control, **kwargs):
        events.append('prepare')
        assert selected == candidate
        if not verified:
            raise ValueError('attestation failed')
        return app
    def activate(installation, control, version):
        assert installation == app and version == '1.2.3'
        events.append('activate')
        return {'state': 'waiting', 'reason': 'active_work'}
    monkeypatch.setattr(activation, 'prepare_release', prepare)
    monkeypatch.setattr(activation, 'activate_release', activate)
    if verified:
        result = activation.install_release(candidate, tmp_path, tmp_path,
                                            verifier=tmp_path / 'gh')
        assert result['state'] == 'waiting' and result['installation'] == str(app)
        assert events == ['prepare', 'activate']
    else:
        with pytest.raises(ValueError, match='attestation'):
            activation.install_release(candidate, tmp_path, tmp_path, verifier=tmp_path / 'gh')
        assert events == ['prepare']


@pytest.mark.parametrize('interrupted', [False, True])
def test_wait_or_interruption_reuses_durable_preparation(tmp_path, monkeypatch, interrupted):
    candidate = ReleaseCandidate('v1.2.3', 'a' * 40, 1, 'fixture.zip', 1, 'b' * 64, 2)
    app = tmp_path / 'prepared'
    app.mkdir()
    preparations, attempts = [], []
    def prepare(*args, **kwargs):
        preparations.append(True)
        return app
    def activate(*args):
        record = read_prepared_release(tmp_path)
        assert record is not None and record.installation == str(app)
        attempts.append(True)
        if len(attempts) == 1:
            if interrupted:
                raise RuntimeError('lost response')
            return {'state': 'waiting'}
        return {'state': 'applied'}
    monkeypatch.setattr(activation, 'prepare_release', prepare)
    monkeypatch.setattr(activation, 'activate_release', activate)
    if interrupted:
        with pytest.raises(RuntimeError, match='lost response'):
            activation.install_release(candidate, tmp_path, tmp_path, verifier=tmp_path / 'gh')
    else:
        assert activation.install_release(candidate, tmp_path, tmp_path,
                                          verifier=tmp_path / 'gh')['state'] == 'waiting'
    assert read_prepared_release(tmp_path).state == 'prepared'
    assert activation.install_release(candidate, tmp_path, tmp_path,
                                      verifier=tmp_path / 'gh')['state'] == 'applied'
    assert len(preparations) == 1 and len(attempts) == 2
    assert read_prepared_release(tmp_path).state == 'applied'


def test_record_save_failure_prevents_activation(tmp_path, monkeypatch):
    candidate = ReleaseCandidate('v1.2.3', 'a' * 40, 1, 'fixture.zip', 1, 'b' * 64, 2)
    monkeypatch.setattr(activation, 'prepare_release', lambda *args, **kwargs: tmp_path)
    def save(*args):
        raise OSError('disk full')
    def activate(*args):
        pytest.fail('activation must not precede durable recording')
    monkeypatch.setattr(activation, 'save_prepared_release', save)
    monkeypatch.setattr(activation, 'activate_release', activate)
    with pytest.raises(OSError, match='disk full'):
        activation.install_release(candidate, tmp_path, tmp_path, verifier=tmp_path / 'gh')


@pytest.mark.parametrize('matching', [False, True])
def test_pending_activation_can_resume_after_engine_stopped(tmp_path, monkeypatch, matching):
    import os

    from anywhere_computer.runtime_selection import RuntimeSelection, begin_runtime_update

    app = tmp_path / 'app'
    executable = app / ('runtime/python.exe' if os.name == 'nt' else 'runtime/bin/python3')
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b'fixture')
    executable.chmod(0o700)
    runtime_id = 'a' * 64
    begin_runtime_update(tmp_path, RuntimeSelection(executable=str(executable),
                         runtime_id=runtime_id if matching else 'b' * 64))
    monkeypatch.setattr(activation, 'probe_release_runtime', lambda *args: runtime_id)
    events = []
    async def exchange(*args, **kwargs):
        if not events:
            raise FileNotFoundError('engine stopped before updater interruption')
        return Reply(operation_id='receipt', state='completed',
                     data={'version': '1.2.3', 'runtime_id': runtime_id})
    def start(command, **kwargs):
        events.append('start')
        return subprocess.CompletedProcess(command, 0)
    monkeypatch.setattr(activation, 'exchange', exchange)
    monkeypatch.setattr(activation.subprocess, 'run', start)
    if matching:
        assert activation.activate_release(app, tmp_path, '1.2.3')['state'] == 'applied'
        assert events == ['start']
    else:
        with pytest.raises(RuntimeError, match='incomplete'):
            activation.activate_release(app, tmp_path, '1.2.3')
        assert events == []
