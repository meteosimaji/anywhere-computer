"""Separate-device enrollment authorization; no relay registration or AI consent."""

import math
import threading
import time
import uuid
from collections.abc import Callable
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .authorization import validate_authorization_url
from .client_tokens import ClientCredentialError
from .device_polling import DevicePolling, PollOutcome
from .enrollment_credentials import EnrollmentCredentials, EnrollmentToken
from .enrollment_http import EnrollmentHTTPReply, EnrollmentTransportError, https_enrollment_form


def _origin(value: str) -> tuple[str, str | None, int]:
    validate_authorization_url(value)
    parsed = urlsplit(value)
    return parsed.scheme, parsed.hostname, parsed.port or 443


class EnrollmentProvider(BaseModel):
    """Trusted native configuration. This first profile requires same-origin endpoints."""

    model_config = ConfigDict(
        strict=True, frozen=True, extra="forbid", hide_input_in_errors=True,
        revalidate_instances="always",
    )
    issuer: str
    device_authorization_endpoint: str
    token_endpoint: str
    client_id: str = Field(min_length=1, max_length=128, pattern=r"^[\x21-\x7e]+$")
    scope: str = Field(min_length=1, max_length=1024,
                       pattern=r"^[\x21\x23-\x5b\x5d-\x7e]+(?: [\x21\x23-\x5b\x5d-\x7e]+)*$")

    @model_validator(mode="after")
    def endpoints(self) -> "EnrollmentProvider":
        origin = _origin(self.issuer)
        for endpoint in (self.issuer, self.device_authorization_endpoint, self.token_endpoint):
            if _origin(endpoint) != origin or urlsplit(endpoint).query:
                raise ValueError("Enrollment requires same-origin HTTPS endpoints without queries")
        return self


class _DeviceCodeReply(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, hide_input_in_errors=True)
    device_code: str = Field(min_length=1, max_length=4096, repr=False,
                             pattern=r"^[\x21-\x7e]+$")
    user_code: str = Field(min_length=1, max_length=64, repr=False, pattern=r"^[\x21-\x7e]+$")
    verification_uri: str
    verification_uri_complete: str | None = Field(default=None, repr=False)
    expires_in: int = Field(ge=1, le=86400)
    interval: int = Field(default=5, ge=1, le=3600)


EnrollmentPhase = Literal[
    "new", "starting", "waiting", "requesting", "grant_saved", "denied",
    "expired", "cancelled", "failed", "uncertain", "credential_error",
]


class EnrollmentProgress(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    phase: EnrollmentPhase
    attempt_id: str
    retry_after: float = 0
    user_code: str | None = Field(default=None, repr=False)
    verification_uri: str | None = None
    verification_uri_complete: str | None = Field(default=None, repr=False)
    credential_reference: str | None = None


EnrollmentWire = Callable[[str, dict[str, str]], EnrollmentHTTPReply]


class DeviceAuthorizationClient:
    """One explicit attempt; cancellation and worker completion share a lock.

    The manager may run start/poll/retry_save in a worker thread. No background
    polling, token output or automatic re-enrollment happens here. UI polling of
    progress never causes network requests. A cancelled in-flight exchange may
    still complete remotely; it cannot publish local credentials afterward.
    """

    def __init__(
        self, provider: EnrollmentProvider, credentials: EnrollmentCredentials, *,
        wire: EnrollmentWire = https_enrollment_form,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self._provider = EnrollmentProvider.model_validate(provider)
        if (credentials.issuer != provider.issuer or credentials.client != provider.client_id):
            raise ValueError("Enrollment credentials belong to another provider or client")
        self._credentials, self._wire = credentials, wire
        self._clock, self._wall_clock = clock, wall_clock
        self._lock = threading.RLock()
        self._progress = EnrollmentProgress(phase="new", attempt_id=uuid.uuid4().hex)
        self._code: _DeviceCodeReply | None = None
        self._polling: DevicePolling | None = None
        self._pending: tuple[EnrollmentToken, float] | None = None

    def _set(self, phase: EnrollmentPhase) -> EnrollmentProgress:
        self._progress = EnrollmentProgress(phase=phase, attempt_id=self._progress.attempt_id)
        if phase not in {"waiting", "requesting"}:
            self._code = None
        return self._progress

    def progress(self) -> EnrollmentProgress:
        with self._lock:
            if self._polling is not None and self._progress.phase == "waiting":
                state = self._polling.progress()
                if state.phase == "expired":
                    return self._set("expired")
                self._progress = self._progress.model_copy(
                    update={"retry_after": state.retry_after},
                )
            return self._progress

    def start(self) -> EnrollmentProgress:
        with self._lock:
            if self._progress.phase != "new":
                return self.progress()
            try:
                self._credentials.require_empty()
            except ClientCredentialError:
                return self._set("credential_error")
            self._set("starting")
        started = self._clock()
        try:
            reply = self._wire(self._provider.device_authorization_endpoint, {
                "client_id": self._provider.client_id, "scope": self._provider.scope,
            })
            if reply.status != 200 or "error" in reply.fields:
                raise ValueError("Device authorization was not accepted")
            if "iss" in reply.fields and reply.fields["iss"] != self._provider.issuer:
                raise ValueError("Unexpected device authorization issuer")
            code = _DeviceCodeReply.model_validate(reply.fields)
            for uri in (code.verification_uri, code.verification_uri_complete):
                if uri is not None and _origin(uri) != _origin(self._provider.issuer):
                    raise ValueError("Unexpected verification origin")
            elapsed = self._clock() - started
            remaining = code.expires_in - math.ceil(elapsed)
            if elapsed < 0:
                raise ValueError("Invalid authorization clock")
            with self._lock:
                if self.progress().phase == "cancelled":
                    return self._progress
                if remaining <= 0:
                    return self._set("expired")
                self._code = code
                self._polling = DevicePolling(remaining, code.interval, clock=self._clock)
                self._progress = EnrollmentProgress(
                    phase="waiting", attempt_id=self._progress.attempt_id,
                    retry_after=self._polling.progress().retry_after,
                    user_code=code.user_code, verification_uri=code.verification_uri,
                    verification_uri_complete=code.verification_uri_complete,
                )
                return self._progress
        except (EnrollmentTransportError, ValueError):
            with self._lock:
                if self.progress().phase == "cancelled":
                    return self._progress
                return self._set("failed")

    def cancel(self) -> EnrollmentProgress:
        with self._lock:
            if self._progress.phase in {"new", "starting", "waiting", "requesting",
                                        "credential_error"}:
                if self._polling is not None:
                    self._polling.cancel()
                self._pending = None
                return self._set("cancelled")
            return self.progress()

    def poll(self) -> EnrollmentProgress:
        with self._lock:
            if (self.progress().phase != "waiting" or self._polling is None
                    or self._code is None or not self._polling.begin_request()):
                return self.progress()
            code = self._code.device_code
            self._progress = self._progress.model_copy(update={"phase": "requesting"})
        requested_at = self._wall_clock()
        token: EnrollmentToken | None = None
        outcome: PollOutcome = "unknown_result"
        try:
            reply = self._wire(self._provider.token_endpoint, {
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "client_id": self._provider.client_id, "device_code": code,
            })
            if reply.status == 200 and "error" not in reply.fields:
                if "iss" in reply.fields and reply.fields["iss"] != self._provider.issuer:
                    raise ValueError("Unexpected token issuer")
                token = EnrollmentToken.model_validate(
                    {"scope": self._provider.scope, **reply.fields},
                )
                if set(token.scope.split()) != set(self._provider.scope.split()):
                    raise ValueError("Unexpected enrollment scope")
                outcome = "authorized"
            elif reply.status == 400:
                error = reply.fields.get("error")
                if error == "authorization_pending":
                    outcome = "authorization_pending"
                elif error == "slow_down":
                    outcome = "slow_down"
                elif error == "access_denied":
                    outcome = "access_denied"
                elif error == "expired_token":
                    outcome = "expired_token"
                else:
                    outcome = "invalid_response"
        except EnrollmentTransportError as error:
            if not error.dispatched:
                outcome = "connection_timeout" if error.timeout else "invalid_response"
        except ValueError:
            pass  # A malformed success may already have consumed the grant.
        with self._lock:
            if self._progress.phase == "cancelled":
                return self._progress
            state = self._polling.finish_request(outcome)
            if state.phase == "authorized" and token is not None:
                self._pending = token, requested_at
                return self.retry_save()
            if state.phase == "waiting":
                self._progress = self._progress.model_copy(update={
                    "phase": "waiting", "retry_after": state.retry_after,
                })
                return self._progress
            match state.phase:
                case "denied" | "expired" | "cancelled" | "failed" | "uncertain":
                    return self._set(state.phase)
                case _:
                    return self._set("failed")

    def retry_save(self) -> EnrollmentProgress:
        """Reconcile only the same received grant; never repeat its token request."""
        with self._lock:
            if self._pending is None:
                return self.progress()
            token, requested_at = self._pending
            try:
                reference = self._credentials.save(
                    self._progress.attempt_id, token, requested_at=requested_at,
                )
            except ClientCredentialError:
                return self._set("credential_error")
            self._pending = None
            self._set("grant_saved")
            self._progress = self._progress.model_copy(update={"credential_reference": reference})
            return self._progress
