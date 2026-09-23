"""Connect enrolled TLS peers to an agent and expose the remote catalog through MCP.

All enrolled peers belong to the device owner. Tool grants are not filesystem,
process or multi-user isolation. Remote operation IDs are isolated per peer.
"""

import hashlib
import math
import re
import ssl
import time
import uuid
from collections.abc import Callable, Mapping
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

_CAPABILITY_GRANT_TOOLS: dict[str, frozenset[str]] = {
    "files": frozenset({"files_read", "files_write"}),
    "terminal": frozenset({"terminal_start", "terminal_output", "terminal_input",
                            "terminal_list", "terminal_stop"}),
    "literal_search": frozenset({"search_start", "search_results", "search_list",
                                  "search_stop"}),
    "office_text_read": frozenset({"documents_read"}),
    "audio_capture": frozenset({"audio_status", "audio_capture"}),
    "gui_native": frozenset({"gui_native_windows", "gui_native_observe", "gui_native_close"}),
    "gui_mcp": frozenset({"gui_observe", "gui_click", "gui_type", "gui_key"}),
    "skills": frozenset({"skills_list", "skills_read"}),
    "codex_skills": frozenset({"codex_skills_list", "codex_skill_read"}),
}


class RemoteAgent:
    def __init__(
        self,
        engine: Engine,
        grants: Mapping[str, frozenset[str]],
        *,
        transport: Literal["mutual-tls", "http"] = "mutual-tls",
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.engine = engine
        self.transport = transport
        self._clock = clock
        self._expires: dict[str, float] = {}
        self.grants: dict[str, frozenset[str]] = {}
        for identity, tools in grants.items():
            self.grant(identity, tools)

    def grant(self, identity: str, tools: frozenset[str], *,
              expires_at: float | None = None) -> None:
        """Install trusted permissions; token validation belongs to the caller.

        Existing enrolled TLS peers may omit expiry. Relay grants must supply
        their verified absolute expiry; refreshing them is an explicit action.
        """
        if not identity or len(identity) > 128:
            raise ValueError("Invalid enrolled identity")
        if tools - self.engine.tools.keys():
            raise ValueError("Grant contains unknown tools")
        if tools & LOCAL_ONLY_TOOLS:
            raise ValueError("Global history is not exposed to remote peers")
        if expires_at is not None:
            now = self._clock()
            if (isinstance(expires_at, bool) or not math.isfinite(expires_at)
                    or not math.isfinite(now) or expires_at <= now):
                raise ValueError("Grant expiry must be finite and in the future")
        self.grants[identity] = tools
        if expires_at is None:
            self._expires.pop(identity, None)
        else:
            self._expires[identity] = expires_at

    def revoke(self, identity: str) -> None:
        self.grants.pop(identity, None)
        self._expires.pop(identity, None)

    def _allowed(self, identity: str) -> frozenset[str] | None:
        expiry = self._expires.get(identity)
        if expiry is not None:
            now = self._clock()
            if not math.isfinite(now) or now >= expiry:
                self.revoke(identity)
                return None
        return self.grants.get(identity)

    @staticmethod
    def internal_id(identity: str, external_id: str) -> str:
        if re.fullmatch(r"[a-f0-9]{32}", external_id) is None:
            raise ValueError("Invalid remote operation ID")
        return hashlib.sha256(f"{len(identity)}:{identity}:{external_id}".encode()).hexdigest()[:32]

    async def dispatch(self, identity: str, payload: bytes) -> bytes:
        request = Request.model_validate_json(payload)
        allowed = self._allowed(identity)
        if allowed is None:
            reply = Reply(operation_id=request.operation_id, state="failed",
                          error="Connection authorization is absent or expired",
                          data={"error_code": "authentication_required",
                                "dispatched": False,
                                "execution_state": "not_dispatched",
                                "next_action": "Use the separate connection authorization flow to "
                                "review or renew this peer's grant; no permission was changed."})
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
                error="This capability is not granted to the connection",
                data={"error_code": "capability_not_authorized",
                      "dispatched": False, "execution_state": "not_dispatched",
                      "next_action": "Review this connection's tool grant in its separate "
                      "authorization flow; no permission was changed."},
            )
        else:
            reply = await self._execute(identity, request, allowed=allowed)
        return reply.model_dump_json().encode()

    async def _execute(
        self, identity: str, request: Request, *, allowed: frozenset[str],
    ) -> Reply:
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
                if target_tool not in (self._allowed(identity) or frozenset()):
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
            diagnostics = data.get("capability_diagnostics")
            if isinstance(diagnostics, dict):
                for capability, required in _CAPABILITY_GRANT_TOOLS.items():
                    granted = required & allowed
                    if not granted:
                        state = "not_granted"
                    elif granted == required:
                        state = "authorized"
                    else:
                        state = "partial_grant"
                    next_action = (
                        "No grant change is indicated by this diagnostic."
                        if state == "authorized" else
                        "Review this connection's grant in its separate authorization flow; "
                        "no permission was changed."
                    )
                    raw = diagnostics.get(capability)
                    if isinstance(raw, dict):
                        raw["connection_authorization"] = state
                        raw["authorization_next_action"] = next_action
                    else:
                        diagnostics[capability] = {
                            "running_implementation": "unknown",
                            "runtime_available": "unknown",
                            "connection_authorization": state,
                            "authorization_next_action": next_action,
                            "helper": "not_checked",
                            "os_permission": "not_checked",
                            "acceptance": "not_verified",
                        }
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
