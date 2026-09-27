"""Local, owner-provisioned peer inbox. This module does not start model turns.

``enroll`` is a trusted owner-administration operation. Runtime-facing methods
authenticate a bearer secret; caller-supplied names never establish identity.
Do not expose enrollment through an untrusted tool or HTTP route.
"""

import base64
import binascii
import hashlib
import json
import math
import re
import secrets
import sqlite3
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import psutil

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


@dataclass(frozen=True)
class PeerHistoryPage:
    messages: list[PeerMessage]
    next_cursor: str | None
    has_more: bool


@dataclass(frozen=True)
class PeerDiagnostic:
    peer_id: str
    presence: str
    process: str
    session: str
    thread: str
    model_turn: str = "UNKNOWN"


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
        process = psutil.Process()
        self._process_id = process.pid
        self._process_started = process.create_time()
        self._session_id: str | None = None
        self._thread_id: str | None = None
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
                    "delivery_id TEXT NOT NULL, sender TEXT NOT NULL, recipient TEXT NOT NULL, "
                    "text TEXT NOT NULL, sent_at REAL NOT NULL, acknowledged_at REAL, "
                    "expected_session_id TEXT, expected_thread_id TEXT, "
                    "PRIMARY KEY(recipient,delivery_id), "
                    "FOREIGN KEY(sender) REFERENCES peers(peer_id), "
                    "FOREIGN KEY(recipient) REFERENCES peers(peer_id))"
                )
                message_columns = {row[1] for row in self.connection.execute(
                    "PRAGMA table_info(peer_messages)")}
                for name in ("expected_session_id", "expected_thread_id"):
                    if name not in message_columns:
                        self.connection.execute(
                            f"ALTER TABLE peer_messages ADD COLUMN {name} TEXT"
                        )
                message_keys = {row[1]: row[5] for row in self.connection.execute(
                    "PRAGMA table_info(peer_messages)")}
                if message_keys.get("recipient") != 1 or message_keys.get("delivery_id") != 2:
                    self.connection.execute(
                        "CREATE TABLE peer_messages_scoped ("
                        "delivery_id TEXT NOT NULL, sender TEXT NOT NULL, "
                        "recipient TEXT NOT NULL, text TEXT NOT NULL, sent_at REAL NOT NULL, "
                        "acknowledged_at REAL, expected_session_id TEXT, expected_thread_id TEXT, "
                        "PRIMARY KEY(recipient,delivery_id), "
                        "FOREIGN KEY(sender) REFERENCES peers(peer_id), "
                        "FOREIGN KEY(recipient) REFERENCES peers(peer_id))"
                    )
                    self.connection.execute(
                        "INSERT INTO peer_messages_scoped SELECT delivery_id,sender,recipient,"
                        "text,sent_at,acknowledged_at,expected_session_id,expected_thread_id "
                        "FROM peer_messages"
                    )
                    self.connection.execute("DROP TABLE peer_messages")
                    self.connection.execute(
                        "ALTER TABLE peer_messages_scoped RENAME TO peer_messages"
                    )
                tombstone_keys = None
                if self.connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                                           "AND name='peer_delivery_tombstones'").fetchone():
                    tombstone_keys = {row[1] for row in self.connection.execute(
                        "PRAGMA table_info(peer_delivery_tombstones)")}
                if tombstone_keys == {"delivery_id"}:
                    self.connection.execute(
                        "ALTER TABLE peer_delivery_tombstones "
                        "RENAME TO peer_delivery_tombstones_legacy"
                    )
                self.connection.execute(
                    "CREATE TABLE IF NOT EXISTS peer_delivery_tombstones ("
                    "recipient TEXT NOT NULL, delivery_id TEXT NOT NULL, "
                    "PRIMARY KEY(recipient,delivery_id))"
                )
                self.connection.execute(
                    "CREATE TABLE IF NOT EXISTS peer_delivery_tombstones_legacy ("
                    "delivery_id TEXT PRIMARY KEY)"
                )
                self.connection.execute(
                    "CREATE INDEX IF NOT EXISTS peer_messages_inbox "
                    "ON peer_messages(recipient, acknowledged_at, sent_at)"
                )
                self.connection.execute(
                    "CREATE TABLE IF NOT EXISTS peer_presence ("
                    "peer_id TEXT NOT NULL, instance_id TEXT NOT NULL, "
                    "online_until REAL NOT NULL, pid INTEGER, process_started REAL, "
                    "session_id TEXT, thread_id TEXT, PRIMARY KEY(peer_id,instance_id), "
                    "FOREIGN KEY(peer_id) REFERENCES peers(peer_id))"
                )
                self.connection.execute(
                    "CREATE TABLE IF NOT EXISTS peer_presentations ("
                    "recipient TEXT NOT NULL, delivery_id TEXT NOT NULL, "
                    "instance_id TEXT NOT NULL, session_id TEXT, thread_id TEXT, "
                    "presented_at REAL NOT NULL, PRIMARY KEY(recipient,delivery_id))"
                )
                columns = {row[1] for row in self.connection.execute(
                    "PRAGMA table_info(peer_presence)")}
                for name, kind in (("pid", "INTEGER"), ("process_started", "REAL"),
                                   ("session_id", "TEXT"), ("thread_id", "TEXT")):
                    if name not in columns:
                        self.connection.execute(
                            f"ALTER TABLE peer_presence ADD COLUMN {name} {kind}"
                        )
            self.connection.execute("PRAGMA foreign_keys=ON")
        except BaseException:
            self.connection.close()
            raise

    def close(self) -> None:
        with self.connection:
            self.connection.execute(
                "DELETE FROM peer_presence WHERE instance_id=?", (self._presence_id,)
            )
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

    def bind(self, *, session_id: str | None = None, thread_id: str | None = None) -> None:
        """Record transport-supplied labels; they are claims, not model-turn proof."""
        self._session_id = _identifier(session_id) if session_id is not None else None
        self._thread_id = _identifier(thread_id) if thread_id is not None else None

    def heartbeat(self, token: str, *, lease_seconds: int = 60) -> Peer:
        """Renew presence explicitly; a persisted identity alone is not online."""
        if type(lease_seconds) is not int or not 5 <= lease_seconds <= 300:
            raise ValueError("lease_seconds must be 5..300")
        peer = self._peer(token)
        with self.connection:
            self.connection.execute(
                "INSERT INTO peer_presence(peer_id,instance_id,online_until,pid,process_started,"
                "session_id,thread_id) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(peer_id,instance_id) DO UPDATE SET "
                "online_until=excluded.online_until,pid=excluded.pid,"
                "process_started=excluded.process_started,session_id=excluded.session_id,"
                "thread_id=excluded.thread_id",
                (peer.peer_id, self._presence_id, self._clock() + lease_seconds,
                 self._process_id, self._process_started, self._session_id, self._thread_id),
            )
        return peer

    @staticmethod
    def _live_process(pid: object, started: object) -> bool | None:
        if not isinstance(pid, int) or not isinstance(started, float):
            return None
        try:
            return psutil.Process(pid).create_time() == started
        except psutil.NoSuchProcess:
            return False
        except (psutil.AccessDenied, ValueError, OSError):
            return None

    def diagnose(self, token: str, *, recipient: str,
                 expected_session_id: str | None = None,
                 expected_thread_id: str | None = None) -> PeerDiagnostic:
        """Read-only transport evidence; no result proves a model read a message."""
        caller = self._peer(token)
        _identifier(recipient)
        if expected_session_id is not None:
            _identifier(expected_session_id)
        if expected_thread_id is not None:
            _identifier(expected_thread_id)
        row = self.connection.execute(
            "SELECT owner,account,project FROM peers WHERE peer_id=?", (recipient,)
        ).fetchone()
        if row is None or row != (caller.owner, caller.account, caller.project):
            raise PeerAccessError("Recipient is unavailable in this account and project")
        records = self.connection.execute(
            "SELECT pid,process_started,session_id,thread_id FROM peer_presence "
            "WHERE peer_id=? AND online_until>?", (recipient, self._clock()),
        ).fetchall()
        states = [(self._live_process(pid, started), session, thread)
                  for pid, started, session, thread in records]
        if any(state is None for state, _, _ in states):
            return PeerDiagnostic(recipient, "UNKNOWN", "UNKNOWN", "UNKNOWN", "UNKNOWN")
        live = [(session, thread) for state, session, thread in states if state is True]
        if not live:
            return PeerDiagnostic(recipient, "MISMATCH",
                                  "MISMATCH" if states else "UNKNOWN",
                                  "UNKNOWN", "UNKNOWN")
        if len(live) != 1:
            return PeerDiagnostic(recipient, "UNKNOWN", "UNKNOWN", "UNKNOWN", "UNKNOWN")
        session_id, thread_id = live[0]

        def compare(expected: str | None, observed: str | None) -> str:
            if expected is None or observed is None:
                return "UNKNOWN"
            return "MATCH" if expected == observed else "MISMATCH"

        return PeerDiagnostic(recipient, "MATCH", "MATCH",
                              compare(expected_session_id, session_id),
                              compare(expected_thread_id, thread_id))

    def disconnect(self, token: str) -> None:
        peer = self._peer(token)
        with self.connection:
            self.connection.execute(
                "DELETE FROM peer_presence WHERE peer_id=? AND instance_id=?",
                (peer.peer_id, self._presence_id),
            )

    def send(self, token: str, *, recipient: str, delivery_id: str, text: str,
             expected_session_id: str | None = None,
             expected_thread_id: str | None = None) -> PeerMessage:
        """Accept text once per delivery ID, including after history pruning."""
        sender = self._peer(token)
        _identifier(recipient)
        _identifier(delivery_id)
        if expected_session_id is not None:
            _identifier(expected_session_id)
        if expected_thread_id is not None:
            _identifier(expected_thread_id)
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
            row = self.connection.execute(
                "SELECT owner,account,project FROM peers WHERE peer_id=?",
                (recipient,),
            ).fetchone()
            if row is None or row != (sender.owner, sender.account, sender.project):
                raise PeerAccessError("Recipient is unavailable in this account and project")
            existing = self._message(recipient, delivery_id)
            if existing is not None:
                binding = self.connection.execute(
                    "SELECT expected_session_id,expected_thread_id FROM peer_messages "
                    "WHERE recipient=? AND delivery_id=?", (recipient, delivery_id),
                ).fetchone()
                if (existing.sender, existing.recipient, existing.text) != (
                    sender.peer_id, recipient, text
                ) or binding != (expected_session_id, expected_thread_id):
                    raise ValueError("Delivery ID was already used for different arguments")
                return existing
            if self.connection.execute(
                "SELECT 1 FROM peer_delivery_tombstones "
                "WHERE recipient=? AND delivery_id=?",
                (recipient, delivery_id),
            ).fetchone() is not None:
                raise ValueError("Delivery ID was already used and its history was pruned")
            if self.connection.execute(
                "SELECT 1 FROM peer_delivery_tombstones_legacy WHERE delivery_id=?",
                (delivery_id,),
            ).fetchone() is not None:
                raise ValueError("Delivery ID was already used before mailbox migration")
            presence = self.connection.execute(
                "SELECT pid,process_started,session_id,thread_id FROM peer_presence "
                "WHERE peer_id=? AND online_until>?",
                (recipient, self._clock()),
            ).fetchall()
            states = [(self._live_process(pid, started), session, thread)
                      for pid, started, session, thread in presence]
            live = [(session, thread) for state, session, thread in states if state is True]
            if any(state is None for state, _, _ in states):
                raise PeerOffline("Recipient presence is uncertain; no message was accepted")
            if not live:
                raise PeerOffline("Recipient is offline; no message was accepted")
            if len(live) != 1:
                raise PeerOffline("Recipient has multiple live sessions; "
                                  "no message was accepted")
            if expected_session_id is not None or expected_thread_id is not None:
                if ((expected_session_id is not None
                            and live[0][0] != expected_session_id)
                        or (expected_thread_id is not None
                            and live[0][1] != expected_thread_id)):
                    raise PeerOffline("Recipient session or thread changed; "
                                      "no message was accepted")
            pending = self.connection.execute(
                "SELECT count(*) FROM peer_messages "
                "WHERE recipient=? AND acknowledged_at IS NULL", (recipient,),
            ).fetchone()[0]
            if pending >= MAX_PENDING_PER_PEER:
                raise MailboxFull("Recipient mailbox is full; no message was accepted")
            sent_at = self._clock()
            self.connection.execute(
                "INSERT INTO peer_messages(delivery_id,sender,recipient,text,sent_at,"
                "expected_session_id,expected_thread_id) VALUES(?,?,?,?,?,?,?)",
                (delivery_id, sender.peer_id, recipient, text, sent_at,
                 expected_session_id, expected_thread_id),
            )
            return PeerMessage(delivery_id, sender.peer_id, recipient, text, sent_at, None)

    def _message(self, recipient: str, delivery_id: str) -> PeerMessage | None:
        row = self.connection.execute(
            "SELECT delivery_id,sender,recipient,text,sent_at,acknowledged_at "
            "FROM peer_messages WHERE recipient=? AND delivery_id=?",
            (recipient, delivery_id),
        ).fetchone()
        return PeerMessage(*row) if row is not None else None

    def _require_current_recipient(self, peer_id: str) -> tuple[str | None, str | None]:
        """Call inside a write transaction before reading or acknowledging text."""
        rows = self.connection.execute(
            "SELECT instance_id,pid,process_started,session_id,thread_id FROM peer_presence "
            "WHERE peer_id=? AND online_until>?", (peer_id, self._clock()),
        ).fetchall()
        states = [(instance, self._live_process(pid, started), session, thread)
                  for instance, pid, started, session, thread in rows]
        live = [(instance, session, thread) for instance, state, session, thread in states
                if state is True]
        if (any(state is None for _, state, _, _ in states)
                or len(live) != 1 or live[0][0] != self._presence_id):
            raise PeerOffline("This peer instance is not the sole live recipient")
        return live[0][1], live[0][2]

    def inbox(self, token: str, *, limit: int = 20) -> list[PeerMessage]:
        peer = self._peer(token)
        if type(limit) is not int or not 1 <= limit <= MAX_INBOX:
            raise ValueError("limit must be 1..100")
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            session_id, thread_id = self._require_current_recipient(peer.peer_id)
            rows = self.connection.execute(
                "SELECT delivery_id,sender,recipient,text,sent_at,acknowledged_at "
                "FROM peer_messages WHERE recipient=? AND acknowledged_at IS NULL "
                "AND (expected_session_id IS NULL OR expected_session_id IS ?) "
                "AND (expected_thread_id IS NULL OR expected_thread_id IS ?) "
                "ORDER BY sent_at,delivery_id LIMIT ?",
                (peer.peer_id, session_id, thread_id, limit),
            ).fetchall()
            # This records preparation of a tool result, not model consumption.
            # A replacement instance must obtain its own inbox result before ack.
            self.connection.executemany(
                "INSERT INTO peer_presentations(recipient,delivery_id,instance_id,"
                "session_id,thread_id,presented_at) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(recipient,delivery_id) DO UPDATE SET "
                "instance_id=excluded.instance_id,session_id=excluded.session_id,"
                "thread_id=excluded.thread_id,presented_at=excluded.presented_at",
                [(peer.peer_id, row[0], self._presence_id, session_id, thread_id,
                  self._clock()) for row in rows],
            )
        return [PeerMessage(*row) for row in rows]

    def acknowledge(self, token: str, delivery_id: str) -> PeerMessage:
        """Record receipt and bound acknowledged history for the addressed peer."""
        peer = self._peer(token)
        _identifier(delivery_id)
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            session_id, thread_id = self._require_current_recipient(peer.peer_id)
            message = self._message(peer.peer_id, delivery_id)
            if message is None:
                raise PeerAccessError("Message is unavailable to this peer")
            binding = self.connection.execute(
                "SELECT expected_session_id,expected_thread_id FROM peer_messages "
                "WHERE recipient=? AND delivery_id=?", (peer.peer_id, delivery_id),
            ).fetchone()
            if binding is None or any(
                expected is not None and expected != actual
                for expected, actual in zip(binding, (session_id, thread_id), strict=True)
            ):
                raise PeerAccessError("Message is unavailable to this peer session")
            if message.acknowledged_at is None:
                presented = self.connection.execute(
                    "SELECT instance_id,session_id,thread_id FROM peer_presentations "
                    "WHERE recipient=? AND delivery_id=?",
                    (peer.peer_id, delivery_id),
                ).fetchone()
                if presented != (self._presence_id, session_id, thread_id):
                    raise PeerAccessError(
                        "Message has not been presented to this peer instance"
                    )
                self.connection.execute(
                    "UPDATE peer_messages SET acknowledged_at=? "
                    "WHERE recipient=? AND delivery_id=?",
                    (self._clock(), peer.peer_id, delivery_id),
                )
                self.connection.execute(
                    "DELETE FROM peer_presentations WHERE recipient=? AND delivery_id=?",
                    (peer.peer_id, delivery_id),
                )
            result = self._message(peer.peer_id, delivery_id)
            assert result is not None
            # Retain IDs even after deleting acknowledged text so an uncertain
            # sender cannot replay an old message after history compaction.
            self.connection.execute(
                "INSERT OR IGNORE INTO peer_delivery_tombstones(recipient,delivery_id) "
                "SELECT recipient,delivery_id FROM peer_messages WHERE recipient=? "
                "AND acknowledged_at IS NOT NULL AND delivery_id NOT IN "
                "(SELECT delivery_id FROM peer_messages WHERE recipient=? "
                "AND acknowledged_at IS NOT NULL ORDER BY acknowledged_at DESC,"
                "delivery_id DESC LIMIT ?)",
                (peer.peer_id, peer.peer_id, MAX_RETAINED_ACKED_PER_PEER),
            )
            self.connection.execute(
                "DELETE FROM peer_messages WHERE recipient=? AND acknowledged_at IS NOT NULL "
                "AND delivery_id NOT IN (SELECT delivery_id FROM peer_messages "
                "WHERE recipient=? AND acknowledged_at IS NOT NULL "
                "ORDER BY acknowledged_at DESC,delivery_id DESC LIMIT ?)",
                (peer.peer_id, peer.peer_id, MAX_RETAINED_ACKED_PER_PEER),
            )
            return result

    def history(self, token: str, *, limit: int = 20) -> list[PeerMessage]:
        return self.history_page(token, limit=limit).messages

    def history_page(self, token: str, *, limit: int = 20,
                     cursor: str | None = None) -> PeerHistoryPage:
        peer = self._peer(token)
        if type(limit) is not int or not 1 <= limit <= MAX_INBOX:
            raise ValueError("limit must be 1..100")
        position: tuple[float, str, str] | None = None
        cursor_binding: tuple[str | None, str | None] | None = None
        if cursor is not None:
            try:
                if (not isinstance(cursor, str) or not cursor.startswith("ph1.")
                        or len(cursor) > 1024):
                    raise ValueError
                encoded = cursor[4:]
                payload = json.loads(base64.urlsafe_b64decode(
                    encoded + "=" * (-len(encoded) % 4)))
                if (not isinstance(payload, list) or len(payload) != 6
                        or payload[0] != peer.peer_id
                        or any(value is not None and (not isinstance(value, str)
                            or not _ID.fullmatch(value)) for value in payload[1:3])
                        or type(payload[3]) not in (float, int)
                        or not math.isfinite(payload[3])
                        or any(not isinstance(value, str) or not _ID.fullmatch(value)
                               for value in payload[4:])):
                    raise ValueError
                cursor_binding = payload[1], payload[2]
                position = float(payload[3]), payload[4], payload[5]
            except (ValueError, UnicodeError, TypeError, binascii.Error) as error:
                raise ValueError("Invalid peer history cursor") from error
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            session_id, thread_id = self._require_current_recipient(peer.peer_id)
            if cursor_binding is not None and cursor_binding != (session_id, thread_id):
                raise PeerAccessError("Peer history cursor belongs to another session")
            after = ""
            parameters: list[object] = [peer.peer_id, peer.peer_id, session_id, thread_id]
            if position is not None:
                sent_at, recipient, delivery_id = position
                after = (" AND (sent_at < ? OR (sent_at = ? AND recipient < ?) "
                         "OR (sent_at = ? AND recipient = ? AND delivery_id < ?))")
                parameters.extend((sent_at, sent_at, recipient, sent_at, recipient, delivery_id))
            parameters.append(limit + 1)
            rows = self.connection.execute(
                "SELECT delivery_id,sender,recipient,text,sent_at,acknowledged_at "
                "FROM peer_messages WHERE (sender=? OR (recipient=? "
                "AND (expected_session_id IS NULL OR expected_session_id IS ?) "
                "AND (expected_thread_id IS NULL OR expected_thread_id IS ?))) "
                + after + " ORDER BY sent_at DESC,recipient DESC,delivery_id DESC LIMIT ?",
                parameters,
            ).fetchall()
        has_more = len(rows) > limit
        messages = [PeerMessage(*row) for row in rows[:limit]]
        next_cursor = None
        if has_more:
            last = messages[-1]
            payload = [peer.peer_id, session_id, thread_id, last.sent_at,
                       last.recipient, last.delivery_id]
            next_cursor = "ph1." + base64.urlsafe_b64encode(
                json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
        return PeerHistoryPage(messages, next_cursor, has_more)
