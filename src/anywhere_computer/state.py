"""A durable operation ledger; a missing response never implies a safe retry."""

import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path

from platformdirs import user_state_path

from .models import Reply, Request


def state_directory() -> Path:
    override = os.environ.get("ANYWHERE_STATE_DIR")
    return Path(override).expanduser() if override else user_state_path("Anywhere Computer")


def prepare_directory(directory: Path) -> None:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("State directory must be a real directory")
    if os.name == "posix":
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
        # A process restart cannot tell whether an interrupted external effect happened.
        for operation_id, payload in self.connection.execute("SELECT id, reply FROM operations"):
            reply = Reply.model_validate_json(payload)
            if reply.state == "running":
                self.finish(
                    Reply(
                        operation_id=operation_id,
                        state="unknown",
                        error="Agent restarted before the outcome was recorded",
                    )
                )

    def claim(self, request: Request) -> Reply | None:
        serialized = json.dumps(
            {"tool": request.tool, "arguments": request.arguments},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        digest = hashlib.sha256(serialized.encode()).hexdigest()
        existing = self.connection.execute(
            "SELECT digest, reply FROM operations WHERE id=?", (request.operation_id,)
        ).fetchone()
        if existing:
            if existing[0] != digest:
                raise ValueError("Operation ID was already used for different arguments")
            return Reply.model_validate_json(existing[1])
        reply = Reply(operation_id=request.operation_id, state="running")
        with self.connection:
            self.connection.execute(
                "INSERT INTO operations VALUES (?,?,?,?,?)",
                (
                    request.operation_id,
                    request.tool,
                    digest,
                    time.time(),
                    reply.model_dump_json(),
                ),
            )
        return None

    def finish(self, reply: Reply) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE operations SET reply=? WHERE id=?",
                (
                    reply.model_dump_json(),
                    reply.operation_id,
                ),
            )

    def get(self, operation_id: str) -> Reply:
        row = self.connection.execute(
            "SELECT reply FROM operations WHERE id=?", (operation_id,)
        ).fetchone()
        if row is None:
            raise ValueError("Unknown operation ID")
        return Reply.model_validate_json(row[0])

    def recent(self, limit: int) -> list[dict[str, str | float]]:
        # Deliberately omit arguments and content from the diagnostic history.
        return [
            {
                "operation_id": row[0],
                "tool": row[1],
                "started": row[2],
                "state": Reply.model_validate_json(row[3]).state,
            }
            for row in self.connection.execute(
                "SELECT id, tool, started, reply FROM operations ORDER BY started DESC LIMIT ?",
                (limit,),
            )
        ]

    def close(self) -> None:
        self.connection.close()
