"""Persistent local device selection with no stored SSH credentials (stdlib only)."""

import json
import sqlite3
import subprocess
import time
import unicodedata
import uuid
from pathlib import Path

from .ssh_transport import ssh_command, validate_ssh_host
from .state import prepare_directory

DeviceData = dict[str, str | float | None]


def device_name(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).strip()
    if (
        not normalized
        or len(normalized) > 120
        or any(unicodedata.category(char).startswith("C") for char in normalized)
    ):
        raise ValueError("Device name must contain 1–120 printable characters")
    return normalized


class DeviceStore:
    def __init__(self, directory: Path) -> None:
        prepare_directory(directory)
        self.db = sqlite3.connect(directory / "devices.sqlite3", timeout=10)
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1):
            self.db.close()
            raise ValueError("Device registry version is unsupported")
        with self.db:
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS devices ("
                "id TEXT PRIMARY KEY, name TEXT NOT NULL, name_key TEXT UNIQUE NOT NULL, "
                "ssh_host TEXT UNIQUE NOT NULL COLLATE NOCASE, "
                "observed TEXT NOT NULL DEFAULT 'unknown', checked REAL, detail TEXT)"
            )
            self.db.execute("PRAGMA user_version=1")

    def add(self, name: str, host: str) -> DeviceData:
        name = device_name(name)
        validate_ssh_host(host)
        identity = uuid.uuid4().hex
        try:
            with self.db:
                self.db.execute(
                    "INSERT INTO devices(id,name,name_key,ssh_host) VALUES(?,?,?,?)",
                    (identity, name, name.casefold(), host),
                )
        except sqlite3.IntegrityError:
            raise ValueError("A device with this name or SSH alias is already registered") from None
        return self.get(identity)

    def get(self, identity: str) -> DeviceData:
        row = self.db.execute(
            "SELECT id,name,ssh_host,observed,checked,detail FROM devices WHERE id=?", (identity,)
        ).fetchone()
        if row is None:
            raise ValueError("Device ID is not registered")
        checked = float(row[4]) if row[4] is not None else None
        age = max(0.0, time.time() - checked) if checked is not None else None
        return {
            "device_id": str(row[0]),
            "name": str(row[1]),
            "ssh_host": str(row[2]),
            "last_observed_state": str(row[3]),
            "checked_at": checked,
            "observation_age_seconds": age,
            "state": str(row[3]) if age is not None and age <= 60 else "unknown",
            "detail": str(row[5]) if row[5] is not None else None,
        }

    def list(self) -> list[DeviceData]:
        return [
            self.get(str(row[0]))
            for row in self.db.execute("SELECT id FROM devices ORDER BY name_key,id").fetchall()
        ]

    def rename(self, identity: str, name: str) -> DeviceData:
        name = device_name(name)
        self.get(identity)
        try:
            with self.db:
                self.db.execute(
                    "UPDATE devices SET name=?,name_key=? WHERE id=?",
                    (name, name.casefold(), identity),
                )
        except sqlite3.IntegrityError:
            raise ValueError("A device with this name already exists") from None
        return self.get(identity)

    def remove(self, identity: str) -> None:
        with self.db:
            cursor = self.db.execute("DELETE FROM devices WHERE id=?", (identity,))
            if cursor.rowcount != 1:
                raise ValueError("Device ID is not registered")

    def record(self, identity: str, state: str, detail: str) -> DeviceData:
        if state not in {"ready", "unreachable", "not_ready"}:
            raise ValueError("Invalid device observation")
        with self.db:
            changed = self.db.execute(
                "UPDATE devices SET observed=?,checked=?,detail=? WHERE id=?",
                (state, time.time(), detail, identity),
            )
            if changed.rowcount != 1:
                raise ValueError("Device was removed during the check")
        return self.get(identity)

    def probe(self, identity: str) -> DeviceData:
        device = self.get(identity)
        try:
            reply = subprocess.run(
                ssh_command(str(device["ssh_host"]), "status"),
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            if reply.returncode != 0:
                return self.record(
                    identity, "unreachable", "SSH connection or remote command failed"
                )
            message = json.loads(reply.stdout)
            if not isinstance(message, dict) or not isinstance(message.get("data"), dict):
                raise ValueError("Invalid status response")
            if message.get("state") == "completed" and message["data"].get("state") == "ready":
                return self.record(
                    identity, "ready", "Agent answered the authenticated status check"
                )
            return self.record(identity, "not_ready", "Remote agent did not report ready")
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired):
            return self.record(identity, "unreachable", "Status check failed or timed out")

    def close(self) -> None:
        self.db.close()
