"""One typed registry drives validation, MCP discovery and execution."""

import asyncio
import platform
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar, cast

from pydantic import JsonValue

from . import __version__
from .document_writer import write_document
from .documents import read_document
from .downloads import Downloads
from .files import Files, absolute_path, inspect_file
from .models import (
    BeginDownload,
    BeginUpload,
    Contract,
    DownloadRange,
    EditFile,
    Empty,
    FilePath,
    History,
    ListDirectory,
    ListProcesses,
    MoveFile,
    OperationId,
    ReadBinary,
    ReadDocument,
    ReadFile,
    ReadFiles,
    Reply,
    Request,
    ResolveUpload,
    RestoreFile,
    RuntimeSettings,
    SearchId,
    SearchPage,
    SessionId,
    SessionInput,
    SessionOutput,
    StartSearch,
    StartSession,
    StopProcess,
    TransferId,
    UpdateSetting,
    UploadChunk,
    WriteBinary,
    WriteDocument,
    WriteFile,
)
from .processes import list_processes, stop_process
from .runtime_identity import runtime_identity
from .search import Searches
from .sessions import Sessions
from .state import Ledger
from .uploads import UploadOutcomeUnknown, Uploads

Input = TypeVar("Input", bound=Contract)
Result = dict[str, JsonValue]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    schema: type[Contract]
    handler: Callable[[Contract], Awaitable[Result]]
    read_only: bool
    destructive: bool
    open_world: bool


class Engine:
    def __init__(self, directory: Path, *, file_locks: Path | None = None) -> None:
        self.ledger = Ledger(directory)
        self.ledger.connection.execute(
            "CREATE TABLE IF NOT EXISTS runtime_settings (id INTEGER PRIMARY KEY CHECK(id=1), "
            "value TEXT NOT NULL)"
        )
        self.files = Files(directory, locks=file_locks)
        self.uploads = Uploads(directory, file_locks=self.files.locks)
        self.downloads = Downloads(directory)
        self.sessions = Sessions()
        self.searches = Searches()
        self.instance_id = uuid.uuid4().hex
        self.runtime_id = runtime_identity()
        self.started = time.monotonic()
        self.tools: dict[str, Tool] = {}
        self.inflight: dict[str, asyncio.Task[Reply]] = {}
        self._register_tools()

    def register(
        self,
        name: str,
        description: str,
        schema: type[Input],
        handler: Callable[[Input], Awaitable[Result]],
        *,
        read_only: bool = False,
        destructive: bool = False,
        open_world: bool = False,
    ) -> None:
        if name in self.tools:
            raise ValueError(f"Duplicate tool name: {name}")

        async def checked(arguments: Contract) -> Result:
            return await handler(cast(Input, arguments))

        self.tools[name] = Tool(
            name, description, schema, checked, read_only, destructive, open_world
        )

    def _register_tools(self) -> None:
        async def settings_get(_: Empty) -> Result:
            return cast(Result, self.settings().model_dump(mode="json"))

        async def settings_update(args: UpdateSetting) -> Result:
            with self.ledger.connection:
                self.ledger.connection.execute("BEGIN IMMEDIATE")
                values = self.settings().model_dump()
                values[args.key] = args.value
                updated = RuntimeSettings.model_validate(values, strict=True)
                if (
                    updated.default_shell is not None
                    and not Path(updated.default_shell).is_absolute()
                ):
                    raise ValueError("Default shell must be an absolute path")
                self.ledger.connection.execute(
                    "INSERT INTO runtime_settings VALUES(1,?) "
                    "ON CONFLICT(id) DO UPDATE SET value=excluded.value",
                    (updated.model_dump_json(),),
                )
            return cast(Result, updated.model_dump(mode="json"))

        self.register(
            "settings_get",
            "Read persisted engine defaults and line limits.",
            Empty,
            settings_get,
            read_only=True,
        )
        self.register(
            "settings_update",
            "Update one validated engine setting persistently.",
            UpdateSetting,
            settings_update,
            destructive=True,
        )

        async def processes(args: ListProcesses) -> Result:
            return await asyncio.to_thread(list_processes, args)

        async def terminate(args: StopProcess) -> Result:
            return await asyncio.to_thread(stop_process, args)

        async def usage(_: Empty) -> Result:
            return {"groups": cast(list[JsonValue], self.ledger.usage())}

        self.register(
            "processes_list",
            "List OS processes with identity and resource counters.",
            ListProcesses,
            processes,
            read_only=True,
        )
        self.register(
            "processes_stop",
            "Terminate a PID after verifying its creation timestamp.",
            StopProcess,
            terminate,
            destructive=True,
        )
        self.register(
            "usage_stats",
            "Count recorded operations by tool and outcome state.",
            Empty,
            usage,
            read_only=True,
        )

        async def status(_: Empty) -> Result:
            return self.status()

        async def document(args: ReadDocument) -> Result:
            return await asyncio.to_thread(read_document, args)

        async def document_write(args: WriteDocument) -> Result:
            return await asyncio.to_thread(write_document, self.files, args)

        async def read(args: ReadFile) -> Result:
            args = args.model_copy(
                update={"limit": min(args.limit, self.settings().file_read_line_limit)}
            )
            return await asyncio.to_thread(self.files.read, args)

        async def read_many(args: ReadFiles) -> Result:
            results: list[JsonValue] = []
            for path in args.paths:
                try:
                    results.append(await read(ReadFile(path=path, limit=args.limit)))
                except (OSError, ValueError) as error:
                    results.append({"path": path, "error": str(error)})
            return {"files": results}

        async def write(args: WriteFile) -> Result:
            if len(args.text.splitlines()) > self.settings().file_write_line_limit:
                raise ValueError("Write exceeds configured line limit")
            return await asyncio.to_thread(self.files.write, args)

        async def read_binary(args: ReadBinary) -> Result:
            return await asyncio.to_thread(self.files.read_binary, args)

        async def write_binary(args: WriteBinary) -> Result:
            return await asyncio.to_thread(self.files.write_binary, args)

        async def download_begin(args: BeginDownload) -> Result:
            return await asyncio.to_thread(self.downloads.begin, args)

        async def download_read(args: DownloadRange) -> Result:
            return await asyncio.to_thread(self.downloads.read, args)

        async def download_status(args: TransferId) -> Result:
            return await asyncio.to_thread(self.downloads.status, args)

        async def download_close(args: TransferId) -> Result:
            return await asyncio.to_thread(self.downloads.close, args)

        async def upload_begin(args: BeginUpload) -> Result:
            return await asyncio.to_thread(self.uploads.begin, args)

        async def upload_chunk(args: UploadChunk) -> Result:
            return await asyncio.to_thread(self.uploads.chunk, args)

        async def upload_status(args: TransferId) -> Result:
            return await asyncio.to_thread(self.uploads.status, args)

        async def upload_commit(args: TransferId) -> Result:
            return await asyncio.to_thread(self.uploads.commit, args)

        async def upload_abort(args: TransferId) -> Result:
            return await asyncio.to_thread(self.uploads.abort, args)

        async def upload_resolve(args: ResolveUpload) -> Result:
            return await asyncio.to_thread(self.uploads.resolve, args)

        async def restore(args: RestoreFile) -> Result:
            return await asyncio.to_thread(self.files.restore, args)

        async def edit(args: EditFile) -> Result:
            return await asyncio.to_thread(self.files.edit, args)

        async def listing(args: ListDirectory) -> Result:
            return await asyncio.to_thread(self.files.list_directory, args)

        async def mkdir(args: FilePath) -> Result:
            path = absolute_path(args.path)
            await asyncio.to_thread(path.mkdir, parents=True, exist_ok=True)
            return {"path": str(path)}

        async def info(args: FilePath) -> Result:
            return await asyncio.to_thread(inspect_file, args.path)

        async def move(args: MoveFile) -> Result:
            return await asyncio.to_thread(self.files.move, args)

        async def search(args: StartSearch) -> Result:
            return self.searches.start(args)

        async def page(args: SearchPage) -> Result:
            return self.searches.page(args)

        async def search_stop(args: SearchId) -> Result:
            return await self.searches.stop(args.search_id)

        async def search_list(_: Empty) -> Result:
            return {
                "searches": [
                    {"search_id": entry.search_id, "state": entry.state}
                    for entry in self.searches.searches.values()
                ]
            }

        async def output(args: SessionOutput) -> Result:
            return await self.sessions.wait_output(args)

        async def session_list(_: Empty) -> Result:
            return {
                "sessions": [
                    self.sessions.describe(entry) for entry in self.sessions.sessions.values()
                ]
            }

        async def stop(args: SessionId) -> Result:
            return await self.sessions.stop(args.session_id)

        async def start_terminal(args: StartSession) -> Result:
            if args.shell is None:
                args = args.model_copy(update={"shell": self.settings().default_shell})
            return await self.sessions.start(args)

        async def operation(args: OperationId) -> Result:
            return cast(Result, self.ledger.get(args.operation_id).model_dump(mode="json"))

        async def history(args: History) -> Result:
            return {
                "operations": cast(
                    list[JsonValue],
                    self.ledger.recent(
                        args.limit,
                        tool_name=args.tool_name,
                        since=args.since,
                    ),
                )
            }

        self.register(
            "documents_read",
            "Inspect Word body text, Excel stored cells/formulas, "
            "or PowerPoint slide text without rendering or evaluating formulas.",
            ReadDocument,
            document,
            read_only=True,
        )
        self.register(
            "documents_write",
            "Generate a simple Word text document or single-sheet string workbook. "
            "Replace regenerates the entire document, requires its current hash, "
            "and retains backup.",
            WriteDocument, document_write, destructive=True,
        )
        self.register(
            "computer_status",
            "Check agent readiness, instance and active work.",
            Empty,
            status,
            read_only=True,
        )
        self.register(
            "files_read",
            "Read UTF-8 text with pagination and a content hash.",
            ReadFile,
            read,
            read_only=True,
        )
        self.register(
            "files_read_many",
            "Read up to 20 text files with per-file results.",
            ReadFiles,
            read_many,
            read_only=True,
        )
        self.register(
            "files_write",
            "Create or atomically update text. Existing files require "
            "the hash returned by files_read; updates retain a backup.",
            WriteFile,
            write,
            destructive=True,
        )
        self.register(
            "files_read_binary",
            "Read a base64 byte range (up to 256 KiB) of a regular file up to 1 GiB. "
            "Carry expected_sha256 between chunks to detect a changed download.",
            ReadBinary,
            read_binary,
            read_only=True,
        )
        self.register(
            "download_begin",
            "Save a durable copy of a regular file up to 1 GiB. Supply a fresh 32-hex "
            "transfer_id and optionally expected_sha256. Hash once; retain the ID for resume.",
            BeginDownload,
            download_begin,
        )
        self.register(
            "download_read",
            "Read up to 256 KiB from your durable download copy without rescanning the source. "
            "Verify chunk and final SHA-256 at the receiver.",
            DownloadRange,
            download_read,
            read_only=True,
        )
        self.register(
            "download_status",
            "Read durable download metadata by your transfer_id, including after restart.",
            TransferId,
            download_status,
            read_only=True,
        )
        self.register(
            "download_close",
            "Release stored download chunks. Retain closed metadata; never delete the source.",
            TransferId,
            download_close,
            destructive=True,
        )
        self.register(
            "upload_begin",
            "Reserve an upload of up to 1 GiB to a new absolute destination. "
            "Supply a fresh 32-hex transfer_id, final length and SHA-256; "
            "retain the ID for resume.",
            BeginUpload,
            upload_begin,
        )
        self.register(
            "upload_chunk",
            "Persist a contiguous canonical-base64 chunk up to 256 KiB. "
            "The same offset and identical bytes may be repeated safely.",
            UploadChunk,
            upload_chunk,
            destructive=True,
        )
        self.register(
            "upload_status",
            "Read durable received length and transfer state by your transfer_id. "
            "Unknown publication must be inspected, never automatically retried.",
            TransferId,
            upload_status,
            read_only=True,
        )
        self.register(
            "upload_commit",
            "Stream-verify the complete upload and publish exclusively at its "
            "new destination. Never overwrite. A lost publication outcome needs inspection.",
            TransferId,
            upload_commit,
            destructive=True,
        )
        self.register(
            "upload_abort",
            "Discard chunks of a receiving upload; never delete its destination. "
            "Published or uncertain uploads are not automatically aborted.",
            TransferId,
            upload_abort,
            destructive=True,
        )
        self.register(
            "files_write_binary",
            "Write up to 256 KiB of canonical base64. Create, replace or append with "
            "expected_sha256 for existing files. Each chunk is atomic and backed up; "
            "total file limit is 16 MiB. Stage under a temporary path before publishing.",
            WriteBinary,
            write_binary,
            destructive=True,
        )
        self.register(
            "upload_resolve",
            "Resolve unknown publication without publishing again: "
            "confirm_published checks the destination; discard_staging frees database chunks "
            "without deleting the destination or leftover staging_path files.",
            ResolveUpload,
            upload_resolve,
            destructive=True,
        )
        self.register(
            "files_restore",
            "Restore a text or binary backup by its ID. Existing targets require "
            "their current SHA-256; the replaced content is backed up again.",
            RestoreFile,
            restore,
            destructive=True,
        )
        self.register(
            "files_edit",
            "Replace an exact number of text matches using a read hash.",
            EditFile,
            edit,
            destructive=True,
        )
        self.register(
            "directories_list",
            "List entries without following directory symlinks.",
            ListDirectory,
            listing,
            read_only=True,
        )
        self.register("directories_create", "Ensure a directory exists.", FilePath, mkdir)
        self.register("files_info", "Inspect file metadata.", FilePath, info, read_only=True)
        self.register(
            "files_move",
            "Move a file, directory or symbolic link on the same filesystem; never overwrite.",
            MoveFile,
            move,
            destructive=True,
        )
        self.register(
            "search_start",
            "Search literal names or UTF-8 text with filename glob, directory exclusions, "
            "whole-word matching and explicit file/depth limits.",
            StartSearch,
            search,
        )
        self.register(
            "search_results", "Read a search page by cursor.", SearchPage, page, read_only=True
        )
        self.register("search_stop", "Cancel a running search.", SearchId, search_stop)
        self.register(
            "search_list", "List searches owned by this agent.", Empty, search_list, read_only=True
        )
        self.register(
            "terminal_start",
            "Start a shell command in an absolute working directory. "
            "It continues when an MCP client disconnects.",
            StartSession,
            start_terminal,
            destructive=True,
            open_world=True,
        )
        self.register(
            "terminal_input",
            "Send exact input to an existing terminal session. "
            "Include a newline when a command needs one.",
            SessionInput,
            self.sessions.send,
            destructive=True,
            open_world=True,
        )
        self.register(
            "terminal_output",
            "Read output using a byte cursor; negative cursors start relative to the current "
            "output end. Returns an absolute next_cursor and reports dropped bytes.",
            SessionOutput,
            output,
            read_only=True,
        )
        self.register(
            "terminal_list",
            "List sessions on this agent instance.",
            Empty,
            session_list,
            read_only=True,
        )
        self.register(
            "terminal_stop",
            "Terminate a session and its process group.",
            SessionId,
            stop,
            destructive=True,
        )
        self.register(
            "operations_get",
            "Recover the recorded outcome of an operation ID.",
            OperationId,
            operation,
            read_only=True,
        )
        self.register(
            "operations_recent",
            "List operation summaries without arguments or output.",
            History,
            history,
            read_only=True,
        )

    def settings(self) -> RuntimeSettings:
        row = self.ledger.connection.execute(
            "SELECT value FROM runtime_settings WHERE id=1"
        ).fetchone()
        return RuntimeSettings.model_validate_json(row[0]) if row else RuntimeSettings()

    def catalog(self, allowed: frozenset[str] | None = None) -> list[JsonValue]:
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "inputSchema": tool.schema.model_json_schema(),
                "outputSchema": Reply.model_json_schema(),
                "annotations": {
                    "readOnlyHint": tool.read_only,
                    "destructiveHint": tool.destructive,
                    "openWorldHint": tool.open_world,
                },
            }
            for tool in self.tools.values()
            if allowed is None or tool.name in allowed
        ]

    def status(self) -> Result:
        return {
            "state": "ready",
            "version": __version__,
            "instance_id": self.instance_id,
            "runtime_id": self.runtime_id,
            "uptime_seconds": time.monotonic() - self.started,
            "platform": platform.system(),
            "active_sessions": sum(
                s.process.returncode is None for s in self.sessions.sessions.values()
            ),
            "active_operations": len(self.inflight),
            "tools": len(self.tools),
            "transport": "authenticated-loopback",
            "remote_ready": False,
            "capabilities": {
                "files": True,
                "terminal": True,
                "literal_search": True,
                "remote": False,
                "office": False,
                "office_text_read": True,
                "gui": False,
            },
        }

    async def execute(self, request: Request) -> Reply:
        tool = self.tools.get(request.tool)
        if tool is None:
            return Reply(operation_id=request.operation_id, state="failed", error="Unknown tool")
        try:
            arguments = tool.schema.model_validate(request.arguments)
            previous = self.ledger.claim(request)
        except ValueError as error:
            return Reply(operation_id=request.operation_id, state="failed", error=str(error))
        if previous is not None:
            running = self.inflight.get(request.operation_id)
            return await asyncio.shield(running) if running else previous

        async def run() -> Reply:
            try:
                result = await tool.handler(arguments)
                reply = Reply(operation_id=request.operation_id, state="completed", data=result)
            except UploadOutcomeUnknown as error:
                reply = Reply(operation_id=request.operation_id, state="unknown", error=str(error))
            except Exception as error:
                reply = Reply(operation_id=request.operation_id, state="failed", error=str(error))
            self.ledger.finish(reply)
            return reply

        task = asyncio.create_task(run())
        self.inflight[request.operation_id] = task
        task.add_done_callback(lambda _: self.inflight.pop(request.operation_id, None))
        return await asyncio.shield(task)

    async def close(self) -> None:
        if self.inflight:
            await asyncio.gather(*list(self.inflight.values()), return_exceptions=True)
        await self.searches.close()
        await self.sessions.close()
        self.ledger.close()
