"""Owner WebAuthn credentials and short-lived, locally authorized enrollment."""

import hashlib
import hmac
import json
import os
import secrets
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from webauthn import base64url_to_bytes

from .client_tokens import ClientCredentialError, CredentialStoreUnavailable, CredentialVault
from .credentials import SERVICE
from .locking import ProcessLock
from .owner_credentials import OwnerCredentials


class PasskeyRecord(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", hide_input_in_errors=True)
    credential_id: str = Field(min_length=8, max_length=2048, repr=False)
    public_key: str = Field(min_length=8, max_length=4096, repr=False)
    sign_count: int = Field(ge=0)
    synced: bool = False
    label: str = Field(min_length=1, max_length=80)


class PasskeyNoLongerEnrolled(ValueError):
    """The verified credential was removed before consent issuance."""


class PasskeyEnrollmentLimitReached(ValueError):
    """Owner reset is required before another passkey registration."""


class _RedeemedTicket(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", hide_input_in_errors=True)
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    expires: float = Field(gt=0, allow_inf_nan=False)


class _PasskeySet(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", hide_input_in_errors=True)
    version: Literal[1] = 1
    credentials: list[PasskeyRecord] = Field(max_length=20)
    redeemed_tickets: list[_RedeemedTicket] = Field(default_factory=list, max_length=128)


class _EnrollmentTicket(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", hide_input_in_errors=True)
    version: Literal[2] = 2
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    verifier_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    expires: float
    device: str


class OwnerPasskeys:
    def __init__(self, credentials: OwnerCredentials, *, device: str) -> None:
        self.credentials = credentials
        self.device = device
        self.vault: CredentialVault = credentials.vault
        binding = json.dumps([credentials.account, device]).encode()
        suffix = hashlib.sha256(binding).hexdigest()
        self.account = "oauth-passkeys-" + suffix
        self.lock_path = credentials.directory / (self.account + ".lock")
        self.ticket_path = credentials.directory / (self.account + ".enroll")

    def _read(self) -> _PasskeySet:
        try:
            raw = self.vault.get_password(SERVICE, self.account)
        except Exception:
            raise CredentialStoreUnavailable("Passkey credential store is unavailable") from None
        if raw is None:
            return _PasskeySet(credentials=[])
        try:
            if len(raw) > 150000:
                raise ValueError("Passkey record exceeds limit")
            return _PasskeySet.model_validate_json(raw)
        except ValueError:
            raise ClientCredentialError("Passkey verification data is invalid") from None

    def list(self) -> list[PasskeyRecord]:
        with ProcessLock(self.lock_path):
            return self._read().credentials

    def find(self, credential_id: str) -> PasskeyRecord | None:
        for record in self.list():
            if hmac.compare_digest(record.credential_id, credential_id):
                return record
        return None

    def register(self, record: PasskeyRecord) -> None:
        with ProcessLock(self.lock_path):
            current = self._read()
            records = current.credentials
            if any(hmac.compare_digest(item.credential_id, record.credential_id)
                   for item in records):
                raise ValueError("Passkey is already enrolled")
            if len(records) >= 20:
                raise PasskeyEnrollmentLimitReached(
                    "Too many passkeys; reset the owner")
            updated = current.model_copy(update={"credentials": [*records, record]})
            self._write(updated)

    def register_with_ticket(self, token: str, record: PasskeyRecord) -> bool:
        """Commit enrollment under the same lock used by owner reset.

        Consume the ticket durably before writing a credential. A failed write
        requires a new locally authorized ticket; a crash cannot reuse this one.
        A concurrent reset either invalidates the ticket first or removes the
        newly enrolled key afterward.
        """
        if len(token) > 128:
            return False
        with ProcessLock(self.credentials.directory / "owner-reset.lock", timeout=30):
            with ProcessLock(self.lock_path):
                try:
                    if self.ticket_path.is_symlink():
                        return False
                    raw = self.ticket_path.read_text(encoding="utf-8")
                    if len(raw) > 4096:
                        return False
                    ticket = _EnrollmentTicket.model_validate_json(raw)
                except (FileNotFoundError, ValueError, OSError):
                    return False
                if (ticket.device != self.device or ticket.expires <= time.time()
                        or not hmac.compare_digest(
                            ticket.digest, hashlib.sha256(token.encode()).hexdigest()
                        ) or ticket.verifier_digest != self._verifier_digest()):
                    return False
                current = self._read()
                if self.credentials.is_passkey_only() and current.credentials:
                    return False
                if any(hmac.compare_digest(item.digest, ticket.digest)
                       for item in current.redeemed_tickets):
                    return False
                records = current.credentials
                if any(hmac.compare_digest(item.credential_id, record.credential_id)
                       for item in records):
                    raise ValueError("Passkey is already enrolled")
                if len(records) >= 20:
                    raise PasskeyEnrollmentLimitReached(
                        "Too many passkeys; reset the owner")
                # A rolled-back wall clock must not make a pruned ticket valid
                # again after its deleted file is restored by a filesystem crash.
                if len(current.redeemed_tickets) >= 128:
                    raise PasskeyEnrollmentLimitReached(
                        "Too many passkey registrations; reset the owner")
                # Persist consumption before the vault write. Otherwise a crash
                # between those operations could enroll another key with this URL.
                self.ticket_path.unlink()
                if os.name != "nt":
                    directory_fd = os.open(self.credentials.directory, os.O_RDONLY)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
                self._write(current.model_copy(update={
                    "credentials": [*records, record],
                    "redeemed_tickets": [*current.redeemed_tickets, _RedeemedTicket(
                        digest=ticket.digest, expires=ticket.expires)],
                }))
                return True

    def _verifier_digest(self) -> str:
        """Bind tickets to the currently installed, salted owner verifier."""
        return hashlib.sha256(self.credentials._record().model_dump_json().encode()).hexdigest()

    def verify_and_update_counter(self, credential_id: str,
                                  verify: Callable[[PasskeyRecord], int]) -> bool:
        """Verify against the current record and commit its counter atomically.

        The callback raises for an invalid assertion. Keeping it under the
        credential lock prevents two assertions using one old counter.
        """
        with ProcessLock(self.lock_path):
            current = self._read()
            records = current.credentials
            for index, item in enumerate(records):
                if hmac.compare_digest(item.credential_id, credential_id):
                    new_count = verify(item)
                    if type(new_count) is not int or new_count < 0:
                        raise ValueError("Invalid passkey counter")
                    if (not item.synced and (item.sign_count or new_count)
                            and new_count <= item.sign_count):
                        raise ValueError("Passkey counter did not advance")
                    # Synced credentials may legitimately report zero or non-monotonic
                    # counters. The WebAuthn verifier handles the clone signal; keep
                    # the greatest observed value so a later rollback is detectable.
                    records[index] = item.model_copy(update={
                        "sign_count": max(item.sign_count, new_count)
                    })
                    self._write(current.model_copy(update={"credentials": records}))
                    return True
            return False

    @contextmanager
    def authorization_guard(self, credential_id: str) -> Iterator[None]:
        """Order issuance with removal without blocking the HTTP event loop."""
        with ProcessLock(self.lock_path):
            if not any(hmac.compare_digest(item.credential_id, credential_id)
                       for item in self._read().credentials):
                raise PasskeyNoLongerEnrolled("Verified passkey is no longer enrolled")
            yield

    def remove(self, credential_id: str) -> None:
        with ProcessLock(self.lock_path):
            current = self._read()
            records = current.credentials
            remaining = [item for item in records if item.credential_id != credential_id]
            if len(remaining) == len(records):
                raise ValueError("Passkey is not enrolled")
            self._write(current.model_copy(update={"credentials": remaining}))

    def remove_with_password(self, credential_id: str, password: str) -> None:
        """Keep owner verification and removal ordered with an offline reset."""
        with ProcessLock(self.credentials.directory / "owner-reset.lock", timeout=30):
            if not self.credentials.verify(password):
                raise ValueError("Current owner password is incorrect")
            self.remove(credential_id)

    def clear(self) -> None:
        """Offline owner reset revokes every passkey, including synced copies."""
        with ProcessLock(self.lock_path):
            # The caller rotates the owner binding before clearing keys, so a
            # restored old ticket cannot pass its verifier binding.
            self._write(_PasskeySet(credentials=[]))
            self.ticket_path.unlink(missing_ok=True)

    def _write(self, records: _PasskeySet) -> None:
        records = _PasskeySet.model_validate(records.model_dump(mode="python"))
        serialized = records.model_dump_json()
        try:
            self.vault.set_password(SERVICE, self.account, serialized)
        except Exception:
            # A native backend may commit and then report failure. Confirm readback.
            pass
        if self._read() != records:
            raise ClientCredentialError("Passkey store update was not confirmed")

    def issue_local_ticket(self, password: str) -> str:
        """Trusted local CLI only. The public HTTP service never calls this method."""
        with ProcessLock(self.credentials.directory / "owner-reset.lock", timeout=30):
            if not self.credentials.verify(password):
                raise ValueError("Current owner password is incorrect")
            with ProcessLock(self.lock_path):
                return self._issue_ticket()

    def issue_initial_local_ticket(self) -> str:
        """Trusted local CLI only; allow one first key in passkey-only mode."""
        with ProcessLock(self.credentials.directory / "owner-reset.lock", timeout=30):
            if not self.credentials.is_passkey_only():
                raise ValueError("Owner is not configured for passkey-only authentication")
            with ProcessLock(self.lock_path):
                if self._read().credentials:
                    raise ValueError("An owner passkey is already enrolled")
                return self._issue_ticket()

    def _issue_ticket(self) -> str:
        """Caller holds the owner-reset and passkey locks."""
        token = secrets.token_urlsafe(32)
        ticket = _EnrollmentTicket(
            digest=hashlib.sha256(token.encode()).hexdigest(),
            verifier_digest=self._verifier_digest(),
            expires=time.time() + 300,
            device=self.device,
        )
        temporary = self.ticket_path.with_name(
            self.ticket_path.name + "." + secrets.token_hex(8)
        )
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as target:
                target.write(ticket.model_dump_json())
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, self.ticket_path)
        finally:
            temporary.unlink(missing_ok=True)
        return token

    def ticket_valid(self, token: str) -> bool:
        if len(token) > 128:
            return False
        with ProcessLock(self.credentials.directory / "owner-reset.lock", timeout=30):
            with ProcessLock(self.lock_path):
                try:
                    if self.ticket_path.is_symlink():
                        return False
                    raw = self.ticket_path.read_text(encoding="utf-8")
                    if len(raw) > 4096:
                        return False
                    ticket = _EnrollmentTicket.model_validate_json(raw)
                except (FileNotFoundError, ValueError, OSError):
                    return False
                current = self._read()
                return (
                    ticket.device == self.device and ticket.expires > time.time()
                    and ticket.verifier_digest == self._verifier_digest()
                    and not (self.credentials.is_passkey_only() and current.credentials)
                    and not any(hmac.compare_digest(item.digest, ticket.digest)
                                for item in current.redeemed_tickets)
                    and hmac.compare_digest(
                        ticket.digest, hashlib.sha256(token.encode()).hexdigest()
                    )
                )

    def consume_ticket(self, token: str) -> bool:
        if len(token) > 128:
            return False
        with ProcessLock(self.credentials.directory / "owner-reset.lock", timeout=30):
            with ProcessLock(self.lock_path):
                # The only caller has completed WebAuthn verification. A racing
                # registration can consume this ticket once; the other must fail.
                try:
                    if self.ticket_path.is_symlink():
                        return False
                    raw = self.ticket_path.read_text(encoding="utf-8")
                    if len(raw) > 4096:
                        return False
                    ticket = _EnrollmentTicket.model_validate_json(raw)
                    current = self._read()
                    if (ticket.device != self.device or ticket.expires <= time.time()
                            or ticket.verifier_digest != self._verifier_digest()
                            or (self.credentials.is_passkey_only() and current.credentials)
                            or any(hmac.compare_digest(item.digest, ticket.digest)
                                   for item in current.redeemed_tickets)
                            or not hmac.compare_digest(
                                ticket.digest, hashlib.sha256(token.encode()).hexdigest()
                            )):
                        return False
                    self.ticket_path.unlink()
                    return True
                except (FileNotFoundError, ValueError, OSError):
                    return False


def credential_bytes(record: PasskeyRecord) -> tuple[bytes, bytes]:
    return base64url_to_bytes(record.credential_id), base64url_to_bytes(record.public_key)
