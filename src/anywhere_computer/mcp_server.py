"""A thin stdio connection to the independently running agent."""

import asyncio
import uuid
from pathlib import Path
from typing import Any

from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from .connection import ensure_agent, exchange
from .models import Reply


async def run_mcp(directory: Path) -> None:
    catalog = await exchange(directory, "__catalog")
    raw_tools = catalog.data.get("tools")
    if not isinstance(raw_tools, list):
        raise ValueError("Agent did not return a tool catalog")
    tools = [types.Tool.model_validate(item) for item in raw_tools]
    server = Server(
        "anywhere-computer",
        instructions=(
            "Check computer_status before operating. Use absolute paths. Existing writes require "
            "the SHA-256 from a recent read. Terminal sessions survive this connection. After a "
            "lost response, inspect the operation ID rather than repeating a write."
        ),
    )

    @server.list_tools()  # type: ignore[no-untyped-call,untyped-decorator]
    async def list_tools() -> list[types.Tool]:
        return tools

    @server.call_tool()  # type: ignore[untyped-decorator]
    async def call_tool(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        operation_id = uuid.uuid4().hex
        try:
            # Readiness is checked BEFORE dispatch. Never replay an uncertain write.
            await asyncio.to_thread(ensure_agent, directory)
            reply = await exchange(directory, name, arguments, operation_id=operation_id)
        except (OSError, ValueError, RuntimeError, TimeoutError):
            reply = Reply(
                operation_id=operation_id,
                state="unknown",
                error=(
                    "Connection interrupted. Use operations_get with this operation_id. "
                    "Do not repeat a write until its outcome is known."
                ),
            )
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=reply.model_dump_json())],
            structuredContent=reply.model_dump(mode="json"),
            isError=reply.state != "completed",
        )

    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())
