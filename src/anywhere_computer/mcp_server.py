"""Small MCP stdio implementation; the SDK is used only for interoperability tests."""

import asyncio
import json
import re
import sys
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import BinaryIO, cast

from pydantic import JsonValue

from . import __version__
from .connection import WIRE_LIMIT, ensure_agent, exchange
from .models import Reply, Request

Catalog = Callable[[], Awaitable[list[JsonValue]]]
Execute = Callable[[Request], Awaitable[Reply]]
PROTOCOL_VERSION = "2025-11-25"
OPERATION_CAPABILITY = "io.github.meteosimaji.anywhere-computer"
OPERATION_META = OPERATION_CAPABILITY + "/operation_id"
INSTRUCTIONS = (
    "Check computer_status before operating. Use absolute paths. Existing writes require "
    "the SHA-256 from a recent read. Terminal sessions survive this connection. After a "
    "lost response, inspect the operation ID rather than repeating a write."
)


def rpc_error(identity: JsonValue, code: int, message: str) -> dict[str, JsonValue]:
    return {"jsonrpc": "2.0", "id": identity, "error": {"code": code, "message": message}}


class MCPSession:
    """Transport-independent tools-only MCP session; no unadvertised capabilities."""

    def __init__(self, catalog: Catalog, execute: Execute) -> None:
        self.catalog = catalog
        self.execute = execute
        self.initialized = False
        self.ready = False

    async def handle(self, packet: JsonValue) -> dict[str, JsonValue] | None:
        if not isinstance(packet, dict):
            return rpc_error(None, -32600, "Expected one JSON-RPC message")
        identity = packet.get("id")
        notification = "id" not in packet
        if packet.get("jsonrpc") != "2.0" or not isinstance(packet.get("method"), str):
            return rpc_error(None, -32600, "Invalid JSON-RPC request")
        if not notification and (
            isinstance(identity, bool) or not isinstance(identity, (str, int))
        ):
            return rpc_error(None, -32600, "Invalid request ID")
        method = packet["method"]
        params = packet.get("params", {})
        if not isinstance(params, dict):
            return None if notification else rpc_error(identity, -32602, "Invalid parameters")
        if notification:
            if method == "notifications/initialized" and self.initialized:
                self.ready = True
            # Cancellation of an observer does not stop durable work. Explicit terminal_stop
            # and search_stop tools control the underlying activities.
            return None
        result: dict[str, JsonValue]
        if method == "ping":
            result = {}
        elif method == "initialize":
            if self.initialized:
                return rpc_error(identity, -32600, "Session is already initialized")
            if (
                not isinstance(params.get("protocolVersion"), str)
                or not isinstance(params.get("capabilities"), dict)
                or not isinstance(params.get("clientInfo"), dict)
            ):
                return rpc_error(identity, -32602, "Invalid initialization parameters")
            self.initialized = True
            result = {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {
                    "tools": {"listChanged": False},
                    "experimental": {OPERATION_CAPABILITY: {"operationId": True}},
                },
                "serverInfo": {"name": "anywhere-computer", "version": __version__},
                "instructions": INSTRUCTIONS,
            }
        elif not self.ready:
            return rpc_error(identity, -32600, "Initialize the session first")
        elif method == "tools/list":
            if params.get("cursor") is not None:
                return rpc_error(identity, -32602, "No continuation cursor exists")
            result = {"tools": await self.catalog()}
        elif method == "tools/call":
            name, arguments = params.get("name"), params.get("arguments", {})
            if not isinstance(name, str) or not isinstance(arguments, dict):
                return rpc_error(identity, -32602, "Invalid tool call")
            names = {
                item.get("name")
                for item in await self.catalog()
                if isinstance(item, dict) and isinstance(item.get("name"), str)
            }
            if name not in names:
                return rpc_error(identity, -32602, "Unknown tool")
            metadata = params.get("_meta", {})
            if not isinstance(metadata, dict):
                return rpc_error(identity, -32602, "Invalid tool metadata")
            operation_id = metadata.get(OPERATION_META, uuid.uuid4().hex)
            if (
                not isinstance(operation_id, str)
                or re.fullmatch(r"[a-f0-9]{32}", operation_id) is None
            ):
                return rpc_error(identity, -32602, "Invalid operation ID")
            operation = Request(operation_id=operation_id, tool=name, arguments=arguments)
            try:
                reply = await self.execute(operation)
            except (OSError, ValueError, RuntimeError, TimeoutError):
                reply = Reply(
                    operation_id=operation.operation_id,
                    state="unknown",
                    error="Connection interrupted. Query operations_get with this operation_id. "
                    "Do not repeat a write until its outcome is known.",
                )
            result = {
                "content": [{"type": "text", "text": reply.model_dump_json()}],
                "structuredContent": cast(dict[str, JsonValue], reply.model_dump(mode="json")),
                "isError": reply.state != "completed",
            }
        else:
            return rpc_error(identity, -32601, "Method not found")
        return {"jsonrpc": "2.0", "id": identity, "result": result}


async def serve_stdio(session: MCPSession, source: BinaryIO, destination: BinaryIO) -> None:
    write_lock = asyncio.Lock()
    tasks: set[asyncio.Task[None]] = set()

    async def respond(line: bytes) -> None:
        response: dict[str, JsonValue] | None
        try:
            try:
                packet = json.loads(
                    line,
                    parse_constant=lambda _: (_ for _ in ()).throw(
                        ValueError("Non-finite JSON number")
                    ),
                )
            except (ValueError, UnicodeError):
                response = rpc_error(None, -32700, "Invalid JSON")
            else:
                response = await session.handle(packet)
        except Exception:
            response = rpc_error(None, -32603, "Internal error")
        if response is not None:
            encoded = json.dumps(response, ensure_ascii=False, allow_nan=False).encode() + b"\n"
            async with write_lock:
                await asyncio.to_thread(destination.write, encoded)
                await asyncio.to_thread(destination.flush)

    try:
        while line := await asyncio.to_thread(source.readline, WIRE_LIMIT + 1):
            if len(line) > WIRE_LIMIT or not line.endswith(b"\n"):
                raise ValueError("Invalid MCP message framing or size")
            if len(tasks) >= 32:
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            task = asyncio.create_task(respond(line))
            tasks.add(task)
            task.add_done_callback(tasks.discard)
        if tasks:
            await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def run_mcp(directory: Path) -> None:
    from .device_router import DeviceRouter

    async def catalog() -> list[JsonValue]:
        reply = await exchange(directory, "__catalog")
        raw = reply.data.get("tools")
        if not isinstance(raw, list):
            raise ValueError("Agent did not return a tool catalog")
        return raw

    async def execute(request: Request) -> Reply:
        await asyncio.to_thread(ensure_agent, directory)
        return await exchange(
            directory, request.tool, request.arguments, operation_id=request.operation_id
        )

    router = DeviceRouter(directory, catalog, execute)
    try:
        await serve_stdio(
            MCPSession(router.catalog, router.execute), sys.stdin.buffer, sys.stdout.buffer,
        )
    finally:
        router.close()
