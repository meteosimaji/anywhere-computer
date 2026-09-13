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
        assert "fixture" not in json.dumps(second.model_dump())
        assert not running.done()
    finally:
        stop.set()
        await asyncio.wait_for(running, 5)
