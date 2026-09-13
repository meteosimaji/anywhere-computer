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
