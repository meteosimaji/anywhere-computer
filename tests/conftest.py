"""Shared test fixtures with explicit, test-only cost controls."""

import hashlib
import os
from pathlib import Path

import pytest

from anywhere_computer.owner_credentials import OwnerCredentials
from anywhere_computer.private_directory import migrate_default_windows_state


@pytest.fixture
def tmp_path(tmp_path: Path) -> Path:
    """Secure a fresh pytest directory before tests use it as application state."""
    if os.name == "nt":
        migrate_default_windows_state(root=tmp_path, apply=True)
    return tmp_path


@pytest.fixture
def fast_owner_derivation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bound KDF cost only in tests explicitly checking credential state changes.

    Password and salt still both determine the stored digest; the production
    verifier, validation, locks, vault writes, and readback run unchanged. Real
    work-factor and stored-verifier coverage lives in test_owner_credentials.
    This is deliberately not autouse, including for browser integration tests.
    """
    def derive(password: str, salt: str) -> str:
        return hashlib.scrypt(
            password.encode("utf-8"), salt=bytes.fromhex(salt),
            n=1024, r=8, p=1, maxmem=32 * 1024 * 1024, dklen=32,
        ).hex()

    monkeypatch.setattr(OwnerCredentials, "_derive", staticmethod(derive))
