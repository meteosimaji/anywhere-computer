"""Owner password verification data lives only in the native OS credential store."""

import hashlib
import hmac
import json
import secrets
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .authorization import validate_authorization_url
from .client_tokens import ClientCredentialError, CredentialVault
from .credentials import SERVICE, secure_backend
from .locking import ProcessLock
from .state import prepare_directory


class _PasswordRecord(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", hide_input_in_errors=True)
    version: Literal[1]
    salt: str = Field(pattern=r"^[a-f0-9]{64}$", repr=False)
    digest: str = Field(pattern=r"^[a-f0-9]{64}$", repr=False)


class OwnerPasswordChangeUnknown(ClientCredentialError):
    """The credential store could not confirm which password is installed."""


class OwnerCredentials:
    def __init__(
        self,
        directory: Path,
        *,
        resource: str,
        owner: str,
        vault: CredentialVault | None = None,
    ) -> None:
        validate_authorization_url(resource)
        if not owner or len(owner) > 128 or any(ord(c) < 33 or ord(c) > 126 for c in owner):
            raise ValueError("Invalid owner identifier")
        prepare_directory(directory)
        self.directory = directory.resolve()
        binding = json.dumps([str(self.directory), resource, owner]).encode()
        self.account = "oauth-owner-" + hashlib.sha256(binding).hexdigest()
        self.lock_path = self.directory / (self.account + ".lock")
        self.resource, self.owner = resource, owner
        self.vault = vault if vault is not None else secure_backend()

    @staticmethod
    def _derive(password: str, salt: str) -> str:
        return hashlib.scrypt(
            password.encode("utf-8"),
            salt=bytes.fromhex(salt),
            n=131072,
            r=8,
            p=1,
            maxmem=256 * 1024 * 1024,
            dklen=32,
        ).hex()

    def _read(self) -> str | None:
        try:
            return self.vault.get_password(SERVICE, self.account)
        except Exception:
            raise ClientCredentialError("Owner credential store is unavailable") from None

    def initialize(self, password: str) -> None:
        """Trusted setup only; never replaces an existing owner password."""
        self._validate_password(password)
        with ProcessLock(self.lock_path, timeout=30):
            if self._read() is not None:
                raise ClientCredentialError(
                    "Owner credentials already exist; setup cannot overwrite them"
                )
            salt = secrets.token_hex(32)
            record = _PasswordRecord(version=1, salt=salt, digest=self._derive(password, salt))
            try:
                self.vault.set_password(SERVICE, self.account, record.model_dump_json())
            except Exception:
                raise ClientCredentialError("Could not save owner verification data") from None

    @staticmethod
    def _validate_password(password: str) -> None:
        if len(password) < 16 or len(password.encode("utf-8")) > 1024:
            raise ValueError(
                "Use an owner password of at least 16 characters and at most 1024 bytes"
            )

    def change_password(self, current: str, replacement: str) -> None:
        """Replace the native verifier after authenticating under the writer lock.

        One write, followed by readback; never roll back or retry an uncertain
        write. This changes owner authentication only, not issued OAuth grants.
        """
        self._validate_password(replacement)
        if not current or len(current.encode("utf-8")) > 1024:
            raise ValueError("Current owner password is incorrect")
        with ProcessLock(self.lock_path, timeout=30):
            previous = self._record()
            if not hmac.compare_digest(self._derive(current, previous.salt), previous.digest):
                raise ValueError("Current owner password is incorrect")
            if hmac.compare_digest(current.encode("utf-8"), replacement.encode("utf-8")):
                raise ValueError("New owner password must differ from the current password")
            salt = secrets.token_hex(32)
            updated = _PasswordRecord(version=1, salt=salt, digest=self._derive(replacement, salt))
            try:
                self.vault.set_password(SERVICE, self.account, updated.model_dump_json())
            except Exception:
                # Some backends report an error after committing. Readback decides
                # the outcome without issuing another credential write.
                pass
            try:
                installed = self._record()
            except ClientCredentialError:
                raise OwnerPasswordChangeUnknown(
                    "Password update outcome is unknown; restore credential store access "
                    "before checking the current and new passwords"
                ) from None
            if installed == updated:
                return
            if installed == previous:
                raise ClientCredentialError(
                    "Password update was not saved; the previous password remains installed"
                )
            raise OwnerPasswordChangeUnknown("Password update outcome is unknown; do not retry")

    def _record(self) -> _PasswordRecord:
        raw = self._read()
        if raw is None:
            raise ClientCredentialError("Owner authentication has not been initialized")
        try:
            if len(raw) > 4096:
                raise ValueError("Owner credential exceeds limit")
            record = _PasswordRecord.model_validate_json(raw)
        except ValueError:
            raise ClientCredentialError("Owner verification data is invalid") from None
        return record

    def ensure_initialized(self) -> None:
        """Check readiness without asking for or deriving a password."""
        self._record()

    def is_initialized(self) -> bool:
        """Missing is resumable; unreadable or corrupt records remain errors."""
        if self._read() is None:
            return False
        self.ensure_initialized()
        return True

    def verify(self, password: str) -> bool:
        if not password or len(password.encode("utf-8")) > 1024:
            return False
        with ProcessLock(self.lock_path, timeout=30):
            record = self._record()
            return hmac.compare_digest(self._derive(password, record.salt), record.digest)

    def forget(self) -> None:
        """Remove this verifier only; callers must separately revoke existing grants."""
        with ProcessLock(self.lock_path, timeout=30):
            try:
                if self._read() is not None:
                    self.vault.delete_password(SERVICE, self.account)
            except Exception:
                raise ClientCredentialError("Could not remove owner verification data") from None
