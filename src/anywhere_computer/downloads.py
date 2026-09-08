"""Persistent download copies: hash once, then read at most two stored chunks."""

import base64
import hashlib
import os
import sqlite3
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import cast

from pydantic import JsonValue

from .files import absolute_path, sha256
from .models import BeginDownload, DownloadRange, TransferId
from .state import prepare_directory

DOWNLOAD_TOOLS = frozenset({"download_begin", "download_read", "download_status", "download_close"})
DOWNLOAD_BLOCK_BYTES = 262144
MAX_DOWNLOAD_BYTES = 1024**3


class Downloads:
    def __init__(self, directory: Path) -> None:
        self.directory = directory.resolve() / "downloads"
        prepare_directory(self.directory)
        self.database = self.directory / "downloads.sqlite3"
        with self._connect() as db, db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("BEGIN IMMEDIATE")
            if db.execute("PRAGMA user_version").fetchone()[0] not in {0, 1}:
                raise ValueError("Unsupported download registry version")
            db.execute(
                "CREATE TABLE IF NOT EXISTS downloads(id TEXT PRIMARY KEY,path TEXT NOT NULL,"
                "expected TEXT,total INTEGER NOT NULL,digest TEXT NOT NULL,state TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS chunks(id TEXT NOT NULL REFERENCES downloads(id),"
                "offset INTEGER NOT NULL,data BLOB NOT NULL,digest TEXT NOT NULL,"
                "PRIMARY KEY(id,offset))"
            )
            db.execute("PRAGMA user_version=1")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.database, timeout=10)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            yield db
        finally:
            db.close()

    @staticmethod
    def _row(db: sqlite3.Connection, identity: str) -> sqlite3.Row:
        row = db.execute("SELECT * FROM downloads WHERE id=?", (identity,)).fetchone()
        if row is None:
            raise ValueError("Download ID is unknown; no committed copy is available")
        return cast(sqlite3.Row, row)

    @staticmethod
    def _describe(row: sqlite3.Row) -> dict[str, JsonValue]:
        # Caller keeps its own ID. Never expose a transport-scoped internal ID.
        return {
            "path": str(row["path"]),
            "total_bytes": int(row["total"]),
            "sha256": str(row["digest"]),
            "state": str(row["state"]),
        }

    def begin(self, args: BeginDownload) -> dict[str, JsonValue]:
        path = absolute_path(args.path)
        # Retain the requested absolute spelling so an idempotent replay does not
        # require the original path (or a symlink target) to still exist.
        target = str(path)
        with self._connect() as db, db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute("SELECT * FROM downloads WHERE id=?", (args.transfer_id,)).fetchone()
            if prior is not None:
                if (prior["path"], prior["expected"]) != (target, args.expected_sha256):
                    raise ValueError("Download ID already belongs to different arguments")
                return self._describe(prior)
            count, reserved = db.execute(
                "SELECT count(*),coalesce(sum(total),0) FROM downloads WHERE state='ready'"
            ).fetchone()
            if count >= 8:
                raise ValueError("Download capacity reached; close existing downloads")
            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
            with os.fdopen(os.open(path, flags), "rb") as source:
                before = os.fstat(source.fileno())
                if not stat.S_ISREG(before.st_mode):
                    raise ValueError("Only regular files can be downloaded")
                if before.st_size > MAX_DOWNLOAD_BYTES or reserved + before.st_size > 4 * 1024**3:
                    raise ValueError("Download exceeds the 1 GiB file or 4 GiB staging limit")
                db.execute(
                    "INSERT INTO downloads VALUES(?,?,?,?,?,'ready')",
                    (args.transfer_id, target, args.expected_sha256, before.st_size, ""),
                )
                digest = hashlib.sha256()
                position = 0
                while data := source.read(DOWNLOAD_BLOCK_BYTES):
                    if position + len(data) > before.st_size:
                        raise ValueError("Source changed during download preparation")
                    digest.update(data)
                    db.execute(
                        "INSERT INTO chunks VALUES(?,?,?,?)",
                        (args.transfer_id, position, data, sha256(data)),
                    )
                    position += len(data)
                after, current = os.fstat(source.fileno()), path.stat()
                # Path stat and fd fstat ctime have different meanings on Windows
                # Python 3.12. Compare change time only between fd samples.
                if (
                    position != before.st_size
                    or after.st_ctime_ns != before.st_ctime_ns
                    or any(
                        (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns)
                        != (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                        for item in (after, current)
                    )
                ):
                    raise ValueError("Source changed during download preparation")
            if args.expected_sha256 is not None and digest.hexdigest() != args.expected_sha256:
                raise ValueError("Source hash differs from expected_sha256")
            db.execute(
                "UPDATE downloads SET digest=? WHERE id=?", (digest.hexdigest(), args.transfer_id)
            )
            return self._describe(self._row(db, args.transfer_id))

    def status(self, args: TransferId) -> dict[str, JsonValue]:
        with self._connect() as db:
            return self._describe(self._row(db, args.transfer_id))

    def read(self, args: DownloadRange) -> dict[str, JsonValue]:
        with self._connect() as db, db:
            # A consistent SQLite snapshot keeps close from removing half a read.
            db.execute("BEGIN")
            row = self._row(db, args.transfer_id)
            if row["state"] != "ready":
                raise ValueError("Download is closed")
            total = int(row["total"])
            if args.offset > total:
                raise ValueError("Byte offset exceeds the download size")
            stop = min(total, args.offset + args.limit)
            collected = bytearray()
            for offset in range(
                args.offset // DOWNLOAD_BLOCK_BYTES * DOWNLOAD_BLOCK_BYTES,
                stop,
                DOWNLOAD_BLOCK_BYTES,
            ):
                chunk = db.execute(
                    "SELECT data,digest FROM chunks WHERE id=? AND offset=?",
                    (args.transfer_id, offset),
                ).fetchone()
                if chunk is None:
                    raise ValueError("Download copy is incomplete")
                data = bytes(chunk[0])
                if (
                    len(data) != min(DOWNLOAD_BLOCK_BYTES, total - offset)
                    or sha256(data) != chunk[1]
                ):
                    raise ValueError("Download copy is corrupt")
                collected.extend(data[max(0, args.offset - offset) : min(len(data), stop - offset)])
            content = bytes(collected)
            if len(content) != stop - args.offset:
                raise ValueError("Download range is incomplete")
            return {
                **self._describe(row),
                "offset": args.offset,
                "next_offset": stop,
                "data_base64": base64.b64encode(content).decode("ascii"),
                "chunk_sha256": sha256(content),
                "eof": stop == total,
            }

    def close(self, args: TransferId) -> dict[str, JsonValue]:
        with self._connect() as db, db:
            db.execute("BEGIN IMMEDIATE")
            self._row(db, args.transfer_id)
            db.execute("DELETE FROM chunks WHERE id=?", (args.transfer_id,))
            db.execute("UPDATE downloads SET state='closed' WHERE id=?", (args.transfer_id,))
            return self._describe(self._row(db, args.transfer_id))
