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
from .documents import read_document
from .files import Files, absolute_path
from .models import (
    Contract,
    EditFile,
    Empty,
    FilePath,
    History,
    ListDirectory,
    MoveFile,
    OperationId,
    ReadDocument,
    ReadFile,
    ReadFiles,
    Reply,
    Request,
    RestoreFile,
    SearchId,
    SearchPage,
    SessionId,
    SessionInput,
    SessionOutput,
    StartSearch,
    StartSession,
    WriteFile,
)
from .runtime_identity import runtime_identity
from .search import Searches
from .sessions import Sessions
from .state import Ledger

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
    def __init__(self, directory: Path) -> None:
        self.ledger = Ledger(directory)
        self.files = Files(directory)
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
        async def status(_: Empty) -> Result:
            return self.status()

        async def document(args: ReadDocument) -> Result:
            return await asyncio.to_thread(read_document, args)

        async def read(args: ReadFile) -> Result:
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
            return await asyncio.to_thread(self.files.write, args)

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
            path = absolute_path(args.path)
            metadata = await asyncio.to_thread(path.stat)
            return {
                "path": str(path),
                "size": metadata.st_size,
                "modified": metadata.st_mtime,
                "directory": path.is_dir(),
                "symlink": path.is_symlink(),
            }

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
            return self.sessions.output(args)

        async def session_list(_: Empty) -> Result:
            return {
                "sessions": [
                    self.sessions.describe(entry) for entry in self.sessions.sessions.values()
                ]
            }

        async def stop(args: SessionId) -> Result:
            return await self.sessions.stop(args.session_id)

        async def operation(args: OperationId) -> Result:
            return cast(Result, self.ledger.get(args.operation_id).model_dump(mode="json"))

        async def history(args: History) -> Result:
            return {"operations": cast(list[JsonValue], self.ledger.recent(args.limit))}

        self.register(
            "documents_read",
            "Inspect Word body text, Excel stored cells/formulas, "
            "or PowerPoint slide text without rendering or evaluating formulas.",
            ReadDocument,
            document,
            read_only=True,
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
            "files_restore",
            "Restore a UTF-8 backup by its ID. Existing targets require "
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
            "Move a regular file on the same filesystem; never overwrite.",
            MoveFile,
            move,
            destructive=True,
        )
        self.register(
            "search_start", "Start a bounded literal name or text search.", StartSearch, search
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
            self.sessions.start,
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
            "Read output using a byte cursor; reports dropped bytes.",
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
