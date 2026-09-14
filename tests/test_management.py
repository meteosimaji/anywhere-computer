import asyncio
import json
import sqlite3

import pytest

from anywhere_computer.connection import serve
from anywhere_computer.devices import DeviceStore
from anywhere_computer.management import ManagementController
from anywhere_computer.remote_setup import plan_remote_setup


async def test_empty_management_does_not_initialize_or_contact_remote(tmp_path):
    directory = tmp_path / "not-installed"
    result = await ManagementController(directory).snapshot()
    assert result.engine_state == "stopped"
    assert result.setup.phase == "new"
    assert result.remote_connection == "not_checked"
    assert result.device_registry_state == "absent"
    assert not result.automatic_stable_updates
    assert result.changed is False
    assert not directory.exists()


async def test_management_start_reports_observation_not_success_shaped_ack(tmp_path, monkeypatch):
    calls = []

    def acknowledged(directory):
        calls.append(directory)
        return {"state": "ready"}

    monkeypatch.setattr("anywhere_computer.management.ensure_agent", acknowledged)
    result = await ManagementController(tmp_path).start()
    assert calls == [tmp_path]
    assert result.state == "not_confirmed"
    assert result.snapshot.engine_state == "stopped"


async def test_management_start_reconciles_error_without_repeating(tmp_path, monkeypatch):
    calls = []

    def failed(directory):
        calls.append(directory)
        raise RuntimeError("private error payload")

    monkeypatch.setattr("anywhere_computer.management.ensure_agent", failed)
    result = await ManagementController(tmp_path).start()
    assert result.state == "not_confirmed"
    assert calls == [tmp_path]
    assert "private error payload" not in result.model_dump_json()


async def test_saved_setup_and_cached_device_do_not_claim_live_readiness(tmp_path):
    controller = ManagementController(tmp_path)
    plan = await plan_remote_setup(resource="https://fixture.example/mcp")
    review = controller.setup.review(plan)
    await controller.setup.confirm(review.plan_id)
    store = DeviceStore(tmp_path)
    try:
        device = store.add("Windows 検証", "fixture-vm")
        store.record(device["device_id"], "ready", "private-detail-not-for-ui")
    finally:
        store.close()
    original = (tmp_path / "devices.sqlite3").read_bytes()
    result = await controller.snapshot()
    assert result.setup.phase == "configured"
    assert result.engine_state == "stopped"
    assert result.remote_connection == "not_checked"
    assert result.devices[0].last_observed_state == "ready"
    assert result.devices[0].live_state == "not_checked"
    assert result.devices[0].checked_at is not None
    assert "private-detail" not in result.model_dump_json()
    assert (tmp_path / "devices.sqlite3").read_bytes() == original


def test_read_only_registry_cannot_write_or_create(tmp_path):
    with pytest.raises(sqlite3.OperationalError):
        DeviceStore(tmp_path, read_only=True)
    assert not list(tmp_path.iterdir())
    store = DeviceStore(tmp_path)
    store.close()
    reader = DeviceStore(tmp_path, read_only=True)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            reader.add("forbidden", "fixture-vm")
    finally:
        reader.close()


async def test_unsupported_registry_is_not_migrated_and_does_not_hide_other_state(tmp_path):
    database = tmp_path / "devices.sqlite3"
    with sqlite3.connect(database) as db:
        db.execute("PRAGMA user_version=99")
    original = database.read_bytes()
    result = await ManagementController(tmp_path).snapshot()
    assert result.device_registry_state == "unavailable"
    assert result.engine_state == "stopped"
    assert result.devices == []
    assert database.read_bytes() == original


async def test_management_reads_live_engine_and_never_restarts_it(tmp_path, monkeypatch):
    monkeypatch.setattr("anywhere_computer.diagnostics.local_credential", lambda _: "fixture")
    monkeypatch.setattr("anywhere_computer.connection.local_credential", lambda *a, **k: "fixture")
    stop = asyncio.Event()
    running = asyncio.create_task(serve(tmp_path, credential="fixture", shutdown=stop))
    try:
        for _ in range(300):
            if (tmp_path / "agent.json").exists():
                break
            if running.done():
                await running
            await asyncio.sleep(.01)
        controller = ManagementController(tmp_path)
        first = await controller.snapshot()
        assert first.engine_state == "ready"
        assert first.capabilities["files"] is True
        assert first.capabilities["gui"] is False
        assert first.active_resources["terminal_sessions"] == 0
        assert first.version and first.runtime_id and first.instance_id
        second = await controller.snapshot()
        assert second.instance_id == first.instance_id
        assert second.observed_at >= first.observed_at
        started = await controller.start()
        assert started.state == "ready"
        assert started.snapshot.instance_id == first.instance_id
        assert "fixture" not in json.dumps(second.model_dump())
        assert not running.done()
    finally:
        stop.set()
        await asyncio.wait_for(running, 5)


async def test_startup_read_does_not_create_state(tmp_path):
    directory = tmp_path / 'absent'
    value = await ManagementController(directory).startup()
    assert value.state == 'not_installed'
    assert value.mode is None
    assert not directory.exists()


@pytest.mark.parametrize('enabled', [True, False])
async def test_management_never_changes_remote_startup(tmp_path, monkeypatch, enabled):
    monkeypatch.setattr('anywhere_computer.management.startup_status', lambda _: {
        'state': 'registered', 'mode': 'remote', 'native_running': True,
    })
    def forbidden(*args, **kwargs):
        pytest.fail('Remote startup must not be changed by local management')
    monkeypatch.setattr('anywhere_computer.management.install_startup', forbidden)
    monkeypatch.setattr('anywhere_computer.management.uninstall_startup', forbidden)
    result = await ManagementController(tmp_path).set_local_startup(enabled)
    assert result.state == 'unsupported_mode'


async def test_startup_lost_ack_reconciles_once_without_leaking_error(tmp_path, monkeypatch):
    states = iter([{'state': 'not_installed'}, {'state': 'registered', 'mode': 'local'}])
    monkeypatch.setattr('anywhere_computer.management.startup_status', lambda _: next(states))
    calls = []
    def install(directory, *, mode):
        calls.append((directory, mode))
        raise RuntimeError('private-error-body')
    monkeypatch.setattr('anywhere_computer.management.install_startup', install)
    result = await ManagementController(tmp_path).set_local_startup(True)
    assert result.state == 'confirmed'
    assert calls == [(tmp_path, 'local')]
    assert 'private-error-body' not in result.model_dump_json()


async def test_unreadable_startup_never_attempts_mutation(tmp_path, monkeypatch):
    def failure(_):
        raise ValueError('private-corrupt-receipt')
    monkeypatch.setattr('anywhere_computer.management.startup_status', failure)
    monkeypatch.setattr(
        'anywhere_computer.management.install_startup', lambda *a, **k: pytest.fail(),
    )
    result = await ManagementController(tmp_path).set_local_startup(True)
    assert result.state == 'not_confirmed'
    assert result.startup.state == 'unavailable'
    assert 'private-corrupt-receipt' not in result.model_dump_json()


@pytest.mark.parametrize('transport', ['ssh', 'http'])
async def test_explicit_device_check_uses_existing_probe_and_hides_details(
    tmp_path, monkeypatch, transport,
):
    store = DeviceStore(tmp_path)
    device = (store.add_http('Windows', 'https://fixture.example/mcp', 'test', 'test')
              if transport == 'http' else store.add('Windows', 'fixture-vm'))
    store.close()
    calls = []

    def probe(self, identity):
        calls.append(identity)
        return self.record(identity, 'ready', 'private diagnostic text')

    async def probe_http(self, identity):
        return probe(self, identity)

    monkeypatch.setattr(DeviceStore, 'probe', probe)
    monkeypatch.setattr(DeviceStore, 'probe_http', probe_http)
    result = await ManagementController(tmp_path).check_device(device['device_id'])
    assert calls == [device['device_id']]
    assert result.state == 'ready'
    assert result.evidence == ('authorized_catalog' if transport == 'http' else 'agent_status')
    assert 'private diagnostic' not in result.model_dump_json()
    snapshot = await ManagementController(tmp_path).snapshot()
    assert snapshot.devices[0].live_state == 'not_checked'


async def test_device_check_does_not_create_missing_registry(tmp_path):
    directory = tmp_path / 'absent'
    controller = ManagementController(directory)
    for identity in ['bad', 'f' * 32]:
        with pytest.raises(ValueError):
            await controller.check_device(identity)
    assert not directory.exists()


def test_device_check_cli_returns_observation_without_falling_through(
    tmp_path, monkeypatch, capsys,
):
    import sys

    from anywhere_computer.cli import main

    store = DeviceStore(tmp_path)
    device = store.add('Offline Windows', 'fixture-vm')
    store.close()

    def probe(self, identity):
        return self.record(identity, 'unreachable', 'synthetic offline result')

    monkeypatch.setattr(DeviceStore, 'probe', probe)
    monkeypatch.setattr(sys, 'argv', ['anywhere', 'management-device-check', '--device',
                                    device['device_id'], '--state-dir', str(tmp_path)])
    main()
    output = capsys.readouterr()
    assert output.err == ''
    assert json.loads(output.out)['state'] == 'unreachable'
    assert not (tmp_path / 'agent.json').exists()
