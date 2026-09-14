from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from anywhere_computer.relay_registry import RelayAccount, RelayRegistry


def account(subject="owner", issuer="https://auth.example"):
    return RelayAccount(issuer=issuer, subject=subject)


def test_registration_reopens_without_duplicate_and_names_are_not_identity(tmp_path):
    first = RelayRegistry(tmp_path)
    device = first.register(account(), enrollment_id="a" * 32, name="同じPC")
    first.close()
    second = RelayRegistry(tmp_path)
    try:
        assert second.register(account(), enrollment_id="a" * 32, name="同じPC") == device
        another = second.register(account(), enrollment_id="b" * 32, name="同じPC")
        assert another.device_id != device.device_id
        assert device.state == another.state == "registered"
        with pytest.raises(ValueError, match="conflicts"):
            second.register(account(), enrollment_id="a" * 32, name="別の名前")
        assert second.get(account(), device.device_id) == device
    finally:
        second.close()


@pytest.mark.parametrize("other", [account("other"), account(issuer="https://other.example")])
def test_cross_account_lookup_and_revocation_have_no_side_effect(tmp_path, other):
    registry = RelayRegistry(tmp_path)
    try:
        original = registry.register(account(), enrollment_id="a" * 32, name="PC")
        for operation in (registry.get, registry.revoke):
            with pytest.raises(ValueError, match="not registered"):
                operation(other, original.device_id)
        assert registry.get(account(), original.device_id) == original
        separate = registry.register(other, enrollment_id="a" * 32, name="PC")
        assert separate.device_id != original.device_id
    finally:
        registry.close()


def test_revocation_survives_restart_and_old_request_cannot_resurrect(tmp_path):
    registry = RelayRegistry(tmp_path)
    original = registry.register(account(), enrollment_id="a" * 32, name="PC")
    revoked = registry.revoke(account(), original.device_id)
    assert revoked.state == "revoked"
    registry.close()
    reopened = RelayRegistry(tmp_path)
    try:
        assert reopened.revoke(account(), original.device_id) == revoked
        assert reopened.register(account(), enrollment_id="a" * 32, name="PC") == revoked
    finally:
        reopened.close()


def test_concurrent_registration_uses_one_durable_device(tmp_path):
    barrier = Barrier(2)

    def register():
        registry = RelayRegistry(tmp_path)
        try:
            barrier.wait(timeout=5)
            return registry.register(account(), enrollment_id="a" * 32, name="PC")
        finally:
            registry.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = [pool.submit(register) for _ in range(2)]
        devices = [future.result(timeout=10) for future in pending]
    assert devices[0] == devices[1]


def test_channel_binding_is_durable_scoped_and_revocation_aware(tmp_path):
    registry = RelayRegistry(tmp_path)
    original = registry.register(account(), enrollment_id="a" * 32, name="PC")
    other = registry.register(account("other"), enrollment_id="a" * 32, name="PC")
    fingerprint = "1" * 64
    with pytest.raises(ValueError):
        registry.bind_channel(account("other"), original.device_id, fingerprint=fingerprint)
    registry.bind_channel(account(), original.device_id, fingerprint=fingerprint)
    registry.bind_channel(account(), original.device_id, fingerprint=fingerprint)
    with pytest.raises(ValueError, match="already bound"):
        registry.bind_channel(account("other"), other.device_id, fingerprint=fingerprint)
    with pytest.raises(ValueError, match="already bound"):
        registry.bind_channel(account(), original.device_id, fingerprint="2" * 64)
    registry.close()
    reopened = RelayRegistry(tmp_path)
    try:
        assert reopened.channel_device(fingerprint) == (account(), original)
        reopened.revoke(account(), original.device_id)
        with pytest.raises(ValueError, match="unknown or revoked"):
            reopened.channel_device(fingerprint)
        with pytest.raises(ValueError, match="revoked"):
            reopened.bind_channel(account(), original.device_id, fingerprint=fingerprint)
        assert reopened.get(account("other"), other.device_id) == other
    finally:
        reopened.close()


@pytest.mark.parametrize("fingerprint", ["", "A" * 64, "1" * 63, "1" * 65, "x" * 64])
def test_invalid_channel_identity_is_not_saved(tmp_path, fingerprint):
    registry = RelayRegistry(tmp_path)
    try:
        original = registry.register(account(), enrollment_id="a" * 32, name="PC")
        with pytest.raises(ValueError):
            registry.bind_channel(account(), original.device_id, fingerprint=fingerprint)
        assert registry.db.execute("SELECT COUNT(*) FROM relay_channels").fetchone()[0] == 0
    finally:
        registry.close()


def test_channel_schema_upgrade_preserves_existing_device_and_revocation(tmp_path):
    import sqlite3

    with sqlite3.connect(tmp_path / "relay-devices.sqlite3") as old:
        old.execute("CREATE TABLE relay_devices (device_id TEXT PRIMARY KEY, issuer TEXT NOT NULL,"
                    "subject TEXT NOT NULL,enrollment_id TEXT NOT NULL,name TEXT NOT NULL,"
                    "state TEXT NOT NULL CHECK(state IN ('registered','revoked')),"
                    "UNIQUE(issuer,subject,enrollment_id))")
        old.execute("INSERT INTO relay_devices VALUES(?,?,?,?,?,?)",
                    ("a" * 32, account().issuer, account().subject, "b" * 32, "元のPC", "revoked"))
        old.execute("PRAGMA user_version=1")
    registry = RelayRegistry(tmp_path)
    try:
        saved = registry.get(account(), "a" * 32)
        assert saved.name == "元のPC" and saved.state == "revoked"
        assert saved.enrollment_id == "b" * 32
        assert registry.db.execute("PRAGMA user_version").fetchone()[0] == 2
        with pytest.raises(ValueError, match="revoked"):
            registry.bind_channel(account(), saved.device_id, fingerprint="1" * 64)
    finally:
        registry.close()


def test_channel_rotation_preserves_device_and_rejects_stale_writer(tmp_path):
    registry = RelayRegistry(tmp_path)
    try:
        device = registry.register(account(), enrollment_id='a' * 32, name='PC')
        registry.bind_channel(account(), device.device_id, fingerprint='1' * 64)
        registry.rotate_channel(account(), device.device_id,
                                expected_fingerprint='1' * 64, fingerprint='2' * 64)
        assert registry.get(account(), device.device_id) == device
        assert registry.channel_device('2' * 64) == (account(), device)
        with pytest.raises(ValueError):
            registry.channel_device('1' * 64)
        for owner, expected, replacement in [
            (account('other'), '2' * 64, '3' * 64),
            (account(), '1' * 64, '3' * 64),
        ]:
            with pytest.raises(ValueError):
                registry.rotate_channel(owner, device.device_id,
                                        expected_fingerprint=expected, fingerprint=replacement)
        other = registry.register(account(), enrollment_id='b' * 32, name='PC')
        registry.bind_channel(account(), other.device_id, fingerprint='3' * 64)
        with pytest.raises(ValueError):
            registry.rotate_channel(account(), device.device_id,
                                    expected_fingerprint='2' * 64, fingerprint='3' * 64)
        assert registry.channel_device('2' * 64) == (account(), device)
        assert registry.channel_device('3' * 64) == (account(), other)
    finally:
        registry.close()
    reopened = RelayRegistry(tmp_path)
    try:
        assert reopened.channel_device('2' * 64) == (account(), device)
        reopened.revoke(account(), device.device_id)
        with pytest.raises(ValueError):
            reopened.rotate_channel(account(), device.device_id,
                                    expected_fingerprint='2' * 64, fingerprint='4' * 64)
    finally:
        reopened.close()
