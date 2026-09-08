"""Opaque authorization grants for explicitly approved devices (stdlib only).

The caller must authenticate the owner and obtain informed approval before
calling approve. This module does not authenticate a browser user or expose
an authorization endpoint. It stores only hashes of codes and access tokens.
"""

import base64
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from .state import prepare_directory


class AuthorizationError(ValueError):
    """Intentionally excludes request secrets from the public error message."""


@dataclass(frozen=True)
class GrantIdentity:
    grant_id: str
    owner: str
    device: str
    client: str
    resource: str
    tools: frozenset[str]


@dataclass(frozen=True)
class AccessToken:
    value: str = field(repr=False)
    expires_in: int
    scope: str
    refresh_value: str = field(repr=False)


def pkce_s256(verifier: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9._~-]{43,128}", verifier) is None:
        raise AuthorizationError("invalid_grant")
    return (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )


def _identifier(value: str) -> None:
    if not value or len(value) > 128 or any(ord(char) < 33 or ord(char) > 126 for char in value):
        raise ValueError("Invalid authorization identifier")


def validate_authorization_url(value: str, *, loopback: bool = False) -> None:
    if (
        not value
        or len(value) > 2048
        or any(ord(char) <= 32 or ord(char) >= 127 or char in '\\"<>#' for char in value)
    ):
        raise ValueError("Invalid authorization URL")
    parsed = urlsplit(value)
    if (
        not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or (
            parsed.scheme != "https"
            and not (
                loopback and parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "::1"}
            )
        )
    ):
        raise ValueError("Authorization URL must use HTTPS (or an explicit loopback callback)")
    # Accessing port also rejects malformed port text/ranges.
    _ = parsed.port


def _secret_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class AuthorizationStore:
    def __init__(self, directory: Path, *, resource: str, known_tools: frozenset[str]) -> None:
        validate_authorization_url(resource)
        if urlsplit(resource).query:
            raise ValueError("Resource URL must not contain a query")
        self.resource = resource
        self.known_tools = known_tools - {"operations_recent"}
        prepare_directory(directory)
        self.db = sqlite3.connect(directory / "authorization.sqlite3", timeout=10)
        self.db.execute("PRAGMA foreign_keys=ON")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in {0, 1, 2}:
            self.db.close()
            raise ValueError("Unsupported authorization store version")
        try:
            with self.db:
                self.db.execute("BEGIN IMMEDIATE")
                self.db.execute("CREATE TABLE IF NOT EXISTS settings (resource TEXT PRIMARY KEY)")
                resources = self.db.execute("SELECT resource FROM settings").fetchall()
                if resources and resources != [(resource,)]:
                    raise ValueError("Authorization store belongs to a different resource")
                self.db.execute("INSERT OR IGNORE INTO settings VALUES(?)", (resource,))
                self.db.execute(
                    "CREATE TABLE IF NOT EXISTS clients "
                    "(id TEXT PRIMARY KEY, redirects TEXT NOT NULL)"
                )
                self.db.execute(
                    "CREATE TABLE IF NOT EXISTS authorized_devices ("
                    "id TEXT PRIMARY KEY, owner TEXT NOT NULL, tools TEXT NOT NULL, "
                    "active INTEGER NOT NULL)"
                )
                self.db.execute(
                    "CREATE TABLE IF NOT EXISTS grants (id TEXT PRIMARY KEY, owner TEXT NOT NULL, "
                    "device TEXT NOT NULL REFERENCES authorized_devices(id), "
                    "client TEXT NOT NULL REFERENCES clients(id), tools TEXT NOT NULL, "
                    "expires REAL NOT NULL, revoked INTEGER NOT NULL DEFAULT 0)"
                )
                self.db.execute(
                    "CREATE TABLE IF NOT EXISTS codes (digest TEXT PRIMARY KEY, "
                    "grant_id TEXT NOT NULL REFERENCES grants(id), redirect TEXT NOT NULL, "
                    "challenge TEXT NOT NULL, expires REAL NOT NULL, "
                    "consumed INTEGER NOT NULL DEFAULT 0)"
                )
                self.db.execute(
                    "CREATE TABLE IF NOT EXISTS tokens (digest TEXT PRIMARY KEY, "
                    "grant_id TEXT NOT NULL REFERENCES grants(id), expires REAL NOT NULL)"
                )
                self.db.execute(
                    "CREATE TABLE IF NOT EXISTS refresh_tokens (digest TEXT PRIMARY KEY, "
                    "grant_id TEXT NOT NULL REFERENCES grants(id), expires REAL NOT NULL, "
                    "consumed INTEGER NOT NULL DEFAULT 0)"
                )
                self.db.execute("PRAGMA user_version=2")
        except Exception:
            self.db.close()
            raise

    def _tools(self, tools: frozenset[str]) -> str:
        if not tools or tools - self.known_tools:
            raise ValueError("Permission set is empty or contains unsupported tools")
        return json.dumps(sorted(tools))

    def register_client(self, client: str, redirects: frozenset[str]) -> None:
        _identifier(client)
        if not redirects or len(redirects) > 10:
            raise ValueError("Register 1–10 exact callback URLs")
        for redirect in redirects:
            validate_authorization_url(redirect, loopback=True)
        with self.db:
            self.db.execute(
                "INSERT INTO clients VALUES(?,?)", (client, json.dumps(sorted(redirects)))
            )

    def enroll_device(self, owner: str, device: str, tools: frozenset[str]) -> None:
        _identifier(owner)
        _identifier(device)
        encoded = self._tools(tools)
        with self.db:
            self.db.execute(
                "INSERT INTO authorized_devices VALUES(?,?,?,1)", (device, owner, encoded)
            )

    def approve(
        self,
        *,
        owner: str,
        device: str,
        client: str,
        redirect: str,
        resource: str,
        tools: frozenset[str],
        challenge: str,
    ) -> str:
        """Trusted consent boundary: never invoke directly from unverified HTTP inputs."""
        if resource != self.resource or re.fullmatch(r"[A-Za-z0-9_-]{43}", challenge) is None:
            raise AuthorizationError("invalid_request")
        encoded = self._tools(tools)
        code = secrets.token_urlsafe(32)
        now = time.time()
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            registered = self.db.execute(
                "SELECT redirects FROM clients WHERE id=?", (client,)
            ).fetchone()
            target = self.db.execute(
                "SELECT owner,tools,active FROM authorized_devices WHERE id=?", (device,)
            ).fetchone()
            if (
                registered is None
                or redirect not in json.loads(registered[0])
                or target is None
                or target[0] != owner
                or not target[2]
                or tools - frozenset(json.loads(target[1]))
            ):
                raise AuthorizationError("access_denied")
            grant = secrets.token_hex(16)
            self.db.execute(
                "INSERT INTO grants(id,owner,device,client,tools,expires) VALUES(?,?,?,?,?,?)",
                (grant, owner, device, client, encoded, now + 86400),
            )
            self.db.execute(
                "INSERT INTO codes(digest,grant_id,redirect,challenge,expires) VALUES(?,?,?,?,?)",
                (_secret_digest(code), grant, redirect, challenge, now + 120),
            )
        return code

    def exchange_code(
        self,
        *,
        code: str,
        verifier: str,
        client: str,
        redirect: str,
        resource: str,
    ) -> AccessToken:
        if len(code) > 256 or resource != self.resource:
            raise AuthorizationError("invalid_grant")
        challenge = pkce_s256(verifier)
        now = time.time()
        invalid = False
        issued: AccessToken | None = None
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute(
                "SELECT c.grant_id,c.redirect,c.challenge,c.expires,c.consumed,g.client,g.tools,"
                "g.expires "
                "FROM codes c JOIN grants g ON g.id=c.grant_id WHERE c.digest=?",
                (_secret_digest(code),),
            ).fetchone()
            if (
                row is None
                or row[1] != redirect
                or row[5] != client
                or not hmac.compare_digest(str(row[2]), challenge)
            ):
                raise AuthorizationError("invalid_grant")
            grant = str(row[0])
            if row[4]:
                # A valid second redemption also invalidates tokens from the original exchange.
                self.db.execute("UPDATE grants SET revoked=1 WHERE id=?", (grant,))
                invalid = True
            elif row[3] <= now or self._grant(grant, now) is None:
                invalid = True
            else:
                self.db.execute(
                    "UPDATE codes SET consumed=1 WHERE digest=?", (_secret_digest(code),)
                )
                issued = self._issue_tokens(
                    grant, frozenset(json.loads(row[6])), float(row[7]), now
                )
        if invalid:
            raise AuthorizationError("invalid_grant")
        assert issued is not None
        return issued

    def _issue_tokens(
        self, grant: str, tools: frozenset[str], grant_expires: float, now: float
    ) -> AccessToken:
        """Called only inside a code/refresh redemption transaction."""
        lifetime = min(900, int(grant_expires - now))
        if lifetime < 1:
            raise AuthorizationError("invalid_grant")
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        self.db.execute(
            "INSERT INTO tokens VALUES(?,?,?)", (_secret_digest(access), grant, now + lifetime)
        )
        self.db.execute(
            "INSERT INTO refresh_tokens(digest,grant_id,expires) VALUES(?,?,?)",
            (_secret_digest(refresh), grant, grant_expires),
        )
        return AccessToken(access, lifetime, " ".join(sorted(tools)), refresh)

    def refresh(
        self,
        *,
        refresh_token: str,
        client: str,
        resource: str,
        scope: frozenset[str] | None = None,
    ) -> AccessToken:
        if len(refresh_token) > 256 or resource != self.resource:
            raise AuthorizationError("invalid_grant")
        now = time.time()
        issued: AccessToken | None = None
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute(
                "SELECT r.grant_id,r.expires,r.consumed,g.client,g.expires "
                "FROM refresh_tokens r JOIN grants g ON g.id=r.grant_id WHERE r.digest=?",
                (_secret_digest(refresh_token),),
            ).fetchone()
            if row is None or row[3] != client:
                raise AuthorizationError("invalid_grant")
            grant_id = str(row[0])
            if row[2]:
                self.db.execute("UPDATE grants SET revoked=1 WHERE id=?", (grant_id,))
            else:
                grant = self._grant(grant_id, now)
                if row[1] <= now or grant is None:
                    raise AuthorizationError("invalid_grant")
                # This policy keeps one grant's permissions stable across refreshes.
                # Permission changes require new consent; they are never silently expanded.
                if scope is not None and scope != grant.tools:
                    raise AuthorizationError("invalid_scope")
                self.db.execute(
                    "UPDATE refresh_tokens SET consumed=1 WHERE digest=?",
                    (_secret_digest(refresh_token),),
                )
                issued = self._issue_tokens(grant_id, grant.tools, float(row[4]), now)
        if issued is None:
            raise AuthorizationError("invalid_grant")
        return issued

    def _grant(self, grant: str, now: float) -> GrantIdentity | None:
        row = self.db.execute(
            "SELECT g.owner,g.device,g.client,g.tools,g.expires,g.revoked,d.owner,d.tools,d.active "
            "FROM grants g JOIN authorized_devices d ON g.device=d.id WHERE g.id=?",
            (grant,),
        ).fetchone()
        if row is None or row[4] <= now or row[5] or not row[8] or row[0] != row[6]:
            return None
        tools = frozenset(json.loads(row[3]))
        if tools - frozenset(json.loads(row[7])) or tools - self.known_tools:
            return None
        return GrantIdentity(grant, str(row[0]), str(row[1]), str(row[2]), self.resource, tools)

    def verify(self, token: str, *, resource: str) -> GrantIdentity | None:
        if len(token) > 256 or resource != self.resource:
            return None
        row = self.db.execute(
            "SELECT grant_id,expires FROM tokens WHERE digest=?", (_secret_digest(token),)
        ).fetchone()
        now = time.time()
        if row is None or row[1] <= now:
            return None
        return self._grant(str(row[0]), now)

    def current_grant(self, grant: str) -> GrantIdentity | None:
        """Internal grant metadata lookup; this does not authenticate a request."""
        return self._grant(grant, time.time())

    def revoke(self, *, owner: str, grant: str) -> None:
        with self.db:
            changed = self.db.execute(
                "UPDATE grants SET revoked=1 WHERE id=? AND owner=?", (grant, owner)
            )
            if changed.rowcount != 1:
                raise AuthorizationError("access_denied")

    def revoke_device(self, *, owner: str, device: str) -> None:
        with self.db:
            changed = self.db.execute(
                "UPDATE authorized_devices SET active=0 WHERE id=? AND owner=?", (device, owner)
            )
            if changed.rowcount != 1:
                raise AuthorizationError("access_denied")

    def close(self) -> None:
        self.db.close()
