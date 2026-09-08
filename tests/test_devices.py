import json
import subprocess
import sys

import pytest

from anywhere_computer.devices import DeviceStore


def test_device_registration_rename_restart_and_duplicate_rejection(tmp_path):
    store = DeviceStore(tmp_path)
    try:
        first = store.add(" Windows ラボ ", "windows-lab")
        second = store.add("Linux", "linux-lab")
        assert first["state"] == "unknown"
        assert first["device_id"] != second["device_id"]
        with pytest.raises(ValueError, match="already"):
            store.add("Ｌｉｎｕｘ", "another-host")
        with pytest.raises(ValueError, match="already"):
            store.add("Another", "WINDOWS-LAB")
        updated = store.rename(str(first["device_id"]), "Windows Arm64")
        assert updated["device_id"] == first["device_id"]
    finally:
        store.close()
    restored = DeviceStore(tmp_path)
    try:
        assert len(restored.list()) == 2
        assert restored.get(str(first["device_id"]))["name"] == "Windows Arm64"
        restored.remove(str(second["device_id"]))
        assert len(restored.list()) == 1
        with pytest.raises(ValueError):
            restored.record(str(second["device_id"]), "ready", "late observation")
    finally:
        restored.close()


def test_failed_probe_replaces_old_ready_and_does_not_store_stderr(tmp_path, monkeypatch):
    store = DeviceStore(tmp_path)
    try:
        device = store.add("Lab", "lab")
        identity = str(device["device_id"])
        monkeypatch.setattr("anywhere_computer.devices.ssh_command", lambda host, command: ["ssh"])
        monkeypatch.setattr(
            "anywhere_computer.devices.subprocess.run",
            lambda *a, **kw: subprocess.CompletedProcess(
                a, 0, json.dumps({"state": "completed", "data": {"state": "ready"}}), ""
            ),
        )
        assert store.probe(identity)["state"] == "ready"
        monkeypatch.setattr(
            "anywhere_computer.devices.subprocess.run",
            lambda *a, **kw: subprocess.CompletedProcess(a, 255, "", "private-proxy-output"),
        )
        assert store.probe(identity)["state"] == "unreachable"
        assert "private-proxy-output" not in repr(store.list())
        with store.db:
            store.db.execute("UPDATE devices SET checked=0")
        stale = store.get(identity)
        assert stale["state"] == "unknown" and stale["last_observed_state"] == "unreachable"
    finally:
        store.close()
    assert b"private-proxy-output" not in (tmp_path / "devices.sqlite3").read_bytes()


def test_cli_device_lifecycle(tmp_path):
    def run(*args):
        return subprocess.run(
            [sys.executable, "-m", "anywhere_computer", *args, "--state-dir", str(tmp_path)],
            capture_output=True,
            text=True,
            timeout=10,
        )

    added = run("device-add", "--name", "Windows test", "--ssh-host", "windows-test")
    assert added.returncode == 0, added.stderr
    identity = json.loads(added.stdout)["device_id"]
    assert len(json.loads(run("devices").stdout)["devices"]) == 1
    assert run("device-remove", "--device", identity).returncode == 0
    assert json.loads(run("devices").stdout) == {"devices": []}
    assert run("remote-mcp", "--device", identity).returncode == 1
    assert run("remote-mcp").returncode == 2
