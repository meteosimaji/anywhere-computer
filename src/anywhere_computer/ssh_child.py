"""Authenticated delegated MCP session over a trusted SSH stdio channel."""

import asyncio
import sys
from contextlib import ExitStack
from pathlib import Path

from pydantic import JsonValue

from .authorized_http import AuthorizedDeviceMCP
from .delegated_tasks import DelegatedTaskStore
from .engine import Engine
from .http_service import _check_enrollment, _http_authority
from .mcp_server import MCPSession, rpc_error, serve_stdio

AUTH_METHOD = "anywhere/childAuthenticate"


class SSHChildSession(MCPSession):
    """No catalog or tool call is reachable until a verified child logs in."""

    def __init__(self, backend: AuthorizedDeviceMCP) -> None:
        self.backend = backend
        self.session: MCPSession | None = None
        self.ready = False
        self._authentication_lock = asyncio.Lock()

    async def handle(self, packet: JsonValue) -> dict[str, JsonValue] | None:
        if not isinstance(packet, dict) or packet.get("jsonrpc") != "2.0":
            return rpc_error(None, -32600, "Invalid JSON-RPC request")
        identity = packet.get("id")
        if self.session is None:
            if packet.get("method") != AUTH_METHOD or "id" not in packet:
                return rpc_error(identity, -32001, "Child authentication required")
            params = packet.get("params")
            bearer = params.get("bearer") if isinstance(params, dict) else None
            if not isinstance(bearer, str) or not 1 <= len(bearer) <= 256:
                return rpc_error(identity, -32001, "Child authentication failed")
            async with self._authentication_lock:
                if self.session is not None:
                    return rpc_error(identity, -32600, "Child session already authenticated")
                grant_id = await self.backend.authenticate(bearer)
                if grant_id is None or not grant_id.startswith("child:"):
                    return rpc_error(identity, -32001, "Child authentication failed")
                self.session = self.backend.session(grant_id)
            return {"jsonrpc": "2.0", "id": identity, "result": {"authenticated": True}}
        if packet.get("method") == AUTH_METHOD:
            return rpc_error(identity, -32600, "Child session already authenticated")
        response = await self.session.handle(packet)
        self.ready = self.session.ready
        return response


async def run_ssh_child_mcp(directory: Path) -> None:
    with ExitStack() as resources:
        config, authority = resources.enter_context(_http_authority(directory))
        _check_enrollment(authority, config)
        delegated = DelegatedTaskStore(directory / "http-server" / "delegated-tasks", authority)
        resources.callback(delegated.close)
        agent_directory = (Path(config.shared_agent_directory)
                           if config.shared_agent_directory is not None else None)
        engine = None
        if agent_directory is None:
            engine = Engine(directory / "http-server" / "engine",
                            file_locks=directory / "file-locks")
        try:
            backend = AuthorizedDeviceMCP(
                authority, engine, agent_directory=agent_directory,
                owner=config.owner, device=config.device, client=config.client,
                allowed_tools=config.scopes, device_directory=agent_directory or directory,
                delegated_tasks=delegated,
            )
            await serve_stdio(SSHChildSession(backend), sys.stdin.buffer, sys.stdout.buffer)
        finally:
            if engine is not None:
                await engine.close()
