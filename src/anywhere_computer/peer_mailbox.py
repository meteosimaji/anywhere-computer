"""Local, owner-provisioned peer inbox. This module does not start model turns.

``enroll`` is a trusted owner-administration operation. Runtime-facing methods
authenticate a bearer secret; caller-supplied names never establish identity.
Do not expose enrollment through an untrusted tool or HTTP route.
"""

import hashlib
import re
import secrets
import sqlite3
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .state import prepare_directory

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
MAX_TEXT_BYTES = 16_384
MAX_INBOX = 100
MAX_PENDING_PER_PEER = 100
MAX_RETAINED_ACKED_PER_PEER = 1000


class PeerAccessError(ValueError):
    """Unknown credential, peer, or account/project boundary."""


class PeerOffline(ValueError):
    """The selected peer has no current local presence lease."""


class MailboxFull(ValueError):
    """The recipient's bounded local mailbox has reached capacity."""


@dataclass(frozen=True)
class Peer:
    peer_id: str
    owner: str
    account: str
    project: str
    runtime: str


@dataclass(frozen=True)
class PeerMessage:
    delivery_id: str
    sender: str
    recipient: str
    text: str
    sent_at: float
    acknowledged_at: float | None


def _identifier(value: str) -> str:
    if not _ID.fullmatch(value):
        raise ValueError("Invalid peer identifier or binding")
    return value


class PeerMailbox:
    """SQLite mailbox for a trusted local controller and its enrolled runtimes.

    A connection is held by one instance; separate instances may use the same
    database. Tokens are returned once at enrollment and only hashes are saved.
    """

    def __init__(self, directory: Path, *, clock: Callable[[], float] = time.time) -> None:
        prepare_directory(directory)
        self._clock = clock
        self._presence_id = uuid.uuid4().hex
        self.connection = sqlite3.connect(directory / "peer_mailbox.sqlite3", timeout=10)
        try:
            self.connection.execute("PRAGMA journal_mode=WAL")
            with self.connection:
                self.connection.execute(
                    "CREATE TABLE IF NOT EXISTS peers ("
                    "peer_id TEXT PRIMARY KEY, owner TEXT NOT NULL, account TEXT NOT NULL, "
                    "project TEXT NOT NULL, runtime TEXT NOT NULL, "
                    "token_hash TEXT NOT NULL UNIQUE)"
                )
                self.connection.execute(
                    "CREATE TABLE IF NOT EXISTS peer_messages ("
                    "delivery_id TEXT PRIMARY KEY, sender TEXT NOT NULL, recipient TEXT NOT NULL, "
                    "text TEXT NOT NULL, sent_at REAL NOT NULL, acknowledged_at REAL, "
                    "FOREIGN KEY(sender) REFERENCES peers(peer_id), "
                    "FOREIGN KEY(recipient) REFERENCES peers(peer_id))"
                )
                self.connection.execute(
                    "CREATE INDEX IF NOT EXISTS peer_messages_inbox "
                    "ON peer_messages(recipient, acknowledged_at, sent_at)"
                )
                self.connection.execute(
                    "CREATE TABLE IF NOT EXISTS peer_presence ("
                    "peer_id TEXT NOT NULL, instance_id TEXT NOT NULL, "
                    "online_until REAL NOT NULL, PRIMARY KEY(peer_id,instance_id), "
                    "FOREIGN KEY(peer_id) REFERENCES peers(peer_id))"
                )
            self.connection.execute("PRAGMA foreign_keys=ON")
        except BaseException:
            self.connection.close()
            raise

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "PeerMailbox":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def enroll(self, peer: Peer, *, credential: str | None = None) -> str:
        """Owner-only provisioning; never call from a peer-supplied request."""
        for value in (peer.peer_id, peer.owner, peer.account, peer.project, peer.runtime):
            _identifier(value)
        token = credential if credential is not None else secrets.token_urlsafe(32)
        if not isinstance(token, str) or not 32 <= len(token) <= 256:
            raise ValueError("Invalid peer credential")
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            existing = self.connection.execute(
                "SELECT owner,account,project,runtime,token_hash FROM peers WHERE peer_id=?",
                (peer.peer_id,),
            ).fetchone()
            if existing is not None:
                if existing == (peer.owner, peer.account, peer.project, peer.runtime, digest):
                    return token
                raise sqlite3.IntegrityError("Peer identity is already enrolled")
            self.connection.execute(
                "INSERT INTO peers(peer_id,owner,account,project,runtime,token_hash) "
                "VALUES(?,?,?,?,?,?)",
                (peer.peer_id, peer.owner, peer.account, peer.project, peer.runtime,
                 digest),
            )
        return token

    def _peer(self, token: str) -> Peer:
        if not isinstance(token, str) or len(token) > 256:
            raise PeerAccessError("Unknown peer credential")
        row = self.connection.execute(
            "SELECT peer_id,owner,account,project,runtime FROM peers WHERE token_hash=?",
            (hashlib.sha256(token.encode()).hexdigest(),),
        ).fetchone()
        if row is None:
            raise PeerAccessError("Unknown peer credential")
        return Peer(*row)

    def identity(self, token: str) -> Peer:
        """Resolve the enrolled identity without accepting a caller-supplied name."""
        return self._peer(token)

    def heartbeat(self, token: str, *, lease_seconds: int = 60) -> Peer:
        """Renew presence explicitly; a persisted identity alone is not online."""
        if type(lease_seconds) is not int or not 5 <= lease_seconds <= 300:
            raise ValueError("lease_seconds must be 5..300")
        peer = self._peer(token)
        with self.connection:
            self.connection.execute(
                "INSERT INTO peer_presence(peer_id,instance_id,online_until) VALUES(?,?,?) "
                "ON CONFLICT(peer_id,instance_id) DO UPDATE SET online_until=excluded.online_until",
                (peer.peer_id, self._presence_id, self._clock() + lease_seconds),
            )
        return peer

    def disconnect(self, token: str) -> None:
        peer = self._peer(token)
        with self.connection:
            self.connection.execute(
                "DELETE FROM peer_presence WHERE peer_id=? AND instance_id=?",
                (peer.peer_id, self._presence_id),
            )

    def send(self, token: str, *, recipient: str, delivery_id: str, text: str) -> PeerMessage:
        """Accept text once per retained delivery ID; pruned IDs may be reused."""
        sender = self._peer(token)
        _identifier(recipient)
        _identifier(delivery_id)
        if not isinstance(text, str) or not text:
            raise ValueError("Message text must be 1..16384 UTF-8 bytes")
        try:
            size = len(text.encode("utf-8"))
        except UnicodeError as error:
            raise ValueError("Message text must be valid UTF-8") from error
        if size > MAX_TEXT_BYTES:
            raise ValueError("Message text must be 1..16384 UTF-8 bytes")
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            existing = self._message(delivery_id)
            if existing is not None:
                if (existing.sender, existing.recipient, existing.text) != (
                    sender.peer_id, recipient, text
                ):
                    raise ValueError("Delivery ID was already used for different arguments")
                return existing
            row = self.connection.execute(
                "SELECT owner,account,project FROM peers WHERE peer_id=?",
                (recipient,),
            ).fetchone()
            if row is None or row != (sender.owner, sender.account, sender.project):
                raise PeerAccessError("Recipient is unavailable in this account and project")
            online = self.connection.execute(
                "SELECT 1 FROM peer_presence WHERE peer_id=? AND online_until>? LIMIT 1",
                (recipient, self._clock()),
            ).fetchone()
            if online is None:
                raise PeerOffline("Recipient is offline; no message was accepted")
            pending = self.connection.execute(
                "SELECT count(*) FROM peer_messages "
                "WHERE recipient=? AND acknowledged_at IS NULL", (recipient,),
            ).fetchone()[0]
            if pending >= MAX_PENDING_PER_PEER:
                raise MailboxFull("Recipient mailbox is full; no message was accepted")
            sent_at = self._clock()
            self.connection.execute(
                "INSERT INTO peer_messages(delivery_id,sender,recipient,text,sent_at) "
                "VALUES(?,?,?,?,?)",
                (delivery_id, sender.peer_id, recipient, text, sent_at),
            )
            return PeerMessage(delivery_id, sender.peer_id, recipient, text, sent_at, None)

    def _message(self, delivery_id: str) -> PeerMessage | None:
        row = self.connection.execute(
            "SELECT delivery_id,sender,recipient,text,sent_at,acknowledged_at "
            "FROM peer_messages WHERE delivery_id=?", (delivery_id,),
        ).fetchone()
        return PeerMessage(*row) if row is not None else None

    def inbox(self, token: str, *, limit: int = 20) -> list[PeerMessage]:
        peer = self._peer(token)
        if type(limit) is not int or not 1 <= limit <= MAX_INBOX:
            raise ValueError("limit must be 1..100")
        rows = self.connection.execute(
            "SELECT delivery_id,sender,recipient,text,sent_at,acknowledged_at "
            "FROM peer_messages WHERE recipient=? AND acknowledged_at IS NULL "
            "ORDER BY sent_at,delivery_id LIMIT ?", (peer.peer_id, limit),
        ).fetchall()
        return [PeerMessage(*row) for row in rows]

    def acknowledge(self, token: str, delivery_id: str) -> PeerMessage:
        """Record receipt and bound acknowledged history for the addressed peer."""
        peer = self._peer(token)
        _identifier(delivery_id)
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            message = self._message(delivery_id)
            if message is None or message.recipient != peer.peer_id:
                raise PeerAccessError("Message is unavailable to this peer")
            if message.acknowledged_at is None:
                self.connection.execute(
                    "UPDATE peer_messages SET acknowledged_at=? WHERE delivery_id=?",
                    (self._clock(), delivery_id),
                )
            result = self._message(delivery_id)
            assert result is not None
            # Keep all unread messages and only the most recently acknowledged
            # records. A pruned delivery ID loses its duplicate guard; callers
            # must not reuse IDs after the retention window.
            self.connection.execute(
                "DELETE FROM peer_messages WHERE recipient=? AND acknowledged_at IS NOT NULL "
                "AND delivery_id NOT IN (SELECT delivery_id FROM peer_messages "
                "WHERE recipient=? AND acknowledged_at IS NOT NULL "
                "ORDER BY acknowledged_at DESC,delivery_id DESC LIMIT ?)",
                (peer.peer_id, peer.peer_id, MAX_RETAINED_ACKED_PER_PEER),
            )
            return result

    def history(self, token: str, *, limit: int = 20) -> list[PeerMessage]:
        peer = self._peer(token)
        if type(limit) is not int or not 1 <= limit <= MAX_INBOX:
            raise ValueError("limit must be 1..100")
        rows = self.connection.execute(
            "SELECT delivery_id,sender,recipient,text,sent_at,acknowledged_at "
            "FROM peer_messages WHERE sender=? OR recipient=? "
            "ORDER BY sent_at DESC,delivery_id DESC LIMIT ?",
            (peer.peer_id, peer.peer_id, limit),
        ).fetchall()
        return [PeerMessage(*row) for row in rows]
