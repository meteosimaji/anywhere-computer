"""A durable operation ledger; a missing response never implies a safe retry."""

import hashlib
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

from .models import Reply, Request


def state_directory() -> Path:
    override = os.environ.get("ANYWHERE_STATE_DIR")
    if override:
        return Path(override).expanduser()
    home = Path.home()
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", str(home / "AppData/Local")))
        return base / "Anywhere Computer" / "Anywhere Computer"
    if base := os.environ.get("XDG_STATE_HOME", "").strip():
        if Path(base).is_absolute():
            return Path(base) / "Anywhere Computer"
    if sys.platform == "darwin":
        return home / "Library/Application Support/Anywhere Computer"
    return home / ".local/state/Anywhere Computer"


def prepare_directory(directory: Path) -> None:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("State directory must be a real directory")
    if sys.platform != "win32":
        if directory.stat().st_uid != os.getuid():
            raise PermissionError("State directory belongs to another user")
        directory.chmod(0o700)


class Ledger:
    def __init__(self, directory: Path) -> None:
        prepare_directory(directory)
        self.connection = sqlite3.connect(directory / "operations.sqlite3")
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS operations ("
            "id TEXT PRIMARY KEY, tool TEXT NOT NULL, digest TEXT NOT NULL, "
            "started REAL NOT NULL, reply TEXT NOT NULL)"
        )
        with self.connection:
            self.connection.execute(
                "CREATE TABLE IF NOT EXISTS operation_results "
                "(sha256 TEXT PRIMARY KEY, body TEXT NOT NULL)"
            )
            columns = {row[1] for row in self.connection.execute("PRAGMA table_info(operations)")}
            if "state" not in columns:
                self.connection.execute("ALTER TABLE operations ADD COLUMN state TEXT")
                self.connection.execute("ALTER TABLE operations ADD COLUMN result_sha256 TEXT")
                # One transactional migration; later startups query only the state index.
                rows = self.connection.execute("SELECT reply FROM operations")
                while batch := rows.fetchmany(100):
                    for (payload,) in batch:
                        self._store(Reply.model_validate_json(payload))
            self.connection.execute(
                "CREATE INDEX IF NOT EXISTS operations_state ON operations(state)"
            )
            self.connection.execute(
                "CREATE INDEX IF NOT EXISTS operations_started ON operations(started)"
            )
            unfinished = self.connection.execute(
                "SELECT id FROM operations WHERE state IN ('running', 'awaiting_approval')"
            ).fetchall()
            for (operation_id,) in unfinished:
                self._store(Reply(operation_id=operation_id, state="unknown",
                    error="Agent restarted before the outcome was recorded"))

    def claim(self, request: Request) -> Reply | None:
        serialized = json.dumps(
            {"tool": request.tool, "arguments": request.arguments},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        digest = hashlib.sha256(serialized.encode()).hexdigest()
        existing = self.connection.execute(
            "SELECT digest FROM operations WHERE id=?", (request.operation_id,)
        ).fetchone()
        if existing:
            if existing[0] != digest:
                raise ValueError("Operation ID was already used for different arguments")
            return self.get(request.operation_id)
        reply = Reply(operation_id=request.operation_id, state="running")
        with self.connection:
            self.connection.execute(
                "INSERT INTO operations (id,tool,digest,started,reply,state) VALUES (?,?,?,?,?,?)",
                (
                    request.operation_id,
                    request.tool,
                    digest,
                    time.time(),
                    reply.model_dump_json(),
                    reply.state,
                ),
            )
        return None

    def _store(self, reply: Reply) -> None:
        body = json.dumps(reply.data, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False)
        key = hashlib.sha256(body.encode()).hexdigest()
        self.connection.execute(
            "INSERT OR IGNORE INTO operation_results VALUES (?,?)", (key, body),
        )
        metadata = reply.model_copy(update={"data": {}}).model_dump_json()
        self.connection.execute(
            "UPDATE operations SET reply=?,state=?,result_sha256=? WHERE id=?",
            (metadata, reply.state, key, reply.operation_id),
        )

    def finish(self, reply: Reply) -> None:
        with self.connection:
            self._store(reply)

    def get(self, operation_id: str) -> Reply:
        row = self.connection.execute(
            "SELECT o.reply,o.result_sha256,r.body FROM operations o "
            "LEFT JOIN operation_results r ON r.sha256=o.result_sha256 WHERE o.id=?",
            (operation_id,),
        ).fetchone()
        if row is None:
            raise ValueError("Unknown operation ID")
        reply = Reply.model_validate_json(row[0])
        if row[1] is None:
            return reply
        if row[2] is None:
            return reply.model_copy(update={"data": {
                "result_expired": True, "result_sha256": row[1],
                "next_action": "The operation ID remains reserved; do not repeat its side effects",
            }})
        if hashlib.sha256(row[2].encode()).hexdigest() != row[1]:
            raise ValueError("Operation result failed integrity validation")
        return reply.model_copy(update={"data": json.loads(row[2])})

    def expire_results(self, before: float) -> int:
        """Explicit retention: remove bodies only when no newer or active record needs them.

        Operation identity/digest metadata is retained indefinitely for duplicate protection.
        No automatic expiry runs at startup.
        """
        with self.connection:
            cursor = self.connection.execute(
                "DELETE FROM operation_results WHERE sha256 NOT IN "
                "(SELECT result_sha256 FROM operations WHERE result_sha256 IS NOT NULL "
                "AND (started>=? OR state IN ('running','awaiting_approval')))", (before,),
            )
        return cursor.rowcount

    def tool_for(self, operation_id: str) -> str | None:
        row = self.connection.execute(
            "SELECT tool FROM operations WHERE id=?", (operation_id,)
        ).fetchone()
        return str(row[0]) if row is not None else None

    def recent(
        self, limit: int, *, tool_name: str | None = None, since: float | None = None,
    ) -> list[dict[str, str | float]]:
        # Deliberately omit arguments and content from the diagnostic history.
        return [
            {
                "operation_id": row[0],
                "tool": row[1],
                "started": row[2],
                "state": row[3],
            }
            for row in self.connection.execute(
                "SELECT id, tool, started, state FROM operations "
                "WHERE (? IS NULL OR tool=?) AND (? IS NULL OR started>=?) "
                "ORDER BY started DESC, id DESC LIMIT ?",
                (tool_name, tool_name, since, since, limit),
            )
        ]

    def close(self) -> None:
        self.connection.close()

    def usage(self) -> list[dict[str, str | int]]:
        return [
            {"tool": tool, "state": state, "count": count}
            for tool, state, count in self.connection.execute(
                "SELECT tool,state,count(*) FROM operations GROUP BY tool,state ORDER BY tool,2"
            )
        ]
