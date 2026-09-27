"""Durable dispatch checkpoints for an explicitly requested Library upload.

This module does not dispatch an upload or attach a file to a Chat. Each
network stage must be claimed durably before its first request; an uncertain
response cannot trigger an automatic retry.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

_OPERATION = re.compile(r"[0-9a-f]{32}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_FILE_ID = re.compile(r"file_[A-Za-z0-9_-]{1,251}\Z")
_LIBRARY_ID = re.compile(r"libfile_[A-Za-z0-9_-]{1,248}\Z")
_MAX_SEARCH_BYTES = 1_048_576
_MAX_FILE_BYTES = 20_971_520


@dataclass(frozen=True, slots=True)
class LibraryUpload:
    operation_id: str
    owner: str | None
    account_id: str
    file_name: str
    file_size: int
    sha256: str
    file_id: str | None
    library_item_id: str | None
    state: Literal["unknown", "ready"]
    create_claimed: bool = False
    put_claimed: bool = False
    put_confirmed: bool = False
    process_claimed: bool = False
    process_confirmed: bool = False
    mcp_prepared_until: float | None = None
    mcp_source_path: str | None = None
    automatic_retry: Literal[False] = False


def _valid_name(value: str) -> bool:
    return (isinstance(value, str) and 0 < len(value) <= 256
            and value not in {".", ".."} and "/" not in value and "\\" not in value
            and not any(ord(char) < 32 for char in value))


def _ready_item(body: bytes, saved: LibraryUpload) -> str | None:
    """Accept one exact Library search item, never a name-only near match."""
    if len(body) > _MAX_SEARCH_BYTES:
        raise ValueError("Library search response is too large")
    try:
        document = json.loads(body)
    except (ValueError, UnicodeDecodeError) as error:
        raise ValueError("Invalid Library search response") from error
    matches: list[str] = []
    pending: list[object] = [document]
    visited = 0
    while pending:
        item = pending.pop()
        visited += 1
        if visited > 10_000:
            raise ValueError("Library search response is too complex")
        if isinstance(item, list):
            pending.extend(item)
        elif isinstance(item, dict):
            if item.get("file_id") == saved.file_id:
                if (item.get("file_name") != saved.file_name
                        or type(item.get("file_size_bytes")) is not int
                        or item["file_size_bytes"] != saved.file_size):
                    raise ValueError("Library item differs from the saved upload")
                if item.get("state") == "ready":
                    library_id = item.get("id", item.get("library_item_id"))
                    legacy_id = item.get("library_item_id")
                    if (not isinstance(library_id, str)
                            or _LIBRARY_ID.fullmatch(library_id) is None
                            or (legacy_id is not None and legacy_id != library_id)):
                        raise ValueError("Ready Library item has no identity")
                    matches.append(library_id)
            pending.extend(item.values())
    if len(matches) > 1:
        raise ValueError("Library search returned an ambiguous upload")
    return matches[0] if matches else None


class LibraryUploadLedger:
    """An owner/account-scoped checkpoint; no method grants a second dispatch."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        with connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS subchat_library_uploads ("
                "operation_id TEXT PRIMARY KEY, owner TEXT, account_id TEXT NOT NULL, "
                "file_name TEXT NOT NULL, file_size INTEGER NOT NULL, sha256 TEXT NOT NULL, "
                "file_id TEXT, library_item_id TEXT, state TEXT NOT NULL, "
                "create_claimed INTEGER NOT NULL DEFAULT 0, "
                "put_claimed INTEGER NOT NULL DEFAULT 0, "
                "put_confirmed INTEGER NOT NULL DEFAULT 0, "
                "process_claimed INTEGER NOT NULL DEFAULT 0, "
                "process_confirmed INTEGER NOT NULL DEFAULT 0, "
                "mcp_prepared_until REAL, mcp_source_path TEXT)"
            )
            columns = {row[1] for row in connection.execute(
                "PRAGMA table_info(subchat_library_uploads)")}
            if "create_claimed" not in columns:
                # An older reservation cannot prove whether its create request
                # was sent. Fail closed on migration, even without a file ID.
                connection.execute(
                    "ALTER TABLE subchat_library_uploads ADD COLUMN "
                    "create_claimed INTEGER NOT NULL DEFAULT 1"
                )
            for name, default in (("put_claimed", 1), ("put_confirmed", 0),
                                  ("process_claimed", 1), ("process_confirmed", 0)):
                if name not in columns:
                    # Older rows have no durable stage receipt. Never infer that a
                    # network step was safe to repeat after upgrading the ledger.
                    connection.execute(
                        f"ALTER TABLE subchat_library_uploads ADD COLUMN {name} "
                        f"INTEGER NOT NULL DEFAULT {default}"
                    )
            if "mcp_prepared_until" not in columns:
                # Older ordinary CLI reservations never authorized MCP dispatch.
                connection.execute(
                    "ALTER TABLE subchat_library_uploads ADD COLUMN mcp_prepared_until REAL")
            if "mcp_source_path" not in columns:
                # Earlier approvals bound bytes but not the owner's selected path.
                connection.execute(
                    "ALTER TABLE subchat_library_uploads ADD COLUMN mcp_source_path TEXT")
                connection.execute(
                    "UPDATE subchat_library_uploads SET mcp_prepared_until=NULL")

    def get(self, operation_id: str, *, owner: str | None,
            account_id: str) -> LibraryUpload:
        row = self.connection.execute(
            "SELECT operation_id,owner,account_id,file_name,file_size,sha256,"
            "file_id,library_item_id,state,create_claimed,put_claimed,put_confirmed,"
            "process_claimed,process_confirmed,mcp_prepared_until,mcp_source_path "
            "FROM subchat_library_uploads "
            "WHERE operation_id=?", (operation_id,),
        ).fetchone()
        if row is None or row[1] != owner or row[2] != account_id:
            raise ValueError("Library upload is unavailable in this owner and account")
        return LibraryUpload(
            operation_id=row[0], owner=row[1], account_id=row[2], file_name=row[3],
            file_size=row[4], sha256=row[5], file_id=row[6], library_item_id=row[7],
            state=row[8], create_claimed=bool(row[9]), put_claimed=bool(row[10]),
            put_confirmed=bool(row[11]), process_claimed=bool(row[12]),
            process_confirmed=bool(row[13]), mcp_prepared_until=row[14],
            mcp_source_path=row[15],
        )

    def authorize_mcp_upload(self, operation_id: str, *, owner: str | None,
                             account_id: str, source_path: str) -> LibraryUpload:
        """Local owner approval for one exact unstarted upload, valid 15 minutes."""
        if not source_path or not Path(source_path).is_absolute():
            raise ValueError("Library upload source path must be absolute")
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            saved = self.get(operation_id, owner=owner, account_id=account_id)
            if (saved.create_claimed or saved.put_claimed or saved.process_claimed
                    or saved.file_id is not None or saved.state == "ready"):
                raise ValueError("Library upload has already started")
            self.connection.execute(
                "UPDATE subchat_library_uploads SET mcp_prepared_until=?,mcp_source_path=? "
                "WHERE operation_id=? AND owner IS ? AND account_id=?",
                (time.time() + 900, source_path, operation_id, owner, account_id),
            )
        return self.get(operation_id, owner=owner, account_id=account_id)

    def reserve(self, operation_id: str, *, owner: str | None, account_id: str,
                file_name: str, file_size: int, sha256: str) -> LibraryUpload:
        """Commit before any provider create call; exact retries only read state."""
        if (not _OPERATION.fullmatch(operation_id)
                or not isinstance(account_id, str) or not account_id or len(account_id) > 256
                or (owner is not None and (not owner or len(owner) > 256))
                or not _valid_name(file_name)
                or type(file_size) is not int or not 0 < file_size <= _MAX_FILE_BYTES
                or not _DIGEST.fullmatch(sha256)):
            raise ValueError("Invalid Library upload reservation")
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            row = self.connection.execute(
                "SELECT operation_id,owner,account_id,file_name,file_size,sha256,"
                "file_id,library_item_id,state,create_claimed,put_claimed,put_confirmed,"
                "process_claimed,process_confirmed "
                "FROM subchat_library_uploads "
                "WHERE operation_id=?", (operation_id,),
            ).fetchone()
            if row is not None:
                saved = self.get(operation_id, owner=owner, account_id=account_id)
                if (saved.file_name, saved.file_size, saved.sha256) != (
                        file_name, file_size, sha256):
                    raise ValueError("Library upload operation ID has different input")
                return saved
            self.connection.execute(
                "INSERT INTO subchat_library_uploads "
                "(operation_id,owner,account_id,file_name,file_size,sha256,"
                "file_id,library_item_id,state,create_claimed,put_claimed,"
                "put_confirmed,process_claimed,process_confirmed) "
                "VALUES (?,?,?,?,?,?,?,?,?,0,0,0,0,0)",
                (operation_id, owner, account_id, file_name, file_size, sha256,
                 None, None, "unknown"),
            )
        return self.get(operation_id, owner=owner, account_id=account_id)

    def claim_create(self, operation_id: str, *, owner: str | None,
                     account_id: str) -> bool:
        """True only for the first create attempt; commit before network I/O."""
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            saved = self.get(operation_id, owner=owner, account_id=account_id)
            if saved.create_claimed or saved.file_id is not None:
                return False
            changed = self.connection.execute(
                "UPDATE subchat_library_uploads SET create_claimed=1 "
                "WHERE operation_id=? AND owner IS ? AND account_id=? "
                "AND create_claimed=0 AND file_id IS NULL",
                (operation_id, owner, account_id),
            )
            return changed.rowcount == 1

    def claim_ui_batch(self, operation_id: str, *, owner: str | None,
                       account_id: str, require_prepared: bool = False,
                       source_path: str | None = None) -> bool:
        """Reserve every stage before the browser owns its automatic upload sequence."""
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            saved = self.get(operation_id, owner=owner, account_id=account_id)
            if (require_prepared and (saved.mcp_prepared_until is None
                                      or saved.mcp_prepared_until <= time.time()
                                      or saved.mcp_source_path != source_path)):
                raise ValueError("Library MCP preparation is unavailable or expired")
            if (saved.create_claimed or saved.put_claimed or saved.process_claimed
                    or saved.file_id is not None or saved.state == "ready"):
                return False
            changed = self.connection.execute(
                "UPDATE subchat_library_uploads SET create_claimed=1,put_claimed=1,"
                "process_claimed=1 WHERE operation_id=? AND owner IS ? AND account_id=? "
                "AND create_claimed=0 AND put_claimed=0 AND process_claimed=0 "
                "AND file_id IS NULL AND state='unknown'",
                (operation_id, owner, account_id))
            return changed.rowcount == 1

    def checkpoint_file_id(self, operation_id: str, file_id: str, *,
                           owner: str | None, account_id: str) -> LibraryUpload:
        if not isinstance(file_id, str) or not _FILE_ID.fullmatch(file_id):
            raise ValueError("Invalid uploaded file identity")
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            saved = self.get(operation_id, owner=owner, account_id=account_id)
            if not saved.create_claimed:
                raise ValueError("Library upload create was not claimed")
            if saved.file_id not in (None, file_id):
                raise ValueError("Library upload file identity changed")
            self.connection.execute(
                "UPDATE subchat_library_uploads SET file_id=? WHERE operation_id=? "
                "AND owner IS ? AND account_id=?",
                (file_id, operation_id, owner, account_id),
            )
        return self.get(operation_id, owner=owner, account_id=account_id)

    def claim_put(self, operation_id: str, *, owner: str | None,
                  account_id: str) -> bool:
        """Reserve the one presigned byte PUT before making the network call."""
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            saved = self.get(operation_id, owner=owner, account_id=account_id)
            if saved.file_id is None:
                raise ValueError("Library upload file identity is unavailable")
            if saved.put_claimed or saved.state == "ready":
                return False
            changed = self.connection.execute(
                "UPDATE subchat_library_uploads SET put_claimed=1 "
                "WHERE operation_id=? AND owner IS ? AND account_id=? "
                "AND file_id IS NOT NULL AND put_claimed=0 AND state='unknown'",
                (operation_id, owner, account_id))
            return changed.rowcount == 1

    def confirm_put(self, operation_id: str, *, owner: str | None,
                    account_id: str) -> LibraryUpload:
        """Record a verified PUT response; a timeout is not confirmation."""
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            saved = self.get(operation_id, owner=owner, account_id=account_id)
            if not saved.put_claimed or saved.file_id is None:
                raise ValueError("Library upload PUT was not claimed")
            self.connection.execute(
                "UPDATE subchat_library_uploads SET put_confirmed=1 "
                "WHERE operation_id=? AND owner IS ? AND account_id=?",
                (operation_id, owner, account_id))
        return self.get(operation_id, owner=owner, account_id=account_id)

    def claim_process(self, operation_id: str, *, owner: str | None,
                      account_id: str) -> bool:
        """Reserve one processing request only after the byte PUT is confirmed."""
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            saved = self.get(operation_id, owner=owner, account_id=account_id)
            if saved.state == "ready":
                return False
            if not saved.put_confirmed:
                raise ValueError("Library upload PUT is unconfirmed")
            if saved.process_claimed:
                return False
            changed = self.connection.execute(
                "UPDATE subchat_library_uploads SET process_claimed=1 "
                "WHERE operation_id=? AND owner IS ? AND account_id=? "
                "AND put_confirmed=1 AND process_claimed=0 AND state='unknown'",
                (operation_id, owner, account_id))
            return changed.rowcount == 1

    def confirm_process(self, operation_id: str, *, owner: str | None,
                        account_id: str) -> LibraryUpload:
        """Record processing acceptance, distinct from a ready Library item."""
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            saved = self.get(operation_id, owner=owner, account_id=account_id)
            if not saved.process_claimed or not saved.put_confirmed:
                raise ValueError("Library processing was not claimed")
            self.connection.execute(
                "UPDATE subchat_library_uploads SET process_confirmed=1 "
                "WHERE operation_id=? AND owner IS ? AND account_id=?",
                (operation_id, owner, account_id))
        return self.get(operation_id, owner=owner, account_id=account_id)

    def reconcile_search(self, operation_id: str, body: bytes, *,
                         owner: str | None, account_id: str,
                         observed_account_id: str) -> LibraryUpload:
        """Confirm only an exact ready item in the pinned account's search result."""
        if observed_account_id != account_id:
            raise ValueError("Selected Chat account changed")
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            saved = self.get(operation_id, owner=owner, account_id=account_id)
            if saved.file_id is None:
                return saved  # Name-only lookup cannot prove an uncertain create.
            library_id = _ready_item(body, saved)
            if library_id is None:
                return saved
            if saved.library_item_id not in (None, library_id):
                raise ValueError("Library item identity changed")
            self.connection.execute(
                "UPDATE subchat_library_uploads SET library_item_id=?,state='ready' "
                "WHERE operation_id=? AND owner IS ? AND account_id=?",
                (library_id, operation_id, owner, account_id),
            )
        return self.get(operation_id, owner=owner, account_id=account_id)
