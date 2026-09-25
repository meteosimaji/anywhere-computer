"""The macOS credential adapter must preserve existing Keychain items."""

import pytest
from keyring.backend import KeyringBackend

from anywhere_computer import credentials


class _Vault(KeyringBackend):
    priority = 1

    def __init__(self):
        self.data = {}
        self.adds = 0
        self.deletes = 0

    def get_password(self, service, account):
        return self.data.get((service, account))

    def set_password(self, service, account, value):
        self.adds += 1
        self.data[service, account] = value

    def delete_password(self, service, account):
        self.deletes += 1
        self.data.pop((service, account), None)


def test_macos_existing_credential_is_updated_without_deleting(monkeypatch):
    native = _Vault()
    wrapped = credentials._UpdatingMacOSKeyring(native)
    wrapped.set_password(credentials.SERVICE, "owner", "first")

    def update(service, account, value):
        native.data[service, account] = value

    monkeypatch.setattr(credentials, "_macos_update_password", update)
    wrapped.set_password(credentials.SERVICE, "owner", "second")

    assert wrapped.get_password(credentials.SERVICE, "owner") == "second"
    assert native.adds == 1
    assert native.deletes == 0


def test_failed_native_update_preserves_existing_credential(monkeypatch):
    native = _Vault()
    native.set_password(credentials.SERVICE, "owner", "first")
    wrapped = credentials._UpdatingMacOSKeyring(native)

    def fail_update(_service, _account, _value):
        raise RuntimeError("synthetic update failure")

    monkeypatch.setattr(credentials, "_macos_update_password", fail_update)
    with pytest.raises(RuntimeError, match="synthetic update failure"):
        wrapped.set_password(credentials.SERVICE, "owner", "second")

    assert wrapped.get_password(credentials.SERVICE, "owner") == "first"
    assert native.adds == 1
    assert native.deletes == 0
