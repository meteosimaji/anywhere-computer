"""Private runner and durable checkpoints for verified Chat-to-device saves.

The journal makes no network calls. The runner records target dispatch before
forwarding and reconciles uncertain responses through target status.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import sqlite3
import stat
import tempfile
import time
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Literal, Protocol, Self

from pydantic import Field, model_validator

from .authorization import GrantIdentity
from .models import Contract
from .private_directory import create_private_directory

_active_save_lease: ContextVar[tuple[str, str] | None] = ContextVar(
    "active_save_lease", default=None)


class DeviceSave(Contract):
    source_operation_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    sandbox_link: str = Field(min_length=1, max_length=1024)
    device_id: str = Field(pattern=r"^(local|[0-9a-f]{32})$")
    destination_path: str = Field(min_length=1, max_length=4096)

    @model_validator(mode="after")
    def canonical_absolute_path(self) -> Self:
        value = self.destination_path
        posix = PurePosixPath(value)
        native = ((os.name != "nt" or self.device_id != "local")
                  and posix.is_absolute() and str(posix) == value
                  and not any(part in {".", "..", ""} for part in value.split("/")[1:]))
        windows = PureWindowsPath(value)
        windows_target = ((os.name == "nt" or self.device_id != "local")
                          and windows.is_absolute() and str(windows) == value
                          and not any(part in {".", ".."} for part in windows.parts))
        if not native and not windows_target:
            raise ValueError("Save destination must be a canonical absolute path")
        return self


SUBCHAT_SAVE_TOOLS = frozenset({"subchat_save_file"})


def _same_target_path(observed: object, requested: str, device_id: str) -> bool:
    if observed == requested:
        return True
    if device_id != "local" or not isinstance(observed, str):
        return False
    actual, intended = Path(observed), Path(requested)
    if not actual.is_absolute() or actual.name != intended.name:
        return False
    try:
        return actual.parent.resolve(strict=True) == intended.parent.resolve(strict=True)
    except (OSError, RuntimeError):
        return False


def _target_path_matches(observed: dict[str, object], requested: str,
                         device_id: str) -> bool:
    actual = observed.get("path")
    if not isinstance(actual, str) or not actual:
        return False
    target_request = observed.get("requested_path")
    if target_request is not None:
        if target_request != requested:
            return False
        # A local canonical path can be checked against the local filesystem.
        # A remote target's own status binds its requested and canonical paths.
        return device_id != "local" or _same_target_path(actual, requested, device_id)
    # Older targets did not retain their original argument. Keep exact remote
    # comparison rather than guessing whether two foreign paths alias.
    return _same_target_path(actual, requested, device_id)


SaveStage = Literal["claimed", "source_verified", "uploading", "commit_unknown",
                    "completed", "failed"]


class SaveJournal:
    """Bind one client ID to an immutable source, target, account and principal."""

    def __init__(self, database: Path) -> None:
        if database.is_symlink():
            raise ValueError("Save journal must not be a symlink")
        database.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.connection = sqlite3.connect(database, timeout=10)
        self.connection.execute("PRAGMA journal_mode=WAL")
        with self.connection:
            self.connection.execute(
                "CREATE TABLE IF NOT EXISTS subchat_device_saves ("
                "id TEXT PRIMARY KEY, principal TEXT NOT NULL, account_id TEXT NOT NULL, "
                "arguments_digest TEXT NOT NULL, route_digest TEXT NOT NULL, "
                "stage TEXT NOT NULL, transfer_id TEXT NOT NULL, "
                "target_operation_id TEXT, bytes_total INTEGER, sha256 TEXT, "
                "target_offset INTEGER, target_length INTEGER, "
                "lease_owner TEXT, lease_until REAL, source_operation_id TEXT, "
                "device_id TEXT, destination_path TEXT)"
            )
            columns = {row[1] for row in self.connection.execute(
                "PRAGMA table_info(subchat_device_saves)")}
            for name in ("target_offset", "target_length", "lease_owner", "lease_until",
                         "source_operation_id", "device_id", "destination_path"):
                if name not in columns:
                    kind = ("REAL" if name == "lease_until" else
                            "INTEGER" if name in {"target_offset", "target_length"}
                            else "TEXT")
                    self.connection.execute(
                        f"ALTER TABLE subchat_device_saves ADD COLUMN {name} {kind}")
            self.connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS subchat_device_save_source_device "
                "ON subchat_device_saves(principal,account_id,source_operation_id,device_id)")
            self.connection.execute(
                "CREATE TABLE IF NOT EXISTS subchat_device_save_steps ("
                "save_id TEXT NOT NULL, operation_id TEXT NOT NULL UNIQUE, "
                "stage TEXT NOT NULL, offset INTEGER NOT NULL, length INTEGER NOT NULL, "
                "state TEXT NOT NULL, PRIMARY KEY(save_id,operation_id))")

    def close(self) -> None:
        self.connection.close()

    @staticmethod
    def _principal(grant: GrantIdentity) -> str:
        parts = (grant.owner, grant.device, grant.client, grant.resource)
        return hashlib.sha256(json.dumps(parts, separators=(",", ":")).encode()).hexdigest()

    def claim(self, save_id: str, args: DeviceSave, *, grant: GrantIdentity,
              account_id: str, route_digest: str) -> dict[str, object]:
        if len(save_id) != 32 or any(char not in "0123456789abcdef" for char in save_id):
            raise ValueError("Invalid save ID")
        if not account_id or len(route_digest) != 64 or any(
                char not in "0123456789abcdef" for char in route_digest):
            raise ValueError("Invalid account or route identity")
        principal = self._principal(grant)
        digest = hashlib.sha256(args.model_dump_json().encode()).hexdigest()
        transfer_id = secrets.token_hex(16)
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            self.connection.execute(
                "INSERT OR IGNORE INTO subchat_device_saves "
                "(id,principal,account_id,arguments_digest,route_digest,stage,"
                "transfer_id,target_operation_id,bytes_total,sha256,"
                "source_operation_id,device_id,destination_path) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (save_id, principal, account_id, digest, route_digest,
                 "claimed", transfer_id, None, None, None,
                 args.source_operation_id, args.device_id, args.destination_path),
            )
            row = self.connection.execute(
                "SELECT principal,account_id,arguments_digest,route_digest,stage,"
                "transfer_id,target_operation_id,bytes_total,sha256,"
                "target_offset,target_length,lease_owner,lease_until,destination_path "
                "FROM subchat_device_saves WHERE id=?", (save_id,),
            ).fetchone()
        if row is None:
            raise ValueError("Source and device already belong to another save ID")
        if row[:4] != (principal, account_id, digest, route_digest):
            raise ValueError("Save ID is bound to another principal, source or device")
        return dict(zip(("principal", "account_id", "arguments_digest", "route_digest",
                         "stage", "transfer_id", "target_operation_id", "bytes_total",
                         "sha256", "target_offset", "target_length", "lease_owner",
                         "lease_until", "destination_path"), row, strict=True))

    def inspect(self, save_id: str, args: DeviceSave, *, grant: GrantIdentity,
                account_id: str) -> dict[str, object]:
        """Read an owned save even when its selected route has changed."""
        row = self.connection.execute(
            "SELECT principal,account_id,arguments_digest,route_digest,stage,"
            "bytes_total,sha256 FROM subchat_device_saves WHERE id=?", (save_id,),
        ).fetchone()
        if row is None or row[:3] != (
                self._principal(grant), account_id,
                hashlib.sha256(args.model_dump_json().encode()).hexdigest()):
            raise ValueError("Save is not visible to this principal and account")
        return dict(zip(("principal", "account_id", "arguments_digest",
                         "route_digest", "stage", "bytes_total", "sha256"), row,
                        strict=True))

    def public_status(self, save_id: str, args: DeviceSave, *, grant: GrantIdentity,
                      account_id: str) -> dict[str, object]:
        """Return only bounded metadata after the exact source/owner check."""
        saved = self.inspect(save_id, args, grant=grant, account_id=account_id)
        return {"state": saved["stage"], "total_bytes": saved["bytes_total"],
                "sha256": saved["sha256"], "device_id": args.device_id}

    def acquire_lease(self, save_id: str, owner: str, *, seconds: float = 30) -> bool:
        """Fence concurrent runners; a crashed runner's lease expires."""
        if not owner or seconds <= 0 or seconds > 120:
            raise ValueError("Invalid save lease")
        now = time.time()
        with self.connection:
            changed = self.connection.execute(
                "UPDATE subchat_device_saves SET lease_owner=?,lease_until=? "
                "WHERE id=? AND (lease_until IS NULL OR lease_until<? OR lease_owner=?)",
                (owner, now + seconds, save_id, now, owner),
            )
        return changed.rowcount == 1

    def release_lease(self, save_id: str, owner: str) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE subchat_device_saves SET lease_owner=NULL,lease_until=NULL "
                "WHERE id=? AND lease_owner=?", (save_id, owner))

    def lease_valid(self, save_id: str, owner: str) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM subchat_device_saves WHERE id=? AND lease_owner=? "
            "AND lease_until>?", (save_id, owner, time.time()),
        ).fetchone()
        return row is not None

    def dispatched(self, save_id: str, *, stage: SaveStage,
                   target_operation_id: str, offset: int = 0,
                   length: int = 0) -> None:
        """Record target dispatch before a potentially irreversible remote call."""
        if stage not in {"uploading", "commit_unknown"}:
            raise ValueError("Only target writes have dispatch checkpoints")
        if len(target_operation_id) != 32 or any(
                char not in "0123456789abcdef" for char in target_operation_id):
            raise ValueError("Invalid target operation ID")
        if offset < 0 or length < 0 or length > 262_144:
            raise ValueError("Invalid target range")
        with self.connection:
            allowed = ("('source_verified','uploading')" if stage == "uploading"
                       else "('uploading')")
            changed = self.connection.execute(
                "UPDATE subchat_device_saves SET stage=?,target_operation_id=?,"
                "target_offset=?,target_length=? "
                f"WHERE id=? AND stage IN {allowed} "
                "AND target_operation_id IS NULL",
                (stage, target_operation_id, offset, length, save_id),
            )
            if changed.rowcount != 1:
                raise ValueError("Save has unresolved target dispatch; inspect status")
            self.connection.execute(
                "INSERT INTO subchat_device_save_steps VALUES(?,?,?,?,?,?)",
                (save_id, target_operation_id, stage, offset, length, "unknown"),
            )

    def verified_source(self, save_id: str, *, total: int, sha256: str) -> None:
        if not 0 <= total <= 16 * 1024 * 1024 or len(sha256) != 64 or any(
                char not in "0123456789abcdef" for char in sha256):
            raise ValueError("Invalid verified source")
        with self.connection:
            changed = self.connection.execute(
                "UPDATE subchat_device_saves SET stage='source_verified',"
                "bytes_total=?,sha256=? WHERE id=? AND stage='claimed'",
                (total, sha256, save_id),
            )
            if changed.rowcount != 1:
                raise ValueError("Source verification checkpoint changed")

    def reconciled(self, save_id: str, *, target_operation_id: str,
                   observed: dict[str, object]) -> None:
        """Clear an upload checkpoint only after matching target status is observed.

        A commit checkpoint stays closed unless publication has been verified.
        No caller may translate an unknown commit into another commit attempt.
        """
        with self.connection:
            row = self.connection.execute(
                "SELECT stage,bytes_total,sha256,target_offset,target_length,"
                "destination_path,device_id "
                "FROM subchat_device_saves WHERE id=? AND target_operation_id=?",
                (save_id, target_operation_id),
            ).fetchone()
            if row is None or observed.get("total_bytes") != row[1] or (
                    observed.get("sha256") != row[2]
                    or not _target_path_matches(observed, row[5], row[6])):
                raise ValueError("Target status does not match verified source")
            if row[0] == "commit_unknown":
                if (observed.get("state") != "complete"
                        or observed.get("publication_verified") is not True
                        or observed.get("received_bytes") != row[1]):
                    raise ValueError("Target publication is unverified")
                changed = self.connection.execute(
                    "UPDATE subchat_device_saves SET stage='completed' "
                    "WHERE id=? AND stage='commit_unknown' AND target_operation_id=?",
                    (save_id, target_operation_id),
                )
            else:
                if (row[0] != "uploading" or observed.get("state") != "receiving"
                        or not isinstance(observed.get("received_bytes"), int)
                        or observed["received_bytes"] < row[3] + row[4]):
                    raise ValueError("Target write is unverified")
                changed = self.connection.execute(
                    "UPDATE subchat_device_saves SET target_operation_id=NULL,"
                    "target_offset=NULL,target_length=NULL "
                    "WHERE id=? AND stage='uploading' AND target_operation_id=?",
                    (save_id, target_operation_id),
                )
            if changed.rowcount != 1:
                raise ValueError("Target checkpoint cannot be reconciled")
            self.connection.execute(
                "UPDATE subchat_device_save_steps SET state='verified' "
                "WHERE save_id=? AND operation_id=?",
                (save_id, target_operation_id),
            )


class SaveTarget(Protocol):
    """Selected route. Implementations must check current target tool grants."""

    async def status(self, device_id: str, transfer_id: str) -> dict[str, object] | None: ...
    async def operation(self, device_id: str, operation_id: str
                        ) -> dict[str, object] | None: ...
    async def begin(self, device_id: str, operation_id: str, transfer_id: str,
                    path: str, total: int, sha256: str) -> None: ...
    async def chunk(self, device_id: str, operation_id: str, transfer_id: str,
                    offset: int, content: bytes) -> None: ...
    async def commit(self, device_id: str, operation_id: str,
                     transfer_id: str) -> None: ...


class SaveRunner:
    """Advance one bounded save; uncertain writes are resolved only by target status.

    A runner does not hold a target credential. `authorize` must re-read both the
    active source grant and selected route on each call. The target adapter must
    check its current `upload_*` permissions before each operation.
    """

    def __init__(self, journal: SaveJournal, *,
                 authorize: Callable[[], Awaitable[tuple[GrantIdentity, str]]],
                 download: Callable[[DeviceSave, int, int],
                                    Awaitable[tuple[bytes, int, bool]]],
                 target: SaveTarget, account_id: str,
                 spool_directory: Path) -> None:
        self.journal = journal
        self.authorize = authorize
        self.download = download
        self.target = target
        self.account_id = account_id
        if spool_directory.is_symlink():
            raise ValueError("Save spool must not be a symlink")
        create_private_directory(spool_directory)
        if os.name != "nt" and spool_directory.stat().st_mode & 0o077:
            raise PermissionError("Save spool must be private")
        self.spool_directory = spool_directory

    def _spool_path(self, save_id: str) -> Path:
        return self.spool_directory / (save_id + ".blob")

    def _write_spool(self, save_id: str, content: bytes) -> None:
        descriptor, temporary = tempfile.mkstemp(prefix=".subchat-save-",
                                                   dir=self.spool_directory)
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self._spool_path(save_id))
        finally:
            if os.path.lexists(temporary):
                os.unlink(temporary)

    def _read_spool(self, save_id: str, record: dict[str, object]) -> bytes:
        descriptor = os.open(self._spool_path(save_id),
                             os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as source:
            metadata = os.fstat(source.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 16 * 1024 * 1024:
                raise ValueError("Save spool is invalid")
            content = source.read(16 * 1024 * 1024 + 1)
        if (len(content) != record["bytes_total"]
                or hashlib.sha256(content).hexdigest() != record["sha256"]):
            raise ValueError("Save spool does not match verified source")
        return content

    @staticmethod
    def _step_id(save_id: str, step: str, offset: int = 0) -> str:
        return hashlib.sha256(
            f"subchat-device-save:{save_id}:{step}:{offset}".encode()).hexdigest()[:32]

    async def _authorized(self, expected: tuple[GrantIdentity, str] | None = None
                          ) -> tuple[GrantIdentity, str]:
        lease = _active_save_lease.get()
        if lease is not None and not self.journal.lease_valid(*lease):
            raise PermissionError("Save lease was lost")
        grant, route = await self.authorize()
        if "subchat_save_file" not in grant.tools:
            raise PermissionError("Direct save is not granted")
        if expected is not None and (
            SaveJournal._principal(grant) != SaveJournal._principal(expected[0])
                or route != expected[1]
        ):
            raise PermissionError("Save authorization or device route changed")
        return grant, route

    async def _source(self, args: DeviceSave,
                      expected: tuple[GrantIdentity, str]) -> bytes:
        content = bytearray()
        total: int | None = None
        while total is None or len(content) < total:
            await self._authorized(expected)
            part, reported_total, eof = await self.download(args, len(content), 262_144)
            if (reported_total < 0 or reported_total > 16 * 1024 * 1024
                    or len(part) > 262_144 or len(content) + len(part) > reported_total
                    or (not part and not eof) or (eof != (len(content) + len(part)
                                                     == reported_total))
                    or (total is not None and total != reported_total)):
                raise ValueError("Verified source chunks are inconsistent")
            total = reported_total
            content.extend(part)
        return bytes(content)

    @staticmethod
    def _target_state(state: dict[str, object] | None, args: DeviceSave,
                      record: dict[str, object]) -> tuple[str, int] | None:
        if state is None:
            return None
        total = record["bytes_total"]
        if not isinstance(total, int) or isinstance(total, bool):
            raise ValueError("Verified source size is unavailable")
        if (not _target_path_matches(state, args.destination_path, args.device_id)
                or state.get("total_bytes") != record["bytes_total"]
                or state.get("sha256") != record["sha256"]):
            raise ValueError("Target upload differs from saved source or destination")
        status, received = state.get("state"), state.get("received_bytes")
        if (status not in {"receiving", "complete", "unknown"}
                or not isinstance(received, int) or isinstance(received, bool)
                or received < 0 or received > total):
            raise ValueError("Target upload status is invalid")
        return str(status), received

    async def advance(self, save_id: str, args: DeviceSave) -> dict[str, object]:
        grant, route = await self._authorized()
        try:
            existing = self.journal.inspect(save_id, args, grant=grant,
                                            account_id=self.account_id)
        except ValueError:
            existing = None
        if existing is not None and existing["route_digest"] != route:
            return {"state": "paused", "reason": "selected_device_route_changed"}
        self.journal.claim(save_id, args, grant=grant,
                           account_id=self.account_id, route_digest=route)
        lease = secrets.token_hex(16)
        if not self.journal.acquire_lease(save_id, lease):
            return {"state": "running", "reason": "another_runner_active"}
        token = _active_save_lease.set((save_id, lease))
        async def refresh() -> None:
            while True:
                await asyncio.sleep(10)
                if not self.journal.acquire_lease(save_id, lease):
                    return
        refresher = asyncio.create_task(refresh())
        try:
            return await self._advance_locked(save_id, args, (grant, route))
        finally:
            refresher.cancel()
            await asyncio.gather(refresher, return_exceptions=True)
            _active_save_lease.reset(token)
            self.journal.release_lease(save_id, lease)

    async def _advance_locked(self, save_id: str, args: DeviceSave,
                              expected: tuple[GrantIdentity, str]) -> dict[str, object]:
        grant, route = expected
        record = self.journal.claim(save_id, args, grant=grant,
                                    account_id=self.account_id, route_digest=route)
        if record["stage"] == "completed":
            return {"state": "completed", "sha256": record["sha256"],
                    "total_bytes": record["bytes_total"], "device_id": args.device_id,
                    "path": args.destination_path}
        if record["stage"] == "claimed":
            content = await self._source(args, expected)
            self._write_spool(save_id, content)
            self.journal.verified_source(save_id, total=len(content),
                                         sha256=hashlib.sha256(content).hexdigest())
            record = self.journal.claim(save_id, args, grant=grant,
                                        account_id=self.account_id, route_digest=route)
        else:
            content = self._read_spool(save_id, record)

        total = record["bytes_total"]
        if not isinstance(total, int) or isinstance(total, bool):
            raise ValueError("Verified source size is unavailable")

        if record["stage"] == "source_verified":
            step = self._step_id(save_id, "begin")
            self.journal.dispatched(save_id, stage="uploading", target_operation_id=step)
            await self._authorized(expected)
            try:
                await self.target.begin(args.device_id, step, str(record["transfer_id"]),
                                        args.destination_path, total,
                                        str(record["sha256"]))
            except Exception:
                return {"state": "unknown", "reason": "target_begin_unconfirmed"}
            return {"state": "running", "next_action": "resume_same_save_id"}

        await self._authorized(expected)
        observed = await self.target.status(args.device_id, str(record["transfer_id"]))
        state = self._target_state(observed, args, record)
        pending = record["target_operation_id"]
        if record["stage"] == "commit_unknown":
            if state is not None and state[0] == "complete":
                self.journal.reconciled(save_id, target_operation_id=str(pending),
                                        observed=observed or {})
                return {"state": "completed", "sha256": record["sha256"],
                        "total_bytes": record["bytes_total"],
                        "device_id": args.device_id, "path": args.destination_path}
            await self._authorized(expected)
            await self.target.operation(args.device_id, str(pending))
            return {"state": "unknown", "reason": "commit_outcome_unconfirmed"}
        if state is not None and state[0] in {"complete", "unknown"}:
            raise ValueError("Target publication state requires explicit resolution")
        if pending is not None:
            pending_offset = record["target_offset"]
            pending_length = record["target_length"]
            if (not isinstance(pending_offset, int)
                    or not isinstance(pending_length, int)):
                raise ValueError("Target checkpoint is invalid")
            expected_end = pending_offset + pending_length
            if state is None or state[1] < expected_end:
                # Inspect the exact routed operation ID before declaring the
                # response unknown. An operation receipt alone cannot prove
                # the target file state, so it never triggers another write.
                await self._authorized(expected)
                await self.target.operation(args.device_id, str(pending))
                return {"state": "unknown", "reason": "target_write_unconfirmed"}
            self.journal.reconciled(save_id, target_operation_id=str(pending),
                                    observed=observed or {})
        assert state is not None
        offset = state[1]
        if offset < len(content):
            part = content[offset:offset + 262_144]
            step = self._step_id(save_id, "chunk", offset)
            self.journal.dispatched(save_id, stage="uploading",
                                    target_operation_id=step, offset=offset,
                                    length=len(part))
            await self._authorized(expected)
            try:
                await self.target.chunk(args.device_id, step, str(record["transfer_id"]),
                                        offset, part)
            except Exception:
                return {"state": "unknown", "reason": "target_chunk_unconfirmed"}
            return {"state": "running", "next_action": "resume_same_save_id"}
        step = self._step_id(save_id, "commit")
        self.journal.dispatched(save_id, stage="commit_unknown",
                                target_operation_id=step)
        await self._authorized(expected)
        try:
            await self.target.commit(args.device_id, step, str(record["transfer_id"]))
        except Exception:
            return {"state": "unknown", "reason": "commit_outcome_unconfirmed"}
        return {"state": "unknown", "reason": "verify_target_publication"}
