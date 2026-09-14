import json
import sqlite3
import subprocess
import sys
import time

import pytest
from test_client_tokens import MemoryVault, pair
from test_http_client import http_remote as http_remote

from anywhere_computer import cli
from anywhere_computer.client_tokens import ClientTokens
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


def test_v1_migration_preserves_all_fields_and_reopens(tmp_path):
    original = ("a" * 32, "Lab", "lab", "LAB-SSH", "ready", time.time(), "verified")
    db = sqlite3.connect(tmp_path / "devices.sqlite3")
    with db:
        db.execute(
            "CREATE TABLE devices (id TEXT PRIMARY KEY, name TEXT NOT NULL, "
            "name_key TEXT UNIQUE NOT NULL, ssh_host TEXT UNIQUE NOT NULL COLLATE NOCASE, "
            "observed TEXT NOT NULL DEFAULT 'unknown', checked REAL, detail TEXT)"
        )
        db.execute("INSERT INTO devices VALUES(?,?,?,?,?,?,?)", original)
        db.execute("PRAGMA user_version=1")
    db.close()
    for _ in range(2):
        store = DeviceStore(tmp_path)
        try:
            assert store.db.execute("PRAGMA user_version").fetchone()[0] == 2
            assert (
                store.db.execute(
                    "SELECT id,name,name_key,ssh_host,observed,checked,detail FROM devices"
                ).fetchone()
                == original
            )
            assert store.named(" ＬＡＢ ")["transport"] == "ssh"
            with pytest.raises(ValueError, match="already"):
                store.add_http("lab", "https://example.com/mcp", "client", "profile")
        finally:
            store.close()


def test_failed_migration_rolls_back(tmp_path):
    db = sqlite3.connect(tmp_path / "devices.sqlite3")
    with db:
        # Corrupted v1 data must not leave a half-migrated registry.
        db.execute("CREATE TABLE devices(id,name,name_key,ssh_host,observed,checked,detail)")
        db.executemany(
            "INSERT INTO devices VALUES(?,?,?,?,?,?,?)",
            [
                ("a", "A", "a", "HOST", "unknown", None, None),
                ("b", "B", "b", "host", "unknown", None, None),
            ],
        )
        db.execute("PRAGMA user_version=1")
    db.close()
    with pytest.raises(sqlite3.IntegrityError):
        DeviceStore(tmp_path)
    db = sqlite3.connect(tmp_path / "devices.sqlite3")
    try:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM devices").fetchone()[0] == 2
        assert (
            db.execute("SELECT name FROM sqlite_master WHERE name='devices_v1'").fetchone() is None
        )
    finally:
        db.close()


def test_http_registration_binding_rename_and_removal(tmp_path):
    store = DeviceStore(tmp_path)
    vault = MemoryVault()
    resource = "https://example.com/mcp"
    tokens = ClientTokens(tmp_path, resource=resource, client="client", profile="one", vault=vault)
    tokens.install(pair(), requested_at=time.time())
    saved = dict(vault.data)
    try:
        device = store.add_http("HTTP", resource, "client", "one")
        identity = str(device["device_id"])
        with pytest.raises(ValueError, match="already"):
            store.add_http("Another", resource, "client", "one")
        with pytest.raises(ValueError, match="already"):
            store.add("ＨＴＴＰ", "ssh-alias")
        store.add_http("Other profile", resource, "client", "two")
        renamed = store.rename(identity, "Work")
        assert renamed["resource"] == resource and renamed["profile"] == "one"
        assert store.named("work")["device_id"] == identity
        with pytest.raises(ValueError, match="asynchronous"):
            store.probe(identity)
        store.remove(identity)
        store.add_http("Readded", resource, "client", "one")
        assert tokens.access_token() == pair().access_token
        assert vault.data == saved
    finally:
        store.close()


async def test_http_probe_real_catalog_closes_session_and_preserves_credentials(
    http_remote, tmp_path
):
    backend, adapter, _, _, calls, _ = http_remote
    tokens = backend.tokens
    store = DeviceStore(tmp_path / "client")
    try:
        device = store.add_http("Remote", tokens.resource, tokens.client, "device")
        observation = await store.probe_http(
            str(device["device_id"]), vault=tokens.vault, wire=backend.wire
        )
        assert observation["state"] == "ready"
        assert [packet["method"] for packet in calls if packet] == [
            "initialize",
            "notifications/initialized",
            "tools/list",
        ]
        assert not adapter.sessions
    finally:
        store.close()


async def test_management_http_check_real_catalog_and_cached_refresh(
    http_remote, tmp_path, monkeypatch,
):
    from anywhere_computer.management import ManagementController

    backend, adapter, _, _, calls, _ = http_remote
    tokens = backend.tokens
    directory = tmp_path / "client"
    store = DeviceStore(directory)
    try:
        device = store.add_http("Managed remote", tokens.resource, tokens.client, "device")
    finally:
        store.close()
    saved_credentials = dict(tokens.vault.data)
    original_probe = DeviceStore.probe_http

    async def fixture_transport(self, identity):
        # Substitute only the native vault and HTTPS transport destination.
        # Discovery, authentication, session cleanup and registry writes remain real.
        return await original_probe(self, identity, vault=tokens.vault, wire=backend.wire)

    monkeypatch.setattr(DeviceStore, "probe_http", fixture_transport)
    controller = ManagementController(directory)
    result = await controller.check_device(str(device["device_id"]))
    assert result.state == "ready"
    assert result.evidence == "authorized_catalog"
    assert result.transport == "http"
    assert [packet["method"] for packet in calls if packet] == [
        "initialize", "notifications/initialized", "tools/list",
    ]
    assert not adapter.sessions
    assert tokens.vault.data == saved_credentials
    snapshot = await controller.snapshot()
    assert snapshot.devices[0].last_observed_state == "ready"
    assert snapshot.devices[0].live_state == "not_checked"
    assert snapshot.devices[0].checked_at is not None


async def test_http_probe_missing_credentials_and_network_failure(tmp_path):
    vault = MemoryVault()
    resource = "https://example.com/mcp"
    store = DeviceStore(tmp_path)
    try:
        device = store.add_http("Remote", resource, "client", "profile")
        identity = str(device["device_id"])

        def failing_wire(*args):
            raise TimeoutError("private-network-details")

        assert (await store.probe_http(identity, vault=vault, wire=failing_wire))[
            "state"
        ] == "authorization_required"
        tokens = ClientTokens(
            tmp_path, resource=resource, client="client", profile="profile", vault=vault
        )
        tokens.install(pair(), requested_at=time.time())
        assert (await store.probe_http(identity, vault=vault, wire=failing_wire))[
            "state"
        ] == "not_ready"
        with store.db:
            store.db.execute("UPDATE devices SET checked=?", (time.time() + 3600,))
        assert store.get(identity)["state"] == "unknown"
        assert store.get(identity)["last_observed_state"] == "not_ready"
        assert "private-network-details" not in repr(store.list())
    finally:
        store.close()


def test_cli_http_name_selection_and_transport_guards(tmp_path, monkeypatch, capsys):
    def run(*arguments):
        monkeypatch.setattr(sys, "argv", ["anywhere", *arguments, "--state-dir", str(tmp_path)])
        cli.main()

    run(
        "device-add-http",
        "--name",
        "Work",
        "--resource",
        "https://example.com/mcp",
        "--client-id",
        "client",
        "--profile",
        "profile",
    )
    device = json.loads(capsys.readouterr().out)
    seen = []
    monkeypatch.setattr(cli, "ClientTokens", lambda directory, **kw: seen.append(kw) or object())

    async def consume(tokens):
        return None

    monkeypatch.setattr(cli, "run_http_mcp", consume)
    run("http-mcp", "--device-name", "ＷＯＲＫ")
    assert seen == [
        {"resource": "https://example.com/mcp", "client": "client", "profile": "profile"}
    ]
    with pytest.raises(SystemExit) as error:
        run("http-mcp", "--device", device["device_id"], "--profile", "override")
    assert error.value.code == 2
    with pytest.raises(SystemExit) as error:
        run("remote-mcp", "--device-name", "Work")
    assert error.value.code == 1
    run("device-add", "--name", "SSH", "--ssh-host", "ssh-alias")
    with pytest.raises(SystemExit) as error:
        run("http-mcp", "--device-name", "SSH")
    assert error.value.code == 1
    assert len(seen) == 1


async def test_http_probe_pending_refresh_and_broken_vault_are_distinct(tmp_path):
    vault = MemoryVault()
    store = DeviceStore(tmp_path)
    try:
        identity = str(
            store.add_http("Home", "https://example.com/mcp", "client", "profile")["device_id"]
        )
        tokens = ClientTokens(
            tmp_path,
            resource="https://example.com/mcp",
            client="client",
            profile="profile",
            vault=vault,
        )
        tokens.install(pair(), requested_at=time.time())
        key = next(iter(vault.data))
        record = json.loads(vault.data[key])
        record["phase"] = "refresh_pending"
        vault.data[key] = json.dumps(record)

        def no_network(*args):
            pytest.fail("A pending refresh must not be retried")

        assert (await store.probe_http(identity, vault=vault, wire=no_network))[
            "state"
        ] == "authorization_required"

        class BrokenVault(MemoryVault):
            def get_password(self, *args):
                raise RuntimeError("private-vault-details")

        assert (await store.probe_http(identity, vault=BrokenVault(), wire=no_network))[
            "state"
        ] == "credential_unavailable"
        assert "private-vault-details" not in repr(store.list())
    finally:
        store.close()


async def test_corrupt_http_metadata_rejected_before_credentials_or_network(tmp_path):
    store = DeviceStore(tmp_path)
    try:
        identity = str(
            store.add_http("Home", "https://example.com/mcp", "client", "profile")["device_id"]
        )
        with store.db:
            store.db.execute("UPDATE devices SET resource='http://invalid.example/mcp'")
        with pytest.raises(ValueError):
            await store.probe_http(identity, vault=MemoryVault())
        assert store.get(identity)["checked_at"] is None
    finally:
        store.close()
