"""Relay-owned device records, separate from personal SSH/HTTP configuration.

Callers must authenticate the account and enrollment permission before invoking
this storage layer. It does not validate bearer tokens or authorize tool calls.
"""

import re
import sqlite3
import uuid
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .authorization import validate_authorization_url
from .devices import device_name
from .state import prepare_directory


class RelayAccount(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid",
                              revalidate_instances="always")
    issuer: str
    subject: str = Field(min_length=1, max_length=255, pattern=r"^[\x21-\x7e]+$")


class RelayDevice(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    device_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    enrollment_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    name: str
    state: Literal["registered", "revoked"]


class RelayRegistry:
    """Synchronous transaction boundary; one connection per worker thread.

    Registration is durable, not evidence that a transport or engine is ready.
    Revoked records remain as tombstones so replay cannot resurrect a device.
    """

    def __init__(self, directory: Path) -> None:
        prepare_directory(directory)
        path = directory / "relay-devices.sqlite3"
        if path.is_symlink():
            raise ValueError("Relay registry must not be a symbolic link")
        self.db = sqlite3.connect(path, timeout=5)
        self.db.row_factory = sqlite3.Row
        try:
            with self.db:
                self.db.execute("BEGIN IMMEDIATE")
                version = self.db.execute("PRAGMA user_version").fetchone()[0]
                if version not in (0, 1, 2):
                    raise ValueError("Unsupported relay registry schema")
                self.db.execute(
                    "CREATE TABLE IF NOT EXISTS relay_devices ("
                    "device_id TEXT PRIMARY KEY, issuer TEXT NOT NULL, subject TEXT NOT NULL,"
                    "enrollment_id TEXT NOT NULL, name TEXT NOT NULL,"
                    "state TEXT NOT NULL CHECK(state IN ('registered','revoked')),"
                    "UNIQUE(issuer,subject,enrollment_id))"
                )
                self.db.execute(
                    "CREATE TABLE IF NOT EXISTS relay_channels ("
                    "fingerprint TEXT PRIMARY KEY, device_id TEXT NOT NULL UNIQUE)"
                )
                self.db.execute("PRAGMA user_version=2")
        except BaseException:
            self.db.close()
            raise

    def close(self) -> None:
        self.db.close()

    @staticmethod
    def _account(account: RelayAccount) -> RelayAccount:
        account = RelayAccount.model_validate(account)
        validate_authorization_url(account.issuer)
        return account

    @staticmethod
    def _device(row: sqlite3.Row) -> RelayDevice:
        return RelayDevice(device_id=row["device_id"], enrollment_id=row["enrollment_id"],
                           name=row["name"], state=row["state"])

    def register(self, account: RelayAccount, *, enrollment_id: str, name: str) -> RelayDevice:
        account = self._account(account)
        proposed = RelayDevice(device_id=uuid.uuid4().hex, enrollment_id=enrollment_id,
                               name=device_name(name), state="registered")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            existing = self.db.execute(
                "SELECT * FROM relay_devices WHERE issuer=? AND subject=? AND enrollment_id=?",
                (account.issuer, account.subject, enrollment_id),
            ).fetchone()
            if existing is not None:
                device = self._device(existing)
                if device.name != proposed.name:
                    raise ValueError("Enrollment request conflicts with its saved registration")
                return device
            self.db.execute(
                "INSERT INTO relay_devices VALUES(?,?,?,?,?,?)",
                (proposed.device_id, account.issuer, account.subject, enrollment_id,
                 proposed.name, proposed.state),
            )
        return proposed

    def get(self, account: RelayAccount, device_id: str) -> RelayDevice:
        account = self._account(account)
        row = self.db.execute(
            "SELECT * FROM relay_devices WHERE issuer=? AND subject=? AND device_id=?",
            (account.issuer, account.subject, device_id),
        ).fetchone()
        if row is None:
            raise ValueError("Device is not registered to this account")
        return self._device(row)

    def revoke(self, account: RelayAccount, device_id: str) -> RelayDevice:
        account = self._account(account)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self.get(account, device_id)
            self.db.execute(
                "UPDATE relay_devices SET state='revoked' "
                "WHERE issuer=? AND subject=? AND device_id=?",
                (account.issuer, account.subject, device_id),
            )
            return self.get(account, device_id)

    def bind_channel(self, account: RelayAccount, device_id: str, *, fingerprint: str) -> None:
        """Bind a verified PC certificate after authenticated provisioning.

        Fingerprints are public identities, not bearer credentials. The caller
        must verify certificate possession and enrollment authorization. Existing
        bindings cannot be silently replaced or moved to another device.
        """
        self._fingerprint(fingerprint)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            if self.get(account, device_id).state != "registered":
                raise ValueError("Device registration is revoked")
            existing = self.db.execute(
                "SELECT fingerprint,device_id FROM relay_channels "
                "WHERE fingerprint=? OR device_id=?", (fingerprint, device_id),
            ).fetchall()
            if existing:
                if (len(existing) == 1 and existing[0]["fingerprint"] == fingerprint
                        and existing[0]["device_id"] == device_id):
                    return
                raise ValueError("PC channel identity is already bound")
            self.db.execute("INSERT INTO relay_channels VALUES(?,?)", (fingerprint, device_id))

    def rotate_channel(self, account: RelayAccount, device_id: str, *,
                       expected_fingerprint: str, fingerprint: str) -> None:
        """Trusted provisioning only: replace an exact binding atomically.

        The caller must authenticate renewal and prove possession of the new
        certificate. Fingerprint knowledge alone is not authorization. A stale
        renewal never overwrites a later binding; the device and ledger IDs stay
        unchanged. Existing channels recheck their old fingerprint on dispatch.
        """
        self._fingerprint(expected_fingerprint)
        self._fingerprint(fingerprint)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            if self.get(account, device_id).state != "registered":
                raise ValueError("Device registration is revoked")
            existing = self.db.execute(
                "SELECT fingerprint FROM relay_channels WHERE device_id=?", (device_id,),
            ).fetchone()
            if existing is None or existing["fingerprint"] != expected_fingerprint:
                raise ValueError("PC channel binding changed; inspect before renewal")
            conflict = self.db.execute(
                "SELECT device_id FROM relay_channels WHERE fingerprint=?", (fingerprint,),
            ).fetchone()
            if conflict is not None and conflict["device_id"] != device_id:
                raise ValueError("PC channel identity is already bound")
            self.db.execute(
                "UPDATE relay_channels SET fingerprint=? WHERE device_id=? AND fingerprint=?",
                (fingerprint, device_id, expected_fingerprint),
            )

    @staticmethod
    def _fingerprint(fingerprint: str) -> None:
        if re.fullmatch(r"[a-f0-9]{64}", fingerprint) is None:
            raise ValueError("Invalid PC certificate fingerprint")

    def channel_device(self, fingerprint: str) -> tuple[RelayAccount, RelayDevice]:
        """Resolve an authenticated channel; recheck on each dispatch.

        Never call this with a self-reported fingerprint from a message. A TLS
        verifier supplies it from the actual peer certificate. Revocation remains
        authoritative even when the network connection has not closed yet.
        """
        self._fingerprint(fingerprint)
        row = self.db.execute(
            "SELECT d.* FROM relay_devices d JOIN relay_channels c "
            "ON d.device_id=c.device_id WHERE c.fingerprint=? AND d.state='registered'",
            (fingerprint,),
        ).fetchone()
        if row is None:
            raise ValueError("PC channel is unknown or revoked")
        account = self._account(RelayAccount(issuer=row["issuer"], subject=row["subject"]))
        return account, self._device(row)
