"""Connect enrolled TLS peers to an agent and expose the remote catalog through MCP.

All enrolled peers belong to the device owner. Tool grants are not filesystem,
process or multi-user isolation. Remote operation IDs are isolated per peer.
"""

import hashlib
import re
import ssl
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from pydantic import JsonValue

from .authorization import LOCAL_ONLY_TOOLS
from .downloads import DOWNLOAD_TOOLS
from .engine import Engine
from .mcp_server import MCPSession
from .models import OperationId, Reply, Request, TransferId
from .remote_transport import remote_exchange
from .uploads import UPLOAD_TOOLS


class RemoteAgent:
    def __init__(
        self,
        engine: Engine,
        grants: Mapping[str, frozenset[str]],
        *,
        transport: Literal["mutual-tls", "http"] = "mutual-tls",
    ) -> None:
        self.engine = engine
        self.transport = transport
        self.grants: dict[str, frozenset[str]] = {}
        for identity, tools in grants.items():
            self.grant(identity, tools)

    def grant(self, identity: str, tools: frozenset[str]) -> None:
        if not identity or len(identity) > 128:
            raise ValueError("Invalid enrolled identity")
        if tools - self.engine.tools.keys():
            raise ValueError("Grant contains unknown tools")
        if tools & LOCAL_ONLY_TOOLS:
            raise ValueError("Global history is not exposed to remote peers")
        self.grants[identity] = tools

    def revoke(self, identity: str) -> None:
        self.grants.pop(identity, None)

    @staticmethod
    def internal_id(identity: str, external_id: str) -> str:
        if re.fullmatch(r"[a-f0-9]{32}", external_id) is None:
            raise ValueError("Invalid remote operation ID")
        return hashlib.sha256(f"{len(identity)}:{identity}:{external_id}".encode()).hexdigest()[:32]

    async def dispatch(self, identity: str, payload: bytes) -> bytes:
        request = Request.model_validate_json(payload)
        allowed = self.grants.get(identity)
        if allowed is None:
            reply = Reply(operation_id=request.operation_id, state="failed", error="Peer revoked")
        elif request.tool == "__catalog":
            reply = Reply(
                operation_id=request.operation_id,
                state="completed",
                data={"tools": self.engine.catalog(allowed)},
            )
        elif request.tool not in allowed:
            reply = Reply(
                operation_id=request.operation_id,
                state="failed",
                error="Tool is not granted to this peer",
            )
        else:
            reply = await self._execute(identity, request)
        return reply.model_dump_json().encode()

    async def _execute(self, identity: str, request: Request) -> Reply:
        internal = self.internal_id(identity, request.operation_id)
        arguments = dict(request.arguments)
        if request.tool in UPLOAD_TOOLS | DOWNLOAD_TOOLS:
            try:
                transfer = TransferId.model_validate({"transfer_id": arguments.get("transfer_id")})
                arguments["transfer_id"] = self.internal_id(identity, transfer.transfer_id)
            except ValueError:
                return Reply(
                    operation_id=request.operation_id, state="failed", error="Invalid transfer ID"
                )
        target_id = None
        if request.tool == "operations_get":
            try:
                target_id = OperationId.model_validate(arguments).operation_id
                arguments["operation_id"] = self.internal_id(identity, target_id)
                target_tool = self.engine.ledger.tool_for(str(arguments["operation_id"]))
                if target_tool not in self.grants.get(identity, frozenset()):
                    return Reply(
                        operation_id=request.operation_id,
                        state="failed",
                        error="Operation is unknown or its tool is no longer granted",
                    )
            except ValueError:
                return Reply(
                    operation_id=request.operation_id,
                    state="failed",
                    error="Invalid operation lookup",
                )
        forwarded = Request(operation_id=internal, tool=request.tool, arguments=arguments)
        result = await self.engine.execute(forwarded, peer=identity)
        data = dict(result.data)
        if target_id is not None and result.state == "completed":
            data["operation_id"] = target_id
        if request.tool == "computer_status" and result.state == "completed":
            data["transport"] = self.transport
            # This establishes this channel, not NAT/internet reachability or HTTP MCP.
            data["remote_channel_authenticated"] = True
        return result.model_copy(update={"operation_id": request.operation_id, "data": data})


@dataclass(frozen=True)
class RemoteBackend:
    host: str
    port: int
    server_name: str
    fingerprint: str
    context: ssl.SSLContext

    async def execute(self, request: Request) -> Reply:
        payload = await remote_exchange(
            self.host,
            self.port,
            request.model_dump_json().encode(),
            context=self.context,
            server_name=self.server_name,
            expected_fingerprint=self.fingerprint,
        )
        reply = Reply.model_validate_json(payload)
        if reply.operation_id != request.operation_id:
            raise ConnectionError("Remote response has a different operation ID")
        return reply

    async def catalog(self) -> list[JsonValue]:
        reply = await self.execute(Request(operation_id=uuid.uuid4().hex, tool="__catalog"))
        tools = reply.data.get("tools")
        if reply.state != "completed" or not isinstance(tools, list):
            raise ConnectionError("Remote catalog is unavailable")
        return tools

    def mcp_session(self) -> MCPSession:
        return MCPSession(self.catalog, self.execute)
