"""Bound stdio frames before the MCP SDK parses peer-controlled JSON."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from anyio.abc import ByteReceiveStream
    from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
    from mcp.client.stdio import StdioServerParameters
    from mcp.shared.message import SessionMessage

FRAME_LIMIT = 8 * 1024 * 1024


@dataclass
class StdioLifecycle:
    """Conservative process cleanup evidence, independent of task completion."""

    cleanup_confirmed: bool = True


async def bounded_lines(
    source: 'ByteReceiveStream', *, limit: int = FRAME_LIMIT,
) -> AsyncIterator[bytes]:
    import anyio

    buffer = bytearray()
    while True:
        try:
            chunk = await source.receive(16384)
        except anyio.EndOfStream:
            if buffer:
                raise ValueError('MCP stream ended inside a frame') from None
            return
        if not chunk:
            raise ValueError('MCP stream returned an empty chunk')
        buffer.extend(chunk)
        while (boundary := buffer.find(b'\n')) >= 0:
            if boundary > limit:
                raise ValueError('MCP frame exceeds the byte limit')
            line = bytes(buffer[:boundary])
            del buffer[:boundary + 1]
            yield line
        if len(buffer) > limit:
            raise ValueError('MCP frame exceeds the byte limit')


@asynccontextmanager
async def bounded_stdio(
    server: 'StdioServerParameters', *, lifecycle: StdioLifecycle | None = None,
) -> AsyncIterator[tuple['MemoryObjectReceiveStream[SessionMessage | Exception]',
                         'MemoryObjectSendStream[SessionMessage]']]:
    import os

    import anyio
    from mcp import types
    from mcp.client.stdio import (
        _create_platform_compatible_process,
        _terminate_process_tree,
        get_default_environment,
    )
    from mcp.shared.message import SessionMessage

    # The optional runtime is pinned to MCP SDK 1.30.0. Keep its Windows Job
    # Object / POSIX process-group lifecycle helpers; replace only unbounded
    # framing and raw parse-error logging. Exercise these helpers in real tests.
    incoming, incoming_reader = anyio.create_memory_object_stream[SessionMessage | Exception](0)
    outgoing, outgoing_reader = anyio.create_memory_object_stream[SessionMessage](0)
    with open(os.devnull, 'w') as errors:
        if lifecycle is not None:
            lifecycle.cleanup_confirmed = False
        process = await _create_platform_compatible_process(
            server.command, server.args, get_default_environment(), errors, server.cwd,
        )

        async def receive() -> None:
            assert process.stdout is not None
            async with incoming:
                async for line in bounded_lines(process.stdout):
                    try:
                        message = types.JSONRPCMessage.model_validate_json(line)
                    except ValueError:
                        raise ValueError('Invalid MCP JSON-RPC frame') from None
                    await incoming.send(SessionMessage(message))

        async def send() -> None:
            assert process.stdin is not None
            async with outgoing_reader:
                async for message in outgoing_reader:
                    data = message.message.model_dump_json(
                        by_alias=True, exclude_none=True,
                    ).encode()
                    if len(data) > FRAME_LIMIT:
                        raise ValueError('Outgoing MCP frame exceeds the byte limit')
                    await process.stdin.send(data + b'\n')

        async with anyio.create_task_group() as group:
            group.start_soon(receive)
            group.start_soon(send)
            try:
                yield incoming_reader, outgoing
            finally:
                with anyio.CancelScope(shield=True):
                    if process.stdin is not None:
                        try:
                            await process.stdin.aclose()
                        except (anyio.BrokenResourceError, anyio.ClosedResourceError, OSError):
                            pass
                    with anyio.move_on_after(2) as grace:
                        await process.wait()
                    if grace.cancel_called:
                        await _terminate_process_tree(process)
                    await process.aclose()
                    if lifecycle is not None:
                        lifecycle.cleanup_confirmed = True
                    await incoming_reader.aclose()
                    await outgoing.aclose()
                group.cancel_scope.cancel()
