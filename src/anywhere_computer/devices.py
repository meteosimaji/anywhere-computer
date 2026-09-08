"""Persistent SSH/HTTP device selection; only public connection metadata is stored."""

import json
import sqlite3
import subprocess
import time
import unicodedata
import uuid
from pathlib import Path

from .client_tokens import (
    ClientAuthorizationRequired,
    ClientCredentialError,
    ClientTokens,
    CredentialVault,
    validate_client_profile,
)
from .http_client import HTTPBackend, HTTPWire, https_mcp_request
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
        self.directory = directory.resolve()
        self.db = sqlite3.connect(directory / "devices.sqlite3", timeout=10)
        try:
            with self.db:
                self.db.execute("BEGIN IMMEDIATE")
                version = self.db.execute("PRAGMA user_version").fetchone()[0]
                if version not in (0, 1, 2):
                    raise ValueError("Device registry version is unsupported")
                if version == 1:
                    self.db.execute("ALTER TABLE devices RENAME TO devices_v1")
                if version != 2:
                    self.db.execute(
                        "CREATE TABLE devices ("
                        "id TEXT PRIMARY KEY, name TEXT NOT NULL, name_key TEXT UNIQUE NOT NULL, "
                        "transport TEXT NOT NULL CHECK(transport IN ('ssh','http')), "
                        "endpoint_key TEXT UNIQUE NOT NULL, ssh_host TEXT UNIQUE COLLATE NOCASE, "
                        "resource TEXT, client TEXT, profile TEXT, "
                        "observed TEXT NOT NULL DEFAULT 'unknown', checked REAL, detail TEXT, "
                        "CHECK((transport='ssh' AND ssh_host IS NOT NULL AND resource IS NULL "
                        "AND client IS NULL AND profile IS NULL) OR "
                        "(transport='http' AND ssh_host IS NULL AND resource IS NOT NULL "
                        "AND client IS NOT NULL AND profile IS NOT NULL)))"
                    )
                if version == 1:
                    self.db.execute(
                        "INSERT INTO devices(id,name,name_key,transport,endpoint_key,ssh_host,"
                        "observed,checked,detail) SELECT id,name,name_key,'ssh',"
                        "'ssh:'||lower(ssh_host),"
                        "ssh_host,observed,checked,detail FROM devices_v1"
                    )
                    self.db.execute("DROP TABLE devices_v1")
                self.db.execute("PRAGMA user_version=2")
        except Exception:
            self.db.close()
            raise

    def add(self, name: str, host: str) -> DeviceData:
        name = device_name(name)
        validate_ssh_host(host)
        identity = uuid.uuid4().hex
        try:
            with self.db:
                self.db.execute(
                    "INSERT INTO devices(id,name,name_key,transport,endpoint_key,ssh_host) "
                    "VALUES(?,?,?,'ssh',?,?)",
                    (identity, name, name.casefold(), "ssh:" + host.lower(), host),
                )
        except sqlite3.IntegrityError:
            raise ValueError("A device with this name or SSH alias is already registered") from None
        return self.get(identity)

    def add_http(self, name: str, resource: str, client: str, profile: str) -> DeviceData:
        name = device_name(name)
        validate_client_profile(resource, client, profile)
        identity = uuid.uuid4().hex
        endpoint = json.dumps(["http", resource, client, profile])
        try:
            with self.db:
                self.db.execute(
                    "INSERT INTO devices(id,name,name_key,transport,endpoint_key,"
                    "resource,client,profile) "
                    "VALUES(?,?,?,'http',?,?,?,?)",
                    (identity, name, name.casefold(), endpoint, resource, client, profile),
                )
        except sqlite3.IntegrityError:
            raise ValueError(
                "A device with this name or HTTP connection is already registered"
            ) from None
        return self.get(identity)

    def named(self, name: str) -> DeviceData:
        row = self.db.execute(
            "SELECT id FROM devices WHERE name_key=?", (device_name(name).casefold(),)
        ).fetchone()
        if row is None:
            raise ValueError("Device name is not registered")
        return self.get(str(row[0]))

    def get(self, identity: str) -> DeviceData:
        row = self.db.execute(
            "SELECT id,name,ssh_host,observed,checked,detail,transport,resource,client,profile "
            "FROM devices WHERE id=?",
            (identity,),
        ).fetchone()
        if row is None:
            raise ValueError("Device ID is not registered")
        checked = float(row[4]) if row[4] is not None else None
        elapsed = time.time() - checked if checked is not None else None
        age = elapsed if elapsed is not None and elapsed >= 0 else None
        return {
            "device_id": str(row[0]),
            "name": str(row[1]),
            "ssh_host": str(row[2]) if row[2] is not None else None,
            "transport": str(row[6]),
            "resource": str(row[7]) if row[7] is not None else None,
            "client_id": str(row[8]) if row[8] is not None else None,
            "profile": str(row[9]) if row[9] is not None else None,
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
        if state not in {
            "ready",
            "unreachable",
            "not_ready",
            "authorization_required",
            "credential_unavailable",
        }:
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
        if device["transport"] != "ssh":
            raise ValueError("HTTP devices require the asynchronous HTTP status check")
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

    async def probe_http(
        self,
        identity: str,
        *,
        vault: CredentialVault | None = None,
        wire: HTTPWire = https_mcp_request,
    ) -> DeviceData:
        device = self.get(identity)
        if device["transport"] != "http":
            raise ValueError("This device does not use HTTP")
        try:
            tokens = ClientTokens(
                self.directory,
                resource=str(device["resource"]),
                client=str(device["client_id"]),
                profile=str(device["profile"]),
                vault=vault,
            )
        except (ClientCredentialError, RuntimeError):
            return self.record(
                identity, "credential_unavailable", "Native credential store is unavailable"
            )
        backend = HTTPBackend(tokens, wire=wire)
        try:
            await backend.catalog()
            return self.record(identity, "ready", "Authorized HTTP MCP catalog was verified")
        except ClientAuthorizationRequired:
            return self.record(
                identity, "authorization_required", "Authorize this connection with login"
            )
        except ClientCredentialError:
            return self.record(
                identity, "credential_unavailable", "Native credential store is unavailable"
            )
        except (OSError, ValueError, RuntimeError):
            return self.record(identity, "not_ready", "HTTP MCP connection could not be verified")
        finally:
            await backend.close()

    def close(self) -> None:
        self.db.close()
