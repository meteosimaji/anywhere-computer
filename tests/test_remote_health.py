import asyncio
from types import SimpleNamespace

import pytest
from test_http_service import configured as configured

from anywhere_computer import http_diagnostics, remote_health
from anywhere_computer.http_service import http_service


async def cancel_monitor(task):
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.parametrize("mode", ["absent", "disabled", "changed"])
async def test_monitor_does_not_contact_unapproved_resource(tmp_path, monkeypatch, mode):
    directory = tmp_path / "state"
    monkeypatch.setattr(
        remote_health, "load_http_config", lambda _: SimpleNamespace(resource="old")
    )
    if mode != "absent":
        remote_health.configure_public_monitor(directory, enabled=mode == "changed")
    monkeypatch.setattr(
        remote_health, "load_http_config", lambda _: SimpleNamespace(resource="new")
    )
    calls = []

    async def probe(*args, **kwargs):
        calls.append(args)
        raise AssertionError("No endpoint has consent for this probe")

    monkeypatch.setattr(http_diagnostics, "diagnose_remote", probe)
    task = asyncio.create_task(remote_health.monitor_public_health(directory, interval=0.01))
    try:
        await asyncio.sleep(0.04)
        assert not task.done()
        assert calls == []
        assert not (directory / "remote-health-observation.json").exists()
        if mode == "absent":
            assert not directory.exists()
    finally:
        await cancel_monitor(task)


async def test_monitor_records_outage_and_recovery(tmp_path, monkeypatch):
    monkeypatch.setattr(
        remote_health, "load_http_config", lambda _: SimpleNamespace(resource="same")
    )
    remote_health.configure_public_monitor(tmp_path, enabled=True)
    states = iter(["metadata_reachable", "unreachable", "metadata_reachable"])
    recorded = []
    complete = asyncio.Event()
    original_write = remote_health._write_monitor_file

    def save(path, observation):
        original_write(path, observation)
        recorded.append(observation.consecutive_failures)
        if len(recorded) == 3:
            complete.set()

    async def probe(*args, **kwargs):
        assert kwargs == {"probe_public": True, "expected_resource": "same"}
        return {"loopback": {"state": "metadata_reachable"}, "public": {"state": next(states)}}

    monkeypatch.setattr(remote_health, "_write_monitor_file", save)
    monkeypatch.setattr(http_diagnostics, "diagnose_remote", probe)
    task = asyncio.create_task(remote_health.monitor_public_health(tmp_path, interval=0.01))
    try:
        await asyncio.wait_for(complete.wait(), 2)
        assert recorded == [0, 1, 0]
        status = remote_health.public_monitor_status(tmp_path)
        assert status["history_state"] == "recorded"
        assert status["current_connectivity"] == "unverified"
        assert status["authenticated"] is False
    finally:
        await cancel_monitor(task)


async def test_disable_discards_inflight_result_and_cancellation_reaches_probe(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        remote_health, "load_http_config", lambda _: SimpleNamespace(resource="same")
    )
    remote_health.configure_public_monitor(tmp_path, enabled=True)
    started, release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def probe(*args, **kwargs):
        started.set()
        try:
            await release.wait()
        finally:
            cancelled.set()
        return {
            "loopback": {"state": "metadata_reachable"},
            "public": {"state": "metadata_reachable"},
        }

    monkeypatch.setattr(http_diagnostics, "diagnose_remote", probe)
    task = asyncio.create_task(remote_health.monitor_public_health(tmp_path, interval=0.01))
    await asyncio.wait_for(started.wait(), 2)
    remote_health.configure_public_monitor(tmp_path, enabled=False)
    release.set()
    await asyncio.wait_for(cancelled.wait(), 2)
    await asyncio.sleep(0.03)
    assert not (tmp_path / "remote-health-observation.json").exists()
    await cancel_monitor(task)

    release.clear()
    started.clear()
    cancelled.clear()
    remote_health.configure_public_monitor(tmp_path, enabled=True)
    task = asyncio.create_task(remote_health.monitor_public_health(tmp_path, interval=0.01))
    await asyncio.wait_for(started.wait(), 2)
    await cancel_monitor(task)
    assert cancelled.is_set()


def test_corrupt_monitor_history_is_not_disclosed(tmp_path):
    (tmp_path / "remote-health-observation.json").write_text("secret private malformed data")
    status = remote_health.public_monitor_status(tmp_path)
    assert status["history_state"] == "unreadable"
    assert "secret" not in str(status)


async def test_diagnosis_rejects_changed_resource_before_public_contact(
    configured, tmp_path, monkeypatch
):
    _, owner = configured
    original = http_diagnostics._metadata
    contacts = []

    async def metadata(port, **kwargs):
        contacts.append(port)
        assert port != 443
        return await original(port, **kwargs)

    monkeypatch.setattr(http_diagnostics, "_metadata", metadata)
    async with http_service(tmp_path, credentials=owner):
        result = await http_diagnostics.diagnose_remote(
            tmp_path,
            probe_public=True,
            expected_resource="https://previous.example/mcp",
        )
    assert result["state"] == "configuration_changed"
    assert result["public"]["state"] == "resource_mismatch"
    assert len(contacts) == 1


async def test_real_loopback_survives_public_outage_and_failed_history_save(
    configured,
    tmp_path,
    monkeypatch,
    capsys,
):
    config, owner = configured
    remote_health.configure_public_monitor(tmp_path, enabled=True)
    original_metadata = http_diagnostics._metadata
    original_save = remote_health._write_monitor_file
    attempts, observations = [], []
    complete = asyncio.Event()

    async def metadata(port, **kwargs):
        if port != 443:
            return await original_metadata(port, **kwargs)
        attempts.append(port)
        if len(attempts) <= 2:
            raise OSError("synthetic private network detail")
        return {"resource": config.resource}

    def save(path, observation):
        observations.append(observation)
        if len(observations) == 1:
            raise OSError("synthetic private disk detail")
        original_save(path, observation)
        if len(observations) == 3:
            complete.set()

    monkeypatch.setattr(http_diagnostics, "_metadata", metadata)
    monkeypatch.setattr(remote_health, "_write_monitor_file", save)
    async with http_service(tmp_path, credentials=owner):
        task = asyncio.create_task(remote_health.monitor_public_health(tmp_path, interval=0.01))
        try:
            await asyncio.wait_for(complete.wait(), 3)
            assert [o.consecutive_failures for o in observations] == [1, 2, 0]
            assert all(o.loopback_state == "metadata_reachable" for o in observations)
            assert (await http_diagnostics.diagnose_http(tmp_path))["state"] == "metadata_reachable"
        finally:
            await cancel_monitor(task)
    assert "private" not in capsys.readouterr().out
