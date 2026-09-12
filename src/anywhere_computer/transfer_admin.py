"""Local-owner transfer inventory, deliberately absent from remote MCP catalogs."""

import sqlite3
from pathlib import Path
from typing import Literal

from pydantic import JsonValue

from .downloads import Downloads
from .engine_selection import engine_directory, require_no_migration
from .http_service import load_http_config
from .models import TransferId
from .uploads import Uploads

TransferArea = Literal["local", "http"]
TransferKind = Literal["upload", "download"]


def _database(directory: Path, area: TransferArea, kind: TransferKind) -> tuple[Path, Path, str]:
    if area not in {"local", "http"} or kind not in {"upload", "download"}:
        raise ValueError("Unknown transfer area or kind")
    require_no_migration(directory)
    scope: str = area
    if area == "http":
        engine = directory / "http-server" / "engine"
        config_path = directory / "http-server/config.json"
        if config_path.exists() or config_path.is_symlink():
            shared = load_http_config(directory).shared_agent_directory
            if shared is not None:
                engine = engine_directory(Path(shared))
                scope = "shared"
    else:
        engine = engine_directory(directory)
        if engine != directory:
            scope = "shared"
    database = engine / (kind + "s") / (kind + "s.sqlite3")
    # Reject existing symlinks. State is owned by a trusted local user; this
    # is not atomic protection against that user replacing paths during access.
    for component in (engine, database.parent, database):
        if component.is_symlink():
            raise ValueError("Transfer registry must not be a symbolic link")
    if area == "http" and (directory / "http-server").is_symlink():
        raise ValueError("HTTP state must not be a symbolic link")
    return engine, database, scope


def list_transfers(
    directory: Path,
    *,
    area: TransferArea,
    kind: TransferKind,
    after: str | None = None,
    limit: int = 100,
) -> dict[str, JsonValue]:
    if after is not None:
        TransferId(transfer_id=after)
    if not 1 <= limit <= 100:
        raise ValueError("Transfer page limit must be 1–100")
    _, database, scope = _database(directory, area, kind)
    if not database.exists():
        return {
            "area": area,
            "storage_scope": scope,
            "kind": kind,
            "registry_exists": False,
            "transfers": [],
            "next_after": None,
        }
    try:
        db = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in ({1, 2} if kind == "upload" else {1}):
                raise ValueError("Unsupported transfer registry version")
            projection = "*"
            if kind == "upload" and version == 1:
                columns = {row[1] for row in db.execute("PRAGMA table_info(uploads)")}
                if "temporary" not in columns:
                    projection = "*,NULL AS temporary"
            # Table names are selected exclusively by the validated literal above.
            rows = db.execute(
                f"SELECT {projection} FROM {kind}s WHERE id>? ORDER BY id LIMIT ?",
                (after or "", limit + 1)
            ).fetchall()
            describe = Uploads._describe if kind == "upload" else Downloads._describe
            entries: list[JsonValue] = [
                {"storage_id": str(row["id"]), **describe(row)} for row in rows[:limit]
            ]
            return {
                "area": area,
                "storage_scope": scope,
                "kind": kind,
                "registry_exists": True,
                "transfers": entries,
                "next_after": str(rows[limit - 1]["id"]) if len(rows) > limit else None,
            }
        finally:
            db.close()
    except sqlite3.Error as error:
        raise ValueError("Transfer registry cannot be read") from error


def release_transfer(
    directory: Path,
    *,
    area: TransferArea,
    kind: TransferKind,
    storage_id: str,
) -> dict[str, JsonValue]:
    identity = TransferId(transfer_id=storage_id)
    engine, database, scope = _database(directory, area, kind)
    if not database.is_file():
        raise ValueError("Transfer registry does not exist")
    try:
        if kind == "download":
            result = Downloads(engine).close(identity)
        else:
            result = Uploads(engine, file_locks=directory / "file-locks").abort(identity)
        return {"area": area, "storage_scope": scope, "kind": kind,
                "storage_id": storage_id, **result}
    except sqlite3.Error as error:
        raise ValueError("Transfer registry cannot be updated") from error
