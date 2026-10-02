"""Owner-issued, durable restrictions for an explicitly identified child task.

The caller must authenticate the child separately. A child ID supplied in tool
arguments is never an authentication credential.
"""

import hashlib
import logging
import secrets
import sqlite3
import time
from collections.abc import Awaitable, Callable, Generator, Iterator
from contextlib import closing, contextmanager
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from .authorization import AuthorizationStore, current_grant_read_only
from .grant_revocation import (
    RevocationState,
    bind_request_input,
    finish_revocation,
    flush_pending_audit,
    record_pending_audit,
    request_revocation,
    revocation_requested,
)
from .models import Reply, Request
from .remote_bridge import RemoteAgent
from .state import Ledger, prepare_directory


class DelegatedTaskGrant(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    owner: str = Field(min_length=1, max_length=128)
    child_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    parent_grant_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    device_id: str = Field(pattern=r"^(local|[a-f0-9]{32})$")
    tools: frozenset[str] = Field(min_length=1)
    read_roots: tuple[str, ...] = ()
    read_files: tuple[str, ...] = ()
    write_roots: tuple[str, ...] = ()
    expires_at: float = Field(gt=0, allow_inf_nan=False)


READ_PATH_TOOLS = frozenset({
    "files_read", "files_read_binary", "files_read_many", "files_info",
    "directories_list", "documents_read", "documents_preview",
})
WRITE_PATH_TOOLS = frozenset({
    "files_write", "files_write_binary", "files_edit", "files_restore", "files_move",
    "documents_write", "documents_edit_paragraph", "documents_edit_cell", "directories_create",
})
PATH_TOOLS = READ_PATH_TOOLS | WRITE_PATH_TOOLS
SUPPORTED_TOOLS = PATH_TOOLS | {"computer_status", "operations_get"}
logger = logging.getLogger(__name__)


def _inside(path_value: JsonValue, roots: tuple[str, ...]) -> bool:
    if not isinstance(path_value, str) or not Path(path_value).is_absolute():
        return False
    # The descriptor-confined executor rejects symlink ancestors and noncanonical
    # paths. Reject them before reserving an operation as well, including aliases
    # that resolve inside an otherwise allowed root.
    path = Path(path_value).resolve(strict=False)
    if str(path) != path_value:
        return False
    return any(path == Path(root) or Path(root) in path.parents for root in roots)


def _paths_allowed(tool: str, arguments: dict[str, JsonValue],
                   grant: DelegatedTaskGrant) -> bool:
    roots = grant.read_roots if tool in READ_PATH_TOOLS else grant.write_roots
    if tool == "files_move":
        return _inside(arguments.get("source"), grant.write_roots) and _inside(
            arguments.get("destination"), grant.write_roots)
    if tool == "files_read_many":
        paths = arguments.get("paths")
        return isinstance(paths, list) and bool(paths) and all(
            _inside(path, roots) for path in paths)
    path_value = arguments.get("path")
    if _inside(path_value, roots):
        return True
    # An individual file never authorizes its parent directory or descendants.
    return (tool == "files_read" and isinstance(path_value, str)
            and path_value in grant.read_files
            and str(Path(path_value).resolve(strict=False)) == path_value)


class DelegatedTaskStore:
    """Trusted issue/revoke boundary; audit rows contain no paths or arguments."""

    def __init__(self, directory: Path, authority: AuthorizationStore) -> None:
        prepare_directory(directory)
        self.directory = directory
        self.authority = authority
        database = directory / "delegated-tasks.sqlite3"
        if database.is_symlink():
            raise ValueError("Delegated task database must not be a symbolic link")
        self.db = sqlite3.connect(database, timeout=10)
        self.ledger = Ledger(directory / "delegated-operations")
        with self.db:
            self.db.execute("CREATE TABLE IF NOT EXISTS grants ("
                            "child_id TEXT PRIMARY KEY, body TEXT NOT NULL, "
                            "token_digest TEXT NOT NULL UNIQUE, revoked INTEGER NOT NULL)")
            self.db.execute("CREATE TABLE IF NOT EXISTS audit ("
                            "operation_id TEXT NOT NULL, child_id TEXT NOT NULL, "
                            "tool TEXT NOT NULL, decision TEXT NOT NULL, reason TEXT NOT NULL, "
                            "observed REAL NOT NULL, PRIMARY KEY(child_id,operation_id))")
            self.db.execute("CREATE TABLE IF NOT EXISTS operation_inputs ("
                            "child_id TEXT NOT NULL, operation_id TEXT NOT NULL, "
                            "request_digest TEXT NOT NULL, "
                            "PRIMARY KEY(child_id,operation_id))")
            self.db.execute("CREATE TABLE IF NOT EXISTS remote_operations ("
                            "child_id TEXT NOT NULL, operation_id TEXT NOT NULL, "
                            "device_id TEXT NOT NULL, tool TEXT NOT NULL, "
                            "target_child_id TEXT NOT NULL, request_digest TEXT NOT NULL, "
                            "PRIMARY KEY(child_id,operation_id))")
            columns = {row[1] for row in self.db.execute(
                "PRAGMA table_info(remote_operations)")}
            if "target_child_id" not in columns:
                self.db.execute(
                    "ALTER TABLE remote_operations ADD COLUMN target_child_id TEXT")
            if "request_digest" not in columns:
                self.db.execute(
                    "ALTER TABLE remote_operations ADD COLUMN request_digest TEXT")

    def bind_remote_operation(self, child_id: str, operation_id: str,
                              device_id: str, tool: str, target_child_id: str,
                              request_digest: str) -> None:
        """Persist the target identity before a remote side effect can dispatch."""
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO remote_operations "
                "(child_id,operation_id,device_id,tool,target_child_id,request_digest) "
                "VALUES (?,?,?,?,?,?)",
                (child_id, operation_id, device_id, tool, target_child_id,
                 request_digest))
            row = self.db.execute(
                "SELECT device_id,tool,target_child_id,request_digest "
                "FROM remote_operations "
                "WHERE child_id=? AND operation_id=?", (child_id, operation_id),
            ).fetchone()
            if row != (device_id, tool, target_child_id, request_digest):
                raise ValueError("Delegated operation ID belongs to another target")

    def bind_request(self, child_id: str, request: Request) -> None:
        """Keep one child operation ID bound across local and remote ledgers."""
        digest = Ledger.request_digest(request)
        # Honor bindings written by previous versions. New admission metadata
        # uses the independent store, so a revoked call can be durably denied
        # while an already admitted file worker holds both grant reservations.
        row = self.db.execute(
            "SELECT request_digest FROM operation_inputs WHERE child_id=? AND operation_id=?",
            (child_id, request.operation_id),
        ).fetchone()
        if row is not None and row[0] != digest:
            raise ValueError("Delegated operation ID was used for another request")
        bind_request_input(self.database, child_id, request.operation_id, digest)

    def remote_operation(self, child_id: str, operation_id: str
                         ) -> tuple[str, str, str, str] | None:
        row = self.db.execute(
            "SELECT device_id,tool,target_child_id,request_digest "
            "FROM remote_operations "
            "WHERE child_id=? AND operation_id=?", (child_id, operation_id),
        ).fetchone()
        return (str(row[0]), str(row[1]), str(row[2]), str(row[3])) if row is not None else None

    def issue(self, grant: DelegatedTaskGrant) -> str:
        parent = self.authority.current_grant(grant.parent_grant_id)
        if (parent is None or parent.owner != grant.owner or grant.expires_at <= time.time()
                or not grant.tools <= SUPPORTED_TOOLS
                or bool(grant.tools & PATH_TOOLS - {"files_read", "files_write"})
                or ("files_write" in grant.tools and "operations_get" not in grant.tools)
                # Remote file roots are validated by the target child executor.
                or (grant.device_id != "local"
                    and bool(grant.read_roots or grant.read_files or grant.write_roots))
                or (grant.device_id == "local" and bool(grant.tools & READ_PATH_TOOLS)
                    and not (grant.read_roots or grant.read_files))
                or (grant.device_id == "local" and bool(grant.tools & WRITE_PATH_TOOLS)
                    and not grant.write_roots)
                or not (grant.tools <= parent.tools if grant.device_id == "local"
                        else "devices_call" in parent.tools)):
            raise ValueError("Parent authorization cannot issue this delegation")
        for root in (*grant.read_roots, *grant.write_roots):
            path = Path(root)
            if not path.is_absolute() or str(path.resolve(strict=False)) != root:
                raise ValueError("Delegated roots must be canonical absolute paths")
        for value in grant.read_files:
            path = Path(value)
            if (not path.is_absolute() or not path.is_file()
                    or str(path.resolve(strict=True)) != value):
                raise ValueError("Delegated files must be existing canonical absolute files")
        token = secrets.token_urlsafe(48)
        with self.db:
            self.db.execute("INSERT INTO grants VALUES (?,?,?,0)",
                            (grant.child_id, grant.model_dump_json(),
                             hashlib.sha256(token.encode()).hexdigest()))
        return token

    def verify_token(self, token: str) -> str | None:
        if len(token) > 256:
            return None
        row = self.db.execute("SELECT child_id FROM grants WHERE token_digest=?",
                              (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
        child_id = str(row[0]) if row is not None else None
        return child_id if child_id is not None and self.current(child_id) is not None else None

    def revoke(self, child_id: str) -> RevocationState:
        row = self.db.execute('SELECT body FROM grants WHERE child_id=?', (child_id,)).fetchone()
        if row is None:
            raise ValueError('Delegated child is unavailable')
        grant = DelegatedTaskGrant.model_validate_json(row[0])
        request_revocation(self.database, child_id, grant.owner)
        state = finish_revocation(self.database, child_id, 'child')
        if state is None:
            raise ValueError('Delegated revocation acceptance could not be confirmed')
        return state

    @property
    def database(self) -> Path:
        return self.directory / 'delegated-tasks.sqlite3'

    def revocation_state(self, child_id: str) -> RevocationState | None:
        state = finish_revocation(self.database, child_id, 'child')
        if state is not None:
            return state
        row = self.db.execute('SELECT revoked FROM grants WHERE child_id=?',
                              (child_id,)).fetchone()
        return 'revoked' if row is not None and row[0] else None

    def list_grants(self, *, owner: str) -> list[tuple[DelegatedTaskGrant, bool]]:
        """Owner administration metadata; bearer digests never leave storage."""
        rows = self.db.execute("SELECT body,revoked FROM grants ORDER BY rowid").fetchall()
        result: list[tuple[DelegatedTaskGrant, bool]] = []
        for body, revoked in rows:
            grant = DelegatedTaskGrant.model_validate_json(body)
            if grant.owner == owner:
                active = not bool(revoked) and self.current(grant.child_id) is not None
                result.append((grant, active))
        return result

    def current(self, child_id: str) -> DelegatedTaskGrant | None:
        if revocation_requested(self.database, child_id):
            finish_revocation(self.database, child_id, 'child')
            return None
        row = self.db.execute("SELECT body,revoked FROM grants WHERE child_id=?",
                              (child_id,)).fetchone()
        if row is None or row[1]:
            return None
        grant = DelegatedTaskGrant.model_validate_json(row[0])
        parent = self.authority.current_grant(grant.parent_grant_id)
        if (grant.expires_at <= time.time() or parent is None or parent.owner != grant.owner
                or ("files_write" in grant.tools and "operations_get" not in grant.tools)
                or not (grant.tools <= parent.tools if grant.device_id == "local"
                        else "devices_call" in parent.tools)):
            return None
        return grant

    def check(self, child_id: str, device_id: str, request: Request,
              *, ledger: Ledger | None = None,
              recorded_request: Request | None = None) -> Reply | None:
        """Return a durable denial, or None when the exact call may proceed.

        The caller must bind child_id to authenticated transport identity and invoke
        this before every dispatch, including reconnects and worker restarts.
        """
        if ledger is not None:
            # Older versions saved denials in a second namespace. Honor those
            # claims before a previously denied ID can reach a side effect.
            legacy_id = RemoteAgent.internal_id("delegated-denial:" + child_id,
                                                request.operation_id)
            try:
                legacy = ledger.get(legacy_id)
            except ValueError:
                pass
            else:
                if ledger.digest_for(legacy_id) != Ledger.request_digest(
                        request.model_copy(update={"operation_id": legacy_id})):
                    return Reply(operation_id=request.operation_id, state="failed",
                                 data={"dispatched": False,
                                       "reason": "operation_id_conflict"},
                                 error="Delegated operation ID was used for another request")
                return legacy.model_copy(update={"operation_id": request.operation_id})
        grant = self.current(child_id)
        reason = "authorization_unavailable"
        if grant is not None and grant.device_id == device_id:
            if request.tool not in grant.tools:
                reason = "tool_out_of_scope"
            elif request.tool in PATH_TOOLS - {"files_read", "files_write"}:
                reason = "path_confinement_unavailable"
            elif device_id == "local" and request.tool in PATH_TOOLS and not _paths_allowed(
                request.tool, request.arguments, grant
            ):
                reason = "path_out_of_scope"
            else:
                return None
        elif grant is not None:
            reason = "device_out_of_scope"
        denial = Reply(operation_id=request.operation_id, state="failed",
                       data={"dispatched": False, "reason": reason},
                       error="Delegated task authorization denied before dispatch")
        if ledger is not None:
            internal_id = RemoteAgent.internal_id("delegated-child:" + child_id,
                                                  request.operation_id)
            recorded = (recorded_request or request).model_copy(
                update={"operation_id": internal_id})
            try:
                previous = ledger.claim(recorded)
            except ValueError:
                return Reply(operation_id=request.operation_id, state="failed",
                             data={"dispatched": False, "reason": "operation_id_conflict"},
                             error="Delegated operation ID was used for another request")
            if previous is not None:
                # A previously dispatched call can become unauthorized after
                # revocation. Its receipt stays in the ledger, but it cannot
                # authorize a new dispatch or replace the current denial.
                if (previous.state == "failed" and previous.data.get("dispatched") is False
                        and previous.data.get("reason") == reason):
                    return previous.model_copy(update={"operation_id": request.operation_id})
            else:
                ledger.finish(denial.model_copy(update={"operation_id": internal_id}))
        record_pending_audit(self.database, request.operation_id, child_id, request.tool,
                             reason, time.time())
        flush_pending_audit(self.database)
        return denial

    def record_target_failure(self, child_id: str, request: Request, *,
                              reason: str = "target_failed") -> None:
        """Audit a failed remote response without retaining arguments or error text."""
        if reason not in {"target_failed", "route_unavailable"}:
            raise ValueError("Invalid delegated remote failure reason")
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO audit VALUES (?,?,?,?,?,?)",
                            (request.operation_id, child_id, request.tool, "denied",
                             reason, time.time()))

    @contextmanager
    def local_file_guard(self, child_id: str, request: Request) -> Iterator[DelegatedTaskGrant]:
        """Serialize a local file operation with child and parent revocation.

        Both write reservations remain held through descriptor-confined I/O.
        A committed revocation intent denies new admission while its UPDATE is
        pending. This already admitted worker may finish; its exit reconciles
        the pending UPDATE after releasing both reservations.
        """
        if revocation_requested(self.database, child_id):
            raise ValueError('Delegated task authorization is unavailable')
        grant: DelegatedTaskGrant | None = None
        try:
            with closing(self._reserved_file_guard(child_id, request)) as reservation:
                grant = next(reservation)
                yield grant
        finally:
            # Never replace a file result/error with bookkeeping failure. A
            # persisted intent remains pending and denies new dispatch until a
            # later owner query or worker exit confirms its original UPDATE.
            try:
                finish_revocation(self.database, child_id, 'child')
                if grant is not None:
                    finish_revocation(self.authority.database, grant.parent_grant_id, 'parent')
                flush_pending_audit(self.database)
            except (sqlite3.Error, OSError, ValueError) as error:
                logger.warning('Delegated revocation remains unconfirmed: error_type=%s',
                               type(error).__name__)

    def _reserved_file_guard(self, child_id: str, request: Request
                             ) -> Generator[DelegatedTaskGrant, None, None]:
        with closing(sqlite3.connect(self.authority.database, timeout=10)) as parent_db, \
                closing(sqlite3.connect(self.directory / "delegated-tasks.sqlite3",
                                        timeout=10)) as child_db:
            parent_db.execute("BEGIN IMMEDIATE")
            try:
                child_db.execute("BEGIN IMMEDIATE")
                try:
                    row = child_db.execute(
                        "SELECT body,revoked FROM grants WHERE child_id=?", (child_id,)
                    ).fetchone()
                    if row is None or row[1] or revocation_requested(self.database, child_id):
                        raise ValueError("Delegated task authorization is unavailable")
                    grant = DelegatedTaskGrant.model_validate_json(row[0])
                    parent = current_grant_read_only(
                        self.authority.database, grant.parent_grant_id)
                    if (grant.expires_at <= time.time() or parent is None
                            or parent.owner != grant.owner or grant.device_id != "local"
                            or (request.tool == "files_write"
                                and "operations_get" not in grant.tools)
                            or request.tool not in {"files_read", "files_write"}
                            or request.tool not in grant.tools or request.tool not in parent.tools):
                        raise ValueError("Delegated task authorization is unavailable")
                    yield grant
                finally:
                    child_db.rollback()
            finally:
                parent_db.rollback()

    async def dispatch(self, child_id: str, device_id: str, request: Request,
                       execute: Callable[[Request], Awaitable[Reply]],
                       *, ledger: Ledger | None = None) -> Reply:
        denied = self.check(child_id, device_id, request, ledger=ledger)
        return denied if denied is not None else await execute(request)

    def close(self) -> None:
        self.ledger.close()
        self.db.close()
