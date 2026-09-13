"""Durable registration attempts; grants remain in the existing OS vault."""

import sqlite3
import uuid
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .authorization import validate_authorization_url
from .devices import device_name
from .enrollment_credentials import EnrollmentCredentials
from .enrollment_http import EnrollmentHTTPReply, https_enrollment_registration
from .locking import ProcessLock
from .relay_registry import RelayAccount, RelayDevice
from .state import prepare_directory


class RegistrationAttempt(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid", hide_input_in_errors=True)
    attempt_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    enrollment_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    name: str
    device: RelayDevice | None = None
    owner: RelayAccount | None = None
    account_endpoint: str | None = None

    @model_validator(mode="after")
    def consistent_receipt(self) -> "RegistrationAttempt":
        if (self.owner is None) != (self.account_endpoint is None):
            raise ValueError("Incomplete registration account binding")
        if self.name != device_name(self.name):
            raise ValueError("Saved registration name is not normalized")
        if self.device is not None and (
            self.device.enrollment_id != self.enrollment_id or self.device.name != self.name
        ):
            raise ValueError("Saved device receipt differs from its registration request")
        return self


RegistrationWire = Callable[[str, dict[str, str], str], EnrollmentHTTPReply]


def _send(endpoint: str, fields: dict[str, str], token: str) -> EnrollmentHTTPReply:
    return https_enrollment_registration(endpoint, fields, token=token)


class RegistrationClient:
    """Persist the request before dispatch; explicitly retry the same identity.

    A record without a device receipt is unconfirmed, not proof of no dispatch.
    Successful receipts are cached registration evidence, never live readiness.
    The caller owns the connection on its thread and must close it.
    """

    def __init__(self, directory: Path, credentials: EnrollmentCredentials, *,
                 endpoint: str, wire: RegistrationWire = _send,
                 account_endpoint: str | None = None) -> None:
        validate_authorization_url(endpoint)
        if urlsplit(endpoint).query:
            raise ValueError("Registration endpoint must not have a query")
        if account_endpoint is not None:
            validate_authorization_url(account_endpoint)
            registration_url, account_url = urlsplit(endpoint), urlsplit(account_endpoint)
            if (account_url.query or account_url.hostname != registration_url.hostname
                    or (account_url.port or 443) != (registration_url.port or 443)):
                raise ValueError("Account lookup must use the registration origin")
        self._account_endpoint = account_endpoint
        prepare_directory(directory)
        path = directory / "registration.sqlite3"
        if path.is_symlink():
            raise ValueError("Registration database must not be a symbolic link")
        self._lock = directory / "registration.lock"
        self._credentials, self._endpoint, self._wire = credentials, endpoint, wire
        self._db = sqlite3.connect(path, timeout=5)
        try:
            with self._db:
                self._db.execute("BEGIN IMMEDIATE")
                version = self._db.execute("PRAGMA user_version").fetchone()[0]
                if version not in (0, 1, 2, 3):
                    raise ValueError("Unsupported registration state version")
                self._db.execute("CREATE TABLE IF NOT EXISTS registration ("
                                 "credential TEXT PRIMARY KEY, endpoint TEXT NOT NULL, "
                                 "record TEXT NOT NULL)")
                self._db.execute("CREATE TABLE IF NOT EXISTS reauthorizations ("
                                 "credential TEXT NOT NULL, slot TEXT NOT NULL UNIQUE)")
                self._db.execute("PRAGMA user_version=3")
        except BaseException:
            self._db.close()
            raise

    def close(self) -> None:
        self._db.close()

    def current(self) -> RegistrationAttempt | None:
        row = self._db.execute("SELECT endpoint,record FROM registration WHERE credential=?",
                               (self._credentials.reference,)).fetchone()
        if row is None:
            return None
        if row[0] != self._endpoint:
            raise ValueError("Saved registration belongs to a different endpoint")
        return RegistrationAttempt.model_validate_json(row[1])

    def reauthorization_slot(self) -> str | None:
        self.current()  # Reject configuration changes before selecting another grant.
        row = self._db.execute(
            "SELECT slot FROM reauthorizations WHERE credential=? ORDER BY rowid DESC LIMIT 1",
            (self._credentials.reference,),
        ).fetchone()
        if row is None:
            return None
        if not isinstance(row[0], str) or uuid.UUID(hex=row[0]).hex != row[0]:
            raise ValueError("Invalid saved reauthorization slot")
        return row[0]

    def begin_reauthorization(self) -> str:
        """Persist a new vault-slot identity before starting explicit authorization.

        Historical slots remain recorded for later credential cleanup; no grant
        is deleted or replaced. Slots contain no codes, tokens or account claims.
        """
        with ProcessLock(self._lock, timeout=0):
            current = self.current()
            if (current is None or current.device is not None or current.owner is None
                    or current.account_endpoint != self._account_endpoint):
                raise ValueError("Reauthorization requires an account-bound pending registration")
            slot = uuid.uuid4().hex
            with self._db:
                self._db.execute("INSERT INTO reauthorizations VALUES(?,?)",
                                 (self._credentials.reference, slot))
            return slot

    def recover(self, credentials: EnrollmentCredentials, *,
                attempt_id: str) -> RegistrationAttempt:
        """Recover a pending registration with a separately saved fresh grant.

        The trusted caller owns the new authorization and its OS-vault lifetime.
        Neither credential slot is rewritten here. Original request identity and
        account binding survive failure and process restart.
        """
        with ProcessLock(self._lock, timeout=0):
            current = self.current()
            if (current is None or current.owner is None
                    or current.account_endpoint != self._account_endpoint):
                raise ValueError("Recovery requires the original verified account binding")
            if (credentials.issuer != self._credentials.issuer
                    or credentials.client != self._credentials.client):
                raise ValueError("Recovery credentials belong to another provider or client")
            token = credentials.access_token(attempt_id=attempt_id, scope="device:enroll")
            if self._verified_account(token) != current.owner:
                raise ValueError("Registration belongs to a different account")
            if current.device is not None:
                return current
            return self._confirm(current, token)

    def _verified_account(self, token: str) -> RelayAccount | None:
        if self._account_endpoint is None:
            return None
        identity = self._wire(self._account_endpoint, {}, token)
        try:
            if identity.status != 200:
                raise ValueError("Account lookup was rejected")
            owner = RelayAccount.model_validate(identity.fields)
            if owner.issuer != self._credentials.issuer:
                raise ValueError("Account issuer mismatch")
            return owner
        except ValueError:
            raise ValueError("Registration account could not be verified") from None

    def register(self, *, attempt_id: str, name: str) -> RegistrationAttempt:
        name = device_name(name)
        proposed = RegistrationAttempt(attempt_id=attempt_id, enrollment_id=uuid.uuid4().hex,
                                       name=name)
        with ProcessLock(self._lock, timeout=0):
            current = self.current()
            if current is not None:
                if current.attempt_id != attempt_id or current.name != name:
                    raise ValueError(
                        "Saved registration must be recovered with its original request",
                    )
                if current.device is not None:
                    return current
                if current.account_endpoint != self._account_endpoint:
                    raise ValueError("Pending registration account binding cannot be changed")
            token = self._credentials.access_token(attempt_id=attempt_id, scope="device:enroll")
            owner = self._verified_account(token)
            if current is not None and current.owner != owner:
                raise ValueError("Registration belongs to a different account")
            if current is None:
                proposed = proposed.model_copy(update={
                    "owner": owner, "account_endpoint": self._account_endpoint,
                })
                with self._db:
                    self._db.execute("INSERT INTO registration VALUES(?,?,?)", (
                        self._credentials.reference, self._endpoint, proposed.model_dump_json(),
                    ))
                current = proposed
            return self._confirm(current, token)

    def _confirm(self, current: RegistrationAttempt, token: str) -> RegistrationAttempt:
        # Caller holds the registration process lock across lookup and dispatch.
        reply = self._wire(self._endpoint, {
            "enrollment_id": current.enrollment_id, "name": current.name,
        }, token)
        if reply.status != 200:
            raise ValueError("Registration was not confirmed; original request retained")
        try:
            device = RelayDevice.model_validate(reply.fields)
        except ValueError:
            raise ValueError(
                "Invalid registration response; original request retained",
            ) from None
        if device.enrollment_id != current.enrollment_id or device.name != current.name:
            raise ValueError("Registration response does not match the saved request")
        confirmed = current.model_copy(update={"device": device})
        with self._db:
            self._db.execute("UPDATE registration SET record=? WHERE credential=?",
                             (confirmed.model_dump_json(), self._credentials.reference))
        return confirmed
