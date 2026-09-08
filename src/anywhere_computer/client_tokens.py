"""Serialized OAuth renewal, with crash-safe intent stored in the OS keyring.

This is the internal client credential manager, not a browser login flow. All
processes sharing a connection must use the same directory and profile. Token
responses and vault contents must never be logged.
"""

import hashlib
import http.client
import json
import math
import ssl
import time
from collections.abc import Callable
from pathlib import Path
from typing import Literal, Protocol
from urllib.parse import urlencode, urlsplit

from pydantic import BaseModel, ConfigDict, Field

from .authorization import validate_authorization_url
from .credentials import SERVICE, secure_backend
from .locking import ProcessLock
from .state import prepare_directory


class ClientAuthorizationRequired(RuntimeError):
    """A new user authorization is necessary; no tokens appear in the message."""


class ClientCredentialError(RuntimeError):
    """The OS credential store could not safely read or persist a credential."""


class RefreshOutcomeUnknown(ClientAuthorizationRequired):
    """A refresh might have consumed its token. Never resend that token."""


class CredentialVault(Protocol):
    def get_password(self, service: str, account: str, /) -> str | None: ...

    def set_password(self, service: str, account: str, value: str, /) -> None: ...

    def delete_password(self, service: str, account: str, /) -> None: ...


class TokenReply(BaseModel):
    model_config = ConfigDict(
        strict=True, frozen=True, hide_input_in_errors=True, revalidate_instances="always"
    )

    access_token: str = Field(min_length=1, max_length=256, pattern=r"^[\x21-\x7e]+$", repr=False)
    refresh_token: str = Field(min_length=1, max_length=256, pattern=r"^[\x21-\x7e]+$", repr=False)
    token_type: str = Field(pattern=r"(?i)^bearer$")
    expires_in: int = Field(ge=1, le=900)
    scope: str = Field(
        min_length=1,
        max_length=4096,
        pattern=r"^[\x21\x23-\x5b\x5d-\x7e]+(?: [\x21\x23-\x5b\x5d-\x7e]+)*$",
    )


class _StoredTokens(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid", hide_input_in_errors=True)

    version: Literal[1]
    phase: Literal["ready", "refresh_pending"]
    tokens: TokenReply = Field(repr=False)
    expires_at: float = Field(gt=0, allow_inf_nan=False)


def https_refresh(resource: str, client: str, refresh_token: str) -> TokenReply:
    """One verified HTTPS POST to our colocated issuer, without redirects/retries."""
    validate_authorization_url(resource)
    parsed = urlsplit(resource)
    if parsed.path != "/mcp" or parsed.query:
        raise ValueError("Token renewal requires a canonical HTTPS /mcp resource")
    connection = http.client.HTTPSConnection(
        parsed.hostname or "", port=parsed.port, timeout=15, context=ssl.create_default_context()
    )
    try:
        connection.request(
            "POST",
            "/oauth/token",
            body=urlencode(
                {
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": client,
                    "resource": resource,
                }
            ).encode("ascii"),
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
        )
        response = connection.getresponse()
        raw = response.read(16385)
        if len(raw) > 16384:
            raise ValueError("Token response exceeds limit")
        if response.status == 400:
            # Do not expose arbitrary provider descriptions, URLs or response text.
            failure = json.loads(raw)
            if isinstance(failure, dict) and failure.get("error") == "invalid_grant":
                raise ClientAuthorizationRequired(
                    "Authorization expired or was revoked; sign in again"
                )
        if response.status != 200:
            raise ValueError("Token endpoint did not return success")
        if (
            response.getheader("Content-Type", "").split(";")[0].strip().lower()
            != "application/json"
        ):
            raise ValueError("Token endpoint did not return JSON")
        return TokenReply.model_validate_json(raw)
    except ClientAuthorizationRequired:
        raise
    except Exception:
        raise RefreshOutcomeUnknown(
            "Token renewal was not confirmed; authorize this connection again"
        ) from None
    finally:
        connection.close()


class ClientTokens:
    """One OS keyring item per resource/client/profile, locked across processes.

    A pending intent is committed before sending a refresh. If the process dies,
    the response is lost, or the replacement cannot be saved, subsequent calls
    refuse to reuse the old refresh token. Installing newly authorized tokens is
    the recovery path. The injected vault/refresh are for trusted embedding/tests.
    """

    def __init__(
        self,
        directory: Path,
        *,
        resource: str,
        client: str,
        profile: str,
        vault: CredentialVault | None = None,
        refresh: Callable[[str, str, str], TokenReply] = https_refresh,
        clock: Callable[[], float] = time.time,
    ) -> None:
        validate_authorization_url(resource)
        parsed = urlsplit(resource)
        if parsed.path != "/mcp" or parsed.query:
            raise ValueError("Client requires a canonical HTTPS /mcp resource")
        for identifier in (client, profile):
            if (
                not identifier
                or len(identifier) > 128
                or any(ord(char) < 33 or ord(char) > 126 for char in identifier)
            ):
                raise ValueError("Invalid client or connection profile")
        prepare_directory(directory)
        binding = json.dumps([str(directory.resolve()), resource, client, profile]).encode()
        self.account = "oauth-client-" + hashlib.sha256(binding).hexdigest()
        self.lock_path = directory / (self.account + ".lock")
        self.resource, self.client = resource, client
        self.vault = vault if vault is not None else secure_backend()
        self.refresh = refresh
        self.clock = clock

    def _save(self, record: _StoredTokens) -> None:
        try:
            self.vault.set_password(SERVICE, self.account, record.model_dump_json())
        except Exception:
            raise ClientCredentialError(
                "Could not save credentials to the OS credential store"
            ) from None

    def _load(self) -> _StoredTokens:
        try:
            raw = self.vault.get_password(SERVICE, self.account)
        except Exception:
            raise ClientCredentialError(
                "Could not read credentials from the OS credential store"
            ) from None
        if raw is None:
            raise ClientAuthorizationRequired("This connection needs authorization")
        try:
            if len(raw) > 16384:
                raise ValueError("Credential exceeds limit")
            return _StoredTokens.model_validate_json(raw)
        except ValueError:
            raise ClientAuthorizationRequired(
                "Saved authorization is invalid; authorize again"
            ) from None

    def install(self, response: object, *, requested_at: float) -> None:
        """Save a freshly authorized pair. requested_at is BEFORE code redemption."""
        try:
            tokens = TokenReply.model_validate(response)
            expires_at = requested_at + tokens.expires_in
            if (
                not math.isfinite(requested_at)
                or requested_at > self.clock()
                or expires_at <= self.clock()
            ):
                raise ValueError("Invalid token lifetime")
            record = _StoredTokens(version=1, phase="ready", tokens=tokens, expires_at=expires_at)
        except (ValueError, TypeError):
            raise ClientAuthorizationRequired(
                "Token response is invalid or expired; authorize again"
            ) from None
        with ProcessLock(self.lock_path, timeout=30):
            self._save(record)

    def access_token(self) -> str:
        """Get access for a NEW request; never retry a previous MCP mutation here."""
        with ProcessLock(self.lock_path, timeout=30):
            record = self._load()
            if record.phase == "refresh_pending":
                raise RefreshOutcomeUnknown(
                    "Previous token renewal was not confirmed; authorize again"
                )
            # Short-lived responses near grant expiry must not cause a refresh on every call.
            margin = min(60.0, record.tokens.expires_in / 10)
            if self.clock() < record.expires_at - margin:
                return record.tokens.access_token
            self._save(record.model_copy(update={"phase": "refresh_pending"}))
            started = self.clock()
            try:
                renewed = self.refresh(self.resource, self.client, record.tokens.refresh_token)
                renewed = TokenReply.model_validate(renewed)
                if (
                    renewed.refresh_token == record.tokens.refresh_token
                    or renewed.access_token == record.tokens.access_token
                    or set(renewed.scope.split()) != set(record.tokens.scope.split())
                    or started + renewed.expires_in <= self.clock()
                ):
                    raise ValueError("Invalid rotation response")
                updated = _StoredTokens(
                    version=1,
                    phase="ready",
                    tokens=renewed,
                    expires_at=started + renewed.expires_in,
                )
                self._save(updated)
                return renewed.access_token
            except ClientAuthorizationRequired:
                raise
            except Exception:
                raise RefreshOutcomeUnknown(
                    "Token renewal was not safely saved; authorize again"
                ) from None

    def forget(self) -> None:
        """Remove local credentials only; remote grant revocation is separate."""
        with ProcessLock(self.lock_path, timeout=30):
            try:
                if self.vault.get_password(SERVICE, self.account) is not None:
                    self.vault.delete_password(SERVICE, self.account)
            except Exception:
                raise ClientCredentialError(
                    "Could not remove credentials from the OS credential store"
                ) from None
