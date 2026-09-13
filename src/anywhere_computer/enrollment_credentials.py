"""OS-vault storage for enrollment grants, separate from AI/relay credentials."""

import hashlib
import json
import math
import time
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .authorization import validate_authorization_url
from .client_tokens import ClientCredentialError, CredentialVault
from .credentials import SERVICE, secure_backend
from .locking import ProcessLock
from .state import prepare_directory


class EnrollmentToken(BaseModel):
    model_config = ConfigDict(
        strict=True, frozen=True, hide_input_in_errors=True, revalidate_instances="always",
    )
    access_token: str = Field(min_length=1, max_length=4096, pattern=r"^[\x21-\x7e]+$", repr=False)
    refresh_token: str | None = Field(
        default=None, min_length=1, max_length=4096, pattern=r"^[\x21-\x7e]+$", repr=False,
    )
    token_type: str = Field(pattern=r"(?i)^bearer$")
    expires_in: int = Field(ge=1, le=86400)
    scope: str = Field(min_length=1, max_length=1024,
                       pattern=r"^[\x21\x23-\x5b\x5d-\x7e]+(?: [\x21\x23-\x5b\x5d-\x7e]+)*$")


class _SavedGrant(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid", hide_input_in_errors=True)
    version: Literal[1] = 1
    attempt_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    issuer: str
    client: str
    expires_at: float = Field(gt=0, allow_inf_nan=False)
    token: EnrollmentToken = Field(repr=False)


class EnrollmentCredentials:
    """No credential is published until exact OS-vault readback succeeds.

    Failed saves can be reconciled using the same attempt and grant without
    redeeming its code again. Existing grants from other attempts are preserved.
    This store does not refresh or grant permission to execute any PC operation.
    """

    def __init__(
        self, directory: Path, *, issuer: str, client: str, profile: str,
        vault: CredentialVault | None = None, clock: Callable[[], float] = time.time,
    ) -> None:
        validate_authorization_url(issuer)
        if any(not value or len(value) > 128 or not value.isascii()
               or any(ord(c) < 33 or ord(c) > 126 for c in value) for value in (client, profile)):
            raise ValueError("Invalid enrollment client or profile")
        prepare_directory(directory)
        binding = json.dumps([str(directory.resolve()), issuer, client, profile]).encode()
        self.reference = "enrollment-grant-" + hashlib.sha256(binding).hexdigest()
        self._lock_path = directory / (self.reference + ".lock")
        self.issuer, self.client = issuer, client
        self._vault = vault if vault is not None else secure_backend()
        self._clock = clock

    def require_empty(self) -> None:
        try:
            with ProcessLock(self._lock_path, timeout=5):
                if self._vault.get_password(SERVICE, self.reference) is not None:
                    raise ClientCredentialError(
                        "An enrollment grant already exists for this profile",
                    )
        except ClientCredentialError:
            raise
        except Exception:
            raise ClientCredentialError("Could not inspect the OS enrollment credential") from None

    def _read_current(self, scope: str) -> _SavedGrant | None:
        with ProcessLock(self._lock_path, timeout=5):
            encoded = self._vault.get_password(SERVICE, self.reference)
            if encoded is None:
                return None
            if len(encoded) > 16384:
                raise ValueError("Oversized enrollment record")
            record = _SavedGrant.model_validate_json(encoded)
            now = self._clock()
            if (record.issuer != self.issuer or record.client != self.client
                    or record.token.scope != scope or not math.isfinite(now)
                    or not record.expires_at - record.token.expires_in <= now < record.expires_at):
                raise ValueError("Enrollment binding or lifetime does not match")
            return record

    def saved_attempt(self, *, scope: str) -> str | None:
        """Read only the identity of a valid saved grant, never its token."""
        try:
            record = self._read_current(scope)
            return record.attempt_id if record is not None else None
        except Exception:
            raise ClientCredentialError("Saved enrollment grant could not be restored") from None

    def access_token(self, *, attempt_id: str, scope: str) -> str:
        """Read a grant for the trusted client; never expose it through IPC/MCP."""
        try:
            record = self._read_current(scope)
            if record is None or record.attempt_id != attempt_id:
                raise ValueError("Enrollment attempt does not match")
            return record.token.access_token
        except Exception:
            raise ClientCredentialError(
                "Saved enrollment grant is unavailable for this attempt",
            ) from None

    def save(self, attempt_id: str, token: EnrollmentToken, *, requested_at: float) -> str:
        try:
            validated = EnrollmentToken.model_validate(token)
            now = self._clock()
            if (not math.isfinite(requested_at) or not math.isfinite(now)
                    or requested_at > now or requested_at + validated.expires_in <= now):
                raise ValueError("Enrollment grant is expired")
            record = _SavedGrant(
                attempt_id=attempt_id, issuer=self.issuer, client=self.client,
                token=validated, expires_at=requested_at + validated.expires_in,
            )
            encoded = record.model_dump_json()
            with ProcessLock(self._lock_path, timeout=5):
                previous = self._vault.get_password(SERVICE, self.reference)
                if previous is not None and previous != encoded:
                    raise ClientCredentialError("Existing enrollment credentials were preserved")
                if previous is None:
                    self._vault.set_password(SERVICE, self.reference, encoded)
                if self._vault.get_password(SERVICE, self.reference) != encoded:
                    raise ClientCredentialError("Enrollment credential readback did not match")
            return self.reference
        except ClientCredentialError:
            raise
        except Exception:
            raise ClientCredentialError(
                "Enrollment grant was not confirmed in the OS store",
            ) from None
