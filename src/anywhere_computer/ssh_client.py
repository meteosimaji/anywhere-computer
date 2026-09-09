"""One-operation MCP client over an existing strict-host-key SSH connection."""

import asyncio
import json
import uuid

from pydantic import JsonValue

from .connection import WIRE_LIMIT
from .mcp_server import OPERATION_CAPABILITY, OPERATION_META, PROTOCOL_VERSION
from .models import Reply, Request
from .ssh_transport import ssh_command


def _reject_constant(value: str) -> None:
    raise ValueError("Non-finite SSH JSON number")


class SSHBackend:
    def __init__(self, host: str) -> None:
        self.host = host
        self.process: asyncio.subprocess.Process | None = None

    async def _write(self, packet: dict[str, JsonValue]) -> None:
        if self.process is None or self.process.stdin is None:
            raise ConnectionError("SSH connection is not open")
        encoded = json.dumps(packet, ensure_ascii=False, allow_nan=False).encode() + b"\n"
        if len(encoded) > WIRE_LIMIT:
            raise ValueError("SSH MCP request exceeds size limit")
        self.process.stdin.write(encoded)
        await self.process.stdin.drain()

    async def _rpc(self, method: str, params: dict[str, JsonValue]) -> dict[str, JsonValue]:
        identity = uuid.uuid4().hex
        async with asyncio.timeout(65):
            await self._write({
                "jsonrpc": "2.0", "id": identity, "method": method, "params": params,
            })
            if self.process is None or self.process.stdout is None:
                raise ConnectionError("SSH connection is not open")
            # The peer may emit notifications; bounded time and line size still apply.
            while True:
                line = await self.process.stdout.readline()
                if not line or len(line) > WIRE_LIMIT or not line.endswith(b"\n"):
                    raise ConnectionError("SSH MCP response was not confirmed")
                packet = json.loads(line, parse_constant=_reject_constant)
                if not isinstance(packet, dict) or packet.get("jsonrpc") != "2.0":
                    raise ConnectionError("Invalid SSH MCP response")
                if "id" not in packet and isinstance(packet.get("method"), str):
                    continue
                if packet.get("id") != identity or "error" in packet or "method" in packet:
                    raise ConnectionError("SSH MCP response did not match the request")
                result = packet.get("result")
                if not isinstance(result, dict):
                    raise ConnectionError("Invalid SSH MCP result")
                return result

    async def _connect(self) -> None:
        if self.process is not None:
            return
        self.process = await asyncio.create_subprocess_exec(
            *ssh_command(self.host), stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, limit=WIRE_LIMIT,
        )
        result = await self._rpc("initialize", {
            "protocolVersion": PROTOCOL_VERSION, "capabilities": {},
            "clientInfo": {"name": "anywhere-computer-device", "version": "1"},
        })
        capabilities = result.get("capabilities")
        experimental = capabilities.get("experimental") if isinstance(capabilities, dict) else None
        extension = (
            experimental.get(OPERATION_CAPABILITY) if isinstance(experimental, dict) else None
        )
        if (result.get("protocolVersion") != PROTOCOL_VERSION
                or not isinstance(extension, dict) or extension.get("operationId") is not True):
            raise ConnectionError("Remote agent does not support recoverable operation IDs")
        await self._write({"jsonrpc": "2.0", "method": "notifications/initialized"})

    async def catalog(self) -> list[JsonValue]:
        await self._connect()
        result = await self._rpc("tools/list", {})
        tools = result.get("tools")
        if not isinstance(tools, list):
            raise ConnectionError("Invalid SSH tool catalog")
        return tools

    async def execute(self, request: Request) -> Reply:
        await self._connect()
        result = await self._rpc("tools/call", {
            "name": request.tool, "arguments": request.arguments,
            "_meta": {OPERATION_META: request.operation_id},
        })
        reply = Reply.model_validate(result.get("structuredContent"))
        if reply.operation_id != request.operation_id:
            raise ConnectionError("SSH operation ID mismatch")
        return reply

    async def close(self) -> None:
        process, self.process = self.process, None
        if process is None:
            return
        if process.stdin is not None:
            process.stdin.close()
        try:
            await asyncio.wait_for(process.wait(), 2)
        except TimeoutError:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), 2)
            except TimeoutError:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()
