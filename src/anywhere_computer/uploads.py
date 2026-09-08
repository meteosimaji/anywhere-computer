"""Durable bounded uploads with transactional chunks and explicit publication intent."""

import base64
import binascii
import hashlib
import os
import secrets
import sqlite3
import stat
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import cast

from pydantic import JsonValue

from .files import absolute_path, sha256
from .locking import ProcessLock
from .models import BeginUpload, ResolveUpload, TransferId, UploadChunk
from .state import prepare_directory

UPLOAD_TOOLS = frozenset(
    {
        "upload_begin",
        "upload_chunk",
        "upload_status",
        "upload_commit",
        "upload_abort",
        "upload_resolve",
    }
)


class UploadOutcomeUnknown(RuntimeError):
    """Publication intent was durable but its external outcome could not be confirmed."""


class Uploads:
    def __init__(self, directory: Path, *, file_locks: Path) -> None:
        self.directory = directory.resolve() / "uploads"
        prepare_directory(self.directory)
        self.file_locks = file_locks
        self.database = self.directory / "uploads.sqlite3"
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            with db:
                db.execute("BEGIN IMMEDIATE")
                version = db.execute("PRAGMA user_version").fetchone()[0]
                if version not in {0, 1, 2}:
                    raise ValueError("Unsupported upload registry version")
                db.execute(
                    "CREATE TABLE IF NOT EXISTS uploads(id TEXT PRIMARY KEY,path TEXT NOT NULL,"
                    "total INTEGER NOT NULL,digest TEXT NOT NULL,"
                    "received INTEGER NOT NULL DEFAULT 0,"
                    "state TEXT NOT NULL DEFAULT 'receiving',temporary TEXT)"
                )
                db.execute(
                    "CREATE TABLE IF NOT EXISTS chunks(id TEXT NOT NULL REFERENCES uploads(id),"
                    "offset INTEGER NOT NULL,data BLOB NOT NULL,PRIMARY KEY(id,offset))"
                )
                if version < 2 and "temporary" not in {
                    row[1] for row in db.execute("PRAGMA table_info(uploads)")
                }:
                    db.execute("ALTER TABLE uploads ADD COLUMN temporary TEXT")
                db.execute("PRAGMA user_version=2")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.database, timeout=10)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            yield db
        finally:
            db.close()

    def _lock(self, identity: str) -> ProcessLock:
        # Validate here as well as in the public model before using an ID as a path.
        TransferId(transfer_id=identity)
        return ProcessLock(self.directory / (identity + ".lock"), timeout=5)

    @staticmethod
    def _row(db: sqlite3.Connection, identity: str) -> sqlite3.Row:
        row = db.execute("SELECT * FROM uploads WHERE id=?", (identity,)).fetchone()
        if row is None:
            raise ValueError("Upload ID is unknown")
        return cast(sqlite3.Row, row)

    @staticmethod
    def _describe(row: sqlite3.Row) -> dict[str, JsonValue]:
        # IDs are supplied by the caller. Do not expose transport-internal IDs in
        # results, including results later recovered through operations_get.
        return {
            "path": str(row["path"]),
            "total_bytes": int(row["total"]),
            "received_bytes": int(row["received"]),
            "sha256": str(row["digest"]),
            "state": "unknown" if row["state"] == "publishing" else str(row["state"]),
            "publication_verified": row["state"] == "complete",
            "staging_path": str(row["temporary"]) if row["temporary"] is not None else None,
            "staging_exists": bool(row["temporary"] and os.path.lexists(row["temporary"])),
        }

    def begin(self, args: BeginUpload) -> dict[str, JsonValue]:
        path = absolute_path(args.path)
        if path.is_symlink():
            raise ValueError("Upload destination must not be a symbolic link")
        target = str(path.resolve())
        with self._lock(args.transfer_id), self._connect() as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM uploads WHERE id=?", (args.transfer_id,)).fetchone()
            if row is not None:
                if (row["path"], row["total"], row["digest"]) != (
                    target,
                    args.total_bytes,
                    args.sha256,
                ):
                    raise ValueError(
                        "Upload ID already belongs to different content or destination"
                    )
                return self._describe(row)
            if path.exists() or not path.parent.is_dir():
                raise ValueError("Use an unused destination in an existing directory")
            for active in db.execute(
                "SELECT path FROM uploads WHERE state IN ('receiving','publishing')"
            ):
                if (
                    unicodedata.normalize("NFC", active[0]).casefold()
                    == unicodedata.normalize("NFC", target).casefold()
                ):
                    raise ValueError("An active upload already reserves this destination")
            count, reserved = db.execute(
                "SELECT count(*),coalesce(sum(total),0) FROM uploads "
                "WHERE state IN ('receiving','publishing')"
            ).fetchone()
            if count >= 8 or reserved + args.total_bytes > 4 * 1024**3:
                raise ValueError(
                    "Upload staging capacity reached; finish or abort existing uploads"
                )
            db.execute(
                "INSERT INTO uploads(id,path,total,digest) VALUES(?,?,?,?)",
                (args.transfer_id, target, args.total_bytes, args.sha256),
            )
            return self._describe(self._row(db, args.transfer_id))

    def status(self, args: TransferId) -> dict[str, JsonValue]:
        with self._connect() as db:
            return self._describe(self._row(db, args.transfer_id))

    def chunk(self, args: UploadChunk) -> dict[str, JsonValue]:
        try:
            data = base64.b64decode(args.data_base64, validate=True)
        except (ValueError, binascii.Error):
            raise ValueError("Upload chunk must contain canonical base64") from None
        if (
            not data
            or len(data) > 262144
            or base64.b64encode(data).decode("ascii") != args.data_base64
        ):
            raise ValueError("Upload chunk must contain 1–262144 bytes in canonical base64")
        with self._lock(args.transfer_id), self._connect() as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = self._row(db, args.transfer_id)
            if row["state"] != "receiving":
                raise ValueError("Upload no longer accepts chunks; inspect its status")
            prior = db.execute(
                "SELECT data FROM chunks WHERE id=? AND offset=?", (args.transfer_id, args.offset)
            ).fetchone()
            if prior is not None:
                if prior[0] != data:
                    raise ValueError("This chunk offset already contains different data")
                return self._describe(row)
            if args.offset != row["received"] or args.offset + len(data) > row["total"]:
                raise ValueError("Upload offset or chunk length differs from the expected prefix")
            db.execute("INSERT INTO chunks VALUES(?,?,?)", (args.transfer_id, args.offset, data))
            db.execute(
                "UPDATE uploads SET received=received+? WHERE id=?", (len(data), args.transfer_id)
            )
            return self._describe(self._row(db, args.transfer_id))

    def abort(self, args: TransferId) -> dict[str, JsonValue]:
        with self._lock(args.transfer_id), self._connect() as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = self._row(db, args.transfer_id)
            if row["state"] not in {"receiving", "aborted"}:
                raise ValueError("Published or uncertain uploads cannot be aborted automatically")
            db.execute("DELETE FROM chunks WHERE id=?", (args.transfer_id,))
            db.execute("UPDATE uploads SET state='aborted' WHERE id=?", (args.transfer_id,))
            return self._describe(self._row(db, args.transfer_id))

    def commit(self, args: TransferId) -> dict[str, JsonValue]:
        with self._lock(args.transfer_id), self._connect() as db:
            row = self._row(db, args.transfer_id)
            if row["state"] == "complete":
                return self._describe(row)
            if row["state"] != "receiving":
                raise ValueError("Publication outcome is unavailable; inspect upload and target")
            if row["received"] != row["total"]:
                raise ValueError("Upload is incomplete")
            target = Path(row["path"])
            with ProcessLock(self.file_locks / sha256(str(target.resolve()).encode()), timeout=5):
                if target.exists() or target.is_symlink():
                    raise FileExistsError(
                        "Upload publication never overwrites an existing destination"
                    )
                if row["temporary"] and os.path.lexists(row["temporary"]):
                    raise ValueError(
                        "Previous staging file remains; inspect staging_path before retry"
                    )
                name = str(target.parent / (".anywhere-upload-" + secrets.token_hex(16)))
                with db:
                    db.execute(
                        "UPDATE uploads SET temporary=? WHERE id=?", (name, args.transfer_id)
                    )
                publishing = False
                created = False
                try:
                    digest = hashlib.sha256()
                    received = 0
                    descriptor = os.open(
                        name,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
                        0o600,
                    )
                    created = True
                    with os.fdopen(descriptor, "wb") as destination:
                        for chunk in db.execute(
                            "SELECT offset,data FROM chunks WHERE id=? ORDER BY offset",
                            (args.transfer_id,),
                        ):
                            if chunk[0] != received:
                                raise ValueError("Upload chunk sequence is corrupt")
                            data = bytes(chunk[1])
                            received += len(data)
                            if received > row["total"]:
                                raise ValueError("Upload data exceeds its declared length")
                            digest.update(data)
                            destination.write(data)
                        if received != row["total"] or digest.hexdigest() != row["digest"]:
                            raise ValueError("Upload content hash or length did not match")
                        destination.flush()
                        os.fsync(destination.fileno())
                    with db:
                        db.execute(
                            "UPDATE uploads SET state='publishing' WHERE id=?", (args.transfer_id,)
                        )
                    # The durable intent precedes the external effect. Any interruption
                    # from here leaves unknown, never an automatically repeatable publish.
                    publishing = True
                    os.link(name, target)
                    with db:
                        db.execute(
                            "UPDATE uploads SET state='complete' WHERE id=?", (args.transfer_id,)
                        )
                        db.execute("DELETE FROM chunks WHERE id=?", (args.transfer_id,))
                except Exception:
                    if publishing:
                        raise UploadOutcomeUnknown(
                            "Publication outcome is unknown; inspect upload_status and destination"
                        ) from None
                    raise
                finally:
                    if created:
                        try:
                            Path(name).unlink(missing_ok=True)
                        except OSError:
                            pass  # Status retains the recorded path for explicit cleanup.
                return self._describe(self._row(db, args.transfer_id))

    def resolve(self, args: ResolveUpload) -> dict[str, JsonValue]:
        """Confirm matching published content or discard only database staging."""
        with self._lock(args.transfer_id), self._connect() as db:
            row = self._row(db, args.transfer_id)
            if row["state"] != "publishing":
                raise ValueError("Only an uncertain publication can be resolved")
            target = Path(row["path"])
            with ProcessLock(self.file_locks / sha256(str(target.resolve()).encode()), timeout=5):
                if args.action == "confirm_published":
                    if target.is_symlink():
                        raise ValueError("Published destination must not be a symbolic link")
                    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
                    with os.fdopen(os.open(target, flags), "rb") as source:
                        metadata = os.fstat(source.fileno())
                        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != row["total"]:
                            raise ValueError("Destination does not match the declared upload")
                        digest = hashlib.sha256()
                        count = 0
                        while data := source.read(262144):
                            count += len(data)
                            if count > row["total"]:
                                raise ValueError("Destination changed during verification")
                            digest.update(data)
                        if count != row["total"] or digest.hexdigest() != row["digest"]:
                            raise ValueError("Destination hash does not match the upload")
                    state = "complete"
                else:
                    state = "discarded"
                with db:
                    db.execute("UPDATE uploads SET state=? WHERE id=?", (state, args.transfer_id))
                    db.execute("DELETE FROM chunks WHERE id=?", (args.transfer_id,))
                return self._describe(self._row(db, args.transfer_id))
