import json
import plistlib
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest
from keyring.errors import KeyringError

from anywhere_computer import remote_service, watch_status
from anywhere_computer.autostart import startup_definition
from anywhere_computer.client_tokens import CredentialStoreUnavailable
from anywhere_computer.http_supervisor import supervise


@pytest.mark.parametrize('code', [0, 130, 1])
def test_persistent_child_exit_cools_down_and_recovers(tmp_path, monkeypatch, code):
    calls = []
    vault = object()
    monkeypatch.setattr(remote_service, 'secure_backend', lambda: vault)

    def once(directory, **options):
        assert options['vault'] is vault
        calls.append(directory)
        if len(calls) == 2:
            raise KeyboardInterrupt
        return code

    sleeps = []
    monkeypatch.setattr(remote_service, '_watch_remote_once', once)
    monkeypatch.setattr(remote_service.time, 'sleep', sleeps.append)
    assert remote_service.watch_remote(tmp_path, persistent=True) == 130
    assert len(calls) == 2 and sleeps == [60]
    records = [json.loads(p.read_text()) for p in tmp_path.glob('*.event-*.json')]
    assert any(r['event'] == ('unexpected_child_exit' if code in {0, 130} else 'cooldown')
               for r in records)


def test_persistent_backend_initialization_and_read_recover(tmp_path, monkeypatch):
    backend = Mock(side_effect=[KeyringError('fixture'), object()])
    monkeypatch.setattr(remote_service, 'secure_backend', backend)
    once = Mock(side_effect=[CredentialStoreUnavailable('fixture'), KeyboardInterrupt])
    monkeypatch.setattr(remote_service, '_watch_remote_once', once)
    sleeps = []
    monkeypatch.setattr(remote_service.time, 'sleep', sleeps.append)
    assert remote_service.watch_remote(tmp_path, persistent=True) == 130
    assert backend.call_count == 2 and once.call_count == 2 and sleeps == [60, 60]


def test_permanent_failure_blocks_without_repeated_credentials(tmp_path, monkeypatch):
    monkeypatch.setattr(remote_service, 'secure_backend', lambda: object())
    once = Mock(side_effect=ValueError('fixture credential missing'))
    monkeypatch.setattr(remote_service, '_watch_remote_once', once)
    sleep = Mock(side_effect=[None, KeyboardInterrupt])
    monkeypatch.setattr(remote_service.time, 'sleep', sleep)
    assert remote_service.watch_remote(tmp_path, persistent=True) == 130
    assert once.call_count == 1
    assert any(json.loads(p.read_text())['event'] == 'blocked'
               for p in tmp_path.glob('*.event-*.json'))


def test_supervisor_survives_broken_console(tmp_path, monkeypatch):
    def broken(*args, **kwargs):
        raise BrokenPipeError('fixture')
    monkeypatch.setattr('builtins.print', broken)
    assert supervise([sys.executable, '-c', 'pass'], status_path=tmp_path/'watch.json') == 0


def test_history_is_bounded_and_identifies_runtime(tmp_path):
    for _ in range(100):
        watch_status.save_watch_observation(tmp_path/'watch.json', 'cooldown', 0, 5, None)
    records = list(tmp_path.glob('*.event-*.json'))
    assert len(records) == 64
    value = json.loads(records[0].read_text())
    assert len(value['generation']) == 32 and len(value['runtime_id']) == 64
    assert value['process_created_at'] > 0


@pytest.mark.parametrize('platform', ['darwin', 'linux', 'win32'])
def test_persistent_definition_preserves_legacy_bytes(tmp_path, platform):
    options = dict(platform=platform, home=tmp_path, executable=str(Path(sys.executable)),
                   user='fixture')
    old = startup_definition(tmp_path/'state', **options)
    explicit = startup_definition(tmp_path/'state', policy_version=1, **options)
    new = startup_definition(tmp_path/'state', policy_version=2, **options)
    assert old == explicit and old.name == new.name
    text = new.content.decode('utf-16' if platform == 'win32' else 'utf-8')
    assert '--persistent' in text
    if platform == 'darwin':
        assert plistlib.loads(new.content)['KeepAlive'] is True
        assert plistlib.loads(new.content)['ThrottleInterval'] == 60
    elif platform == 'linux':
        assert 'Restart=always' in text and 'RestartSec=60' in text
    else:
        assert '<RestartOnFailure>' in text
