import sys

import pytest

from anywhere_computer.connection import ensure_agent
from anywhere_computer.models import Reply
from anywhere_computer.runtime_selection import (
    RuntimeSelection,
    load_runtime_selection,
    save_runtime_selection,
)


def test_missing_selection_and_atomic_round_trip(tmp_path):
    assert load_runtime_selection(tmp_path) is None
    selected = RuntimeSelection(executable=sys.executable, runtime_id='a' * 64)
    save_runtime_selection(tmp_path, selected)
    assert load_runtime_selection(tmp_path) == selected
    assert not list(tmp_path.glob('.runtime-selection-*'))


@pytest.mark.parametrize('raw', ['{', '{}', ' ' * 8193,
    '{"executable":"relative","runtime_id":"' + 'a' * 64 + '"}',
    '{"executable":"/nonexistent-anywhere-python","runtime_id":"' + 'a' * 64 + '"}'])
def test_invalid_selection_never_bootstraps_bundled_runtime(tmp_path, monkeypatch, raw):
    (tmp_path / 'runtime-selection.json').write_text(raw)
    def forbidden(*args, **kwargs):
        pytest.fail('Must not launch a fallback runtime')
    monkeypatch.setattr('anywhere_computer.connection.subprocess.Popen', forbidden)
    with pytest.raises(ValueError):
        ensure_agent(tmp_path)


def test_connector_bootstraps_selected_interpreter_not_its_own(tmp_path, monkeypatch):
    interpreter = tmp_path / 'selected-python'
    interpreter.write_bytes(b'fixture; subprocess launch is intercepted')
    interpreter.chmod(0o700)
    selected = RuntimeSelection(executable=str(interpreter), runtime_id='a' * 64)
    save_runtime_selection(tmp_path, selected)
    calls = []
    async def exchange(directory, tool, **kwargs):
        if not calls:
            raise ConnectionRefusedError()
        return Reply(operation_id='0' * 32, state='completed', data={
            'runtime_id': selected.runtime_id, 'engine_api_version': 1,
        })
    monkeypatch.setattr('anywhere_computer.connection.runtime_identity', lambda: 'b' * 64)
    monkeypatch.setattr('anywhere_computer.connection.local_credential', lambda *a, **kw: 'fixture')
    monkeypatch.setattr('anywhere_computer.connection.exchange', exchange)
    monkeypatch.setattr('anywhere_computer.connection.subprocess.Popen',
                        lambda command, **kwargs: calls.append(command))
    assert ensure_agent(tmp_path)['runtime_id'] == selected.runtime_id
    assert calls == [[str(interpreter), '-I', '-X', 'utf8', '-m',
                      'anywhere_computer', 'serve', '--state-dir', str(tmp_path)]]
    assert load_runtime_selection(tmp_path) == selected


@pytest.mark.parametrize('fail_save', [False, True])
def test_explicit_start_records_ready_runtime_without_restarting(tmp_path, monkeypatch, fail_save):
    import anywhere_computer.connection as connection

    async def exchange(directory, tool, **kwargs):
        assert tool == '__status'
        return Reply(operation_id='0' * 32, state='completed', data={
            'runtime_id': 'a' * 64, 'engine_api_version': 1,
        })
    def forbidden(*args, **kwargs):
        pytest.fail('A ready engine must not be restarted')
    def unavailable(*args, **kwargs):
        raise OSError('fixture disk failure')
    monkeypatch.setattr(connection, 'runtime_identity', lambda: 'a' * 64)
    monkeypatch.setattr(connection, 'local_credential', lambda *a, **kw: 'fixture')
    monkeypatch.setattr(connection, 'exchange', exchange)
    monkeypatch.setattr(connection.subprocess, 'Popen', forbidden)
    if fail_save:
        monkeypatch.setattr('anywhere_computer.runtime_selection.tempfile.mkstemp', unavailable)
        with pytest.raises(RuntimeError, match='stage'):
            ensure_agent(tmp_path, replace_idle=True)
        assert load_runtime_selection(tmp_path) is None
    else:
        ensure_agent(tmp_path, replace_idle=True)
        assert load_runtime_selection(tmp_path) == RuntimeSelection(
            executable=sys.executable, runtime_id='a' * 64,
        )


def test_interrupted_update_requires_same_installation_and_completes(tmp_path):
    from anywhere_computer.runtime_selection import begin_runtime_update

    old = RuntimeSelection(executable=sys.executable, runtime_id='a' * 64)
    new = RuntimeSelection(executable=sys.executable, runtime_id='b' * 64)
    save_runtime_selection(tmp_path, old)
    begin_runtime_update(tmp_path, new)
    with pytest.raises(RuntimeError, match='incomplete'):
        load_runtime_selection(tmp_path)
    with pytest.raises(RuntimeError, match='incomplete'):
        load_runtime_selection(tmp_path, recovery=old)
    assert load_runtime_selection(tmp_path, recovery=new) == old
    # The matching installer can finish after an interruption without reverting data.
    begin_runtime_update(tmp_path, new)
    save_runtime_selection(tmp_path, new)
    assert load_runtime_selection(tmp_path) == new
    assert not (tmp_path / 'runtime-update.pending.json').exists()


def test_busy_shutdown_race_clears_pending_intent(tmp_path, monkeypatch):
    import anywhere_computer.connection as connection

    calls = []
    async def exchange(directory, tool, **kwargs):
        calls.append(tool)
        return Reply(operation_id='0' * 32,
                     state='failed' if tool == '__stop' else 'completed',
                     data={'runtime_id': 'a' * 64})
    monkeypatch.setattr(connection, 'runtime_identity', lambda: 'b' * 64)
    monkeypatch.setattr(connection, 'local_credential', lambda *a, **kw: 'fixture')
    monkeypatch.setattr(connection, 'exchange', exchange)
    with pytest.raises(RuntimeError, match='became busy'):
        ensure_agent(tmp_path, replace_idle=True)
    assert calls == ['__status', '__stop']
    assert load_runtime_selection(tmp_path) is None


@pytest.mark.parametrize('live', [False, True])
def test_owner_pipe_timeout_checks_process_before_starting(tmp_path, monkeypatch, live):
    import os

    import psutil

    from anywhere_computer import connection
    from anywhere_computer.owner_json_pipe import OwnerPipeTimeout

    launches = []
    async def exchange(directory, tool, **kwargs):
        if not launches:
            raise OwnerPipeTimeout('Timed out connecting to owner pipe')
        return Reply(operation_id='0' * 32, state='completed', data={'runtime_id': 'a' * 64})
    monkeypatch.setattr(connection, 'exchange', exchange)
    monkeypatch.setattr(connection, 'runtime_identity', lambda: 'a' * 64)
    monkeypatch.setattr(connection, 'load_endpoint', lambda _: {
        'pid': os.getpid() if live else -1,
        'process_started': psutil.Process().create_time() if live else -1,
    })
    def credential(*args, **kwargs):
        assert not live, 'Must not access credentials while an existing agent is alive'
        return 'synthetic-test-credential'
    monkeypatch.setattr(connection, 'local_credential', credential)
    monkeypatch.setattr(connection.subprocess, 'Popen',
                        lambda command, **kwargs: launches.append(command))
    if live:
        with pytest.raises(RuntimeError, match='process exists but is not responding'):
            ensure_agent(tmp_path, replace_idle=True)
        assert not launches
    else:
        assert ensure_agent(tmp_path, replace_idle=True)['runtime_id'] == 'a' * 64
        assert len(launches) == 1


def test_owner_pipe_identity_failure_never_attempts_startup(tmp_path, monkeypatch):
    from anywhere_computer import connection
    from anywhere_computer.owner_json_pipe import OwnerPipeIdentityError

    async def exchange(*args, **kwargs):
        raise OwnerPipeIdentityError('fixture mismatch')
    def forbidden(*args, **kwargs):
        pytest.fail('Identity rejection must not bootstrap or use another credential path')
    monkeypatch.setattr(connection, 'exchange', exchange)
    monkeypatch.setattr(connection, 'local_credential', forbidden)
    monkeypatch.setattr(connection.subprocess, 'Popen', forbidden)
    with pytest.raises(OwnerPipeIdentityError):
        ensure_agent(tmp_path, replace_idle=True)
