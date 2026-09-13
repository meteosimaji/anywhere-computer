"""Durable registration attempts; grants remain in the existing OS vault."""

import sqlite3
import uuid
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field

from .authorization import validate_authorization_url
from .devices import device_name
from .enrollment_credentials import EnrollmentCredentials
from .enrollment_http import EnrollmentHTTPReply, https_enrollment_registration
from .locking import ProcessLock
from .relay_registry import RelayDevice
from .state import prepare_directory


class RegistrationAttempt(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    attempt_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    enrollment_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    name: str
    device: RelayDevice | None = None


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
                 endpoint: str, wire: RegistrationWire = _send) -> None:
        validate_authorization_url(endpoint)
        if urlsplit(endpoint).query:
            raise ValueError("Registration endpoint must not have a query")
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
                if version not in (0, 1):
                    raise ValueError("Unsupported registration state version")
                self._db.execute("CREATE TABLE IF NOT EXISTS registration ("
                                 "credential TEXT PRIMARY KEY, endpoint TEXT NOT NULL, "
                                 "record TEXT NOT NULL)")
                self._db.execute("PRAGMA user_version=1")
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

    def register(self, *, attempt_id: str, name: str) -> RegistrationAttempt:
        name = device_name(name)
        proposed = RegistrationAttempt(attempt_id=attempt_id, enrollment_id=uuid.uuid4().hex,
                                       name=name)
        with ProcessLock(self._lock, timeout=0):
            current = self.current()
            if current is None:
                # Verify the grant before creating new local registration state.
                self._credentials.access_token(attempt_id=attempt_id, scope="device:enroll")
                with self._db:
                    self._db.execute("INSERT INTO registration VALUES(?,?,?)", (
                        self._credentials.reference, self._endpoint, proposed.model_dump_json(),
                    ))
                current = proposed
            if current.attempt_id != attempt_id or current.name != name:
                raise ValueError("Saved registration must be recovered with its original request")
            if current.device is not None:
                return current
            token = self._credentials.access_token(attempt_id=attempt_id, scope="device:enroll")
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
