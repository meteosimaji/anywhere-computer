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

LOCAL_ONLY_TOOLS = frozenset({"operations_recent"})


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


def _registered_redirect(redirect: str, registered: list[str]) -> bool:
    """RFC 8252 loopback exception: change only the port, never the host/path/query."""
    validate_authorization_url(redirect, loopback=True)
    if redirect in registered:
        return True
    pattern = r"http://(127\.0\.0\.1|\[::1\])(?::([0-9]+))?([/?].*|)"
    requested = re.fullmatch(pattern, redirect)
    if requested is None or not requested[2] or not 1 <= int(requested[2]) <= 65535:
        return False
    for value in registered:
        saved = re.fullmatch(pattern, value)
        if saved and requested[1] == saved[1] and requested[3] == saved[3]:
            return True
    return False


class AuthorizationStore:
    def __init__(self, directory: Path, *, resource: str, known_tools: frozenset[str]) -> None:
        validate_authorization_url(resource)
        if urlsplit(resource).query:
            raise ValueError("Resource URL must not contain a query")
        self.resource = resource
        self.known_tools = known_tools - LOCAL_ONLY_TOOLS
        prepare_directory(directory)
        self.db = sqlite3.connect(directory / "authorization.sqlite3", timeout=10)
        self.db.execute("PRAGMA foreign_keys=ON")
        try:
            with self.db:
                self.db.execute("BEGIN IMMEDIATE")
                version = self.db.execute("PRAGMA user_version").fetchone()[0]
                if version not in {0, 1, 2, 3}:
                    raise ValueError("Unsupported authorization store version")
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
                    "active INTEGER NOT NULL, generation INTEGER NOT NULL DEFAULT 0)"
                )
                if version < 3 and "generation" not in {
                    row[1] for row in self.db.execute("PRAGMA table_info(authorized_devices)")
                }:
                    self.db.execute(
                        "ALTER TABLE authorized_devices ADD COLUMN generation INTEGER NOT NULL "
                        "DEFAULT 0"
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
                self.db.execute("PRAGMA user_version=3")
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
                "INSERT INTO authorized_devices(id,owner,tools,active) VALUES(?,?,?,1)",
                (device, owner, encoded),
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
        generation: int | None = None,
    ) -> str:
        """Trusted consent boundary: never invoke directly from unverified HTTP inputs."""
        if resource != self.resource or re.fullmatch(r"[A-Za-z0-9_-]{43}", challenge) is None:
            raise AuthorizationError("invalid_request")
        encoded = self._tools(tools)
        code = secrets.token_urlsafe(32)
        now = time.time()
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self.validate_consent(
                owner=owner,
                device=device,
                client=client,
                redirect=redirect,
                resource=resource,
                tools=tools,
                challenge=challenge,
                generation=generation,
            )
            grant = secrets.token_hex(16)
            self.db.execute(
                "INSERT INTO grants(id,owner,device,client,tools,expires) VALUES(?,?,?,?,?,?)",
                (grant, owner, device, client, encoded, 0),
            )
            self.db.execute(
                "INSERT INTO codes(digest,grant_id,redirect,challenge,expires) VALUES(?,?,?,?,?)",
                (_secret_digest(code), grant, redirect, challenge, now + 120),
            )
        return code

    def validate_consent(
        self,
        *,
        owner: str,
        device: str,
        client: str,
        redirect: str,
        resource: str,
        tools: frozenset[str],
        challenge: str,
        generation: int | None = None,
    ) -> int:
        """Read-only validation for a consent page; approve checks again in its transaction."""
        if resource != self.resource or re.fullmatch(r"[A-Za-z0-9_-]{43}", challenge) is None:
            raise AuthorizationError("invalid_request")
        self._tools(tools)
        registered = self.db.execute(
            "SELECT redirects FROM clients WHERE id=?", (client,)
        ).fetchone()
        target = self.db.execute(
            "SELECT owner,tools,active,generation FROM authorized_devices WHERE id=?", (device,)
        ).fetchone()
        if (
            registered is None
            or not _registered_redirect(redirect, json.loads(registered[0]))
            or target is None
            or target[0] != owner
            or not target[2]
            or tools - frozenset(json.loads(target[1]))
            or (generation is not None and generation != target[3])
        ):
            raise AuthorizationError("access_denied")
        return int(target[3])

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
        lifetime = 900 if grant_expires == 0 else min(900, int(grant_expires - now))
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
                if (row[1] != 0 and row[1] <= now) or grant is None:
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
        if (
            row is None or (row[4] != 0 and row[4] <= now)
            or row[5] or not row[8] or row[0] != row[6]
        ):
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

    def retain_active_grants(self, *, owner: str, device: str, client: str) -> int:
        """Local owner administration: remove deadlines only from still-valid grants.

        Zero means no time deadline, never bypassing revocation or device/scope checks.
        Expired refresh credentials and consumed tokens are not revived.
        """
        now = time.time()
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            if not self.device_enabled(owner=owner, device=device):
                raise AuthorizationError("access_denied")
            candidates = self.db.execute(
                "SELECT id FROM grants WHERE owner=? AND device=? AND client=? "
                "AND expires>? AND revoked=0", (owner, device, client, now),
            ).fetchall()
            changed = 0
            for (grant_id,) in candidates:
                if self._grant(grant_id, now) is None:
                    continue
                self.db.execute("UPDATE grants SET expires=0 WHERE id=?", (grant_id,))
                self.db.execute(
                    "UPDATE refresh_tokens SET expires=0 WHERE grant_id=? "
                    "AND consumed=0 AND expires>?", (grant_id, now),
                )
                changed += 1
        return changed

    def revoke(self, *, owner: str, grant: str) -> None:
        with self.db:
            changed = self.db.execute(
                "UPDATE grants SET revoked=1 WHERE id=? AND owner=?", (grant, owner)
            )
            if changed.rowcount != 1:
                raise AuthorizationError("access_denied")

    def revoke_device(self, *, owner: str, device: str) -> None:
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            changed = self.db.execute(
                "UPDATE authorized_devices SET active=0,generation=generation+1 "
                "WHERE id=? AND owner=?",
                (device, owner),
            )
            if changed.rowcount != 1:
                raise AuthorizationError("access_denied")
            self.db.execute("UPDATE grants SET revoked=1 WHERE device=?", (device,))

    def device_enabled(self, *, owner: str, device: str) -> bool:
        row = self.db.execute(
            "SELECT active FROM authorized_devices WHERE id=? AND owner=?", (device, owner)
        ).fetchone()
        if row is None:
            raise AuthorizationError("access_denied")
        return bool(row[0])

    def enable_device(self, *, owner: str, device: str) -> bool:
        """Trusted administration: permit new consent without reviving old grants.

        Return whether the disabled device changed state. Repeating this command
        on an enabled device preserves its newly approved grants.
        """
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            if self.device_enabled(owner=owner, device=device):
                return False
            # Older registries disabled devices without marking grants revoked.
            # Invalidate every old code/access/refresh family before reenabling.
            self.db.execute("UPDATE grants SET revoked=1 WHERE device=?", (device,))
            self.db.execute(
                "UPDATE authorized_devices SET active=1,generation=generation+1 "
                "WHERE id=? AND owner=?",
                (device, owner),
            )
        return True

    def close(self) -> None:
        self.db.close()
