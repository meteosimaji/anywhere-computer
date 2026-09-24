"""Bind verified HTTP grants to one owner's device and its current tool permissions."""

import asyncio
import uuid
from pathlib import Path

from pydantic import JsonValue

from .authorization import AuthorizationStore, GrantIdentity
from .connection import ensure_agent, exchange_remote
from .device_router import NESTED_REQUEST_ID_ERROR, ROUTER_TOOLS, DeviceRouter
from .engine import Engine
from .mcp_server import INSTRUCTIONS as MCP_INSTRUCTIONS
from .mcp_server import OPERATION_META, MCPSession, rpc_error
from .models import OperationId, Reply, Request
from .remote_bridge import RemoteAgent
from .subchat_gateway import SUBCHAT_GATEWAY_TOOLS, SubchatGateway


class SubchatHTTPSession(MCPSession):
    """Require a client-known ID before dispatching an ordinary Chat input."""

    async def handle(self, packet: JsonValue) -> dict[str, JsonValue] | None:
        if (isinstance(packet, dict) and "id" in packet
                and packet.get("jsonrpc") == "2.0"
                and packet.get("method") == "tools/call"):
            params = packet.get("params")
            if isinstance(params, dict) and params.get("name") in {
                "subchat_send", "subchat_message",
            }:
                arguments = params.get("arguments")
                metadata = params.get("_meta")
                supplied = (isinstance(arguments, dict) and "request_id" in arguments)
                supplied = supplied or (isinstance(metadata, dict)
                                        and OPERATION_META in metadata)
                if not supplied:
                    return rpc_error(packet.get("id"), -32602,
                                     "Choose request_id before sending a Subchat input")
        return await super().handle(packet)


class AuthorizedDeviceMCP:
    def __init__(
        self,
        store: AuthorizationStore,
        engine: Engine | None = None,
        *,
        agent_directory: Path | None = None,
        owner: str,
        device: str,
        client: str | None = None,
        allowed_tools: frozenset[str] | None = None,
        device_directory: Path | None = None,
        subchat_gateway: SubchatGateway | None = None,
    ) -> None:
        if (engine is None) == (agent_directory is None):
            raise ValueError("Select exactly one embedded engine or shared agent directory")
        self.store = store
        self.engine = engine
        self.agent_directory = agent_directory
        self.owner = owner
        self.device = device
        self.client, self.allowed_tools = client, allowed_tools
        self.device_directory = device_directory
        self.subchat_gateway = subchat_gateway

    def _matches(self, grant: GrantIdentity | None) -> bool:
        return (
            grant is not None
            and grant.owner == self.owner
            and grant.device == self.device
            and (self.client is None or grant.client == self.client)
            and (self.allowed_tools is None or grant.tools <= self.allowed_tools)
        )

    async def authenticate(self, token: str) -> str | None:
        grant = self.store.verify(token, resource=self.store.resource)
        return grant.grant_id if grant is not None and self._matches(grant) else None

    def session(self, grant_id: str) -> MCPSession:
        def current() -> GrantIdentity:
            grant = self.store.current_grant(grant_id)
            if grant is None or not self._matches(grant):
                raise ValueError("Device authorization is no longer available")
            return grant

        async def local_catalog() -> list[JsonValue]:
            grant = current()
            if self.engine is not None:
                return self.engine.catalog(grant.tools - ROUTER_TOOLS - SUBCHAT_GATEWAY_TOOLS)
            if self.agent_directory is None:
                raise RuntimeError("No engine was configured")
            # Retained HTTP sessions must recover before MCP's pre-dispatch catalog
            # check. Reuse the selected agent without replacing live work. The grant
            # is checked again by local_execute after waiting for startup.
            await asyncio.to_thread(ensure_agent, self.agent_directory, replace_idle=False)
            reply = await local_execute(Request(operation_id=uuid.uuid4().hex, tool="__catalog"))
            tools = reply.data.get("tools")
            if reply.state != "completed" or not isinstance(tools, list):
                raise ConnectionError("Shared engine catalog is unavailable")
            return [item for item in tools if isinstance(item, dict)
                    and item.get("name") not in SUBCHAT_GATEWAY_TOOLS]

        async def local_execute(request: Request) -> Reply:
            grant = current()
            if self.agent_directory is not None:
                return await exchange_remote(
                    self.agent_directory, grant.grant_id,
                    grant.tools - ROUTER_TOOLS - SUBCHAT_GATEWAY_TOOLS, request,
                    authorization_database=(self.store.database
                                            if request.tool == 'mcp_session_open' else None),
                )
            if self.engine is None:
                raise RuntimeError("No engine was configured")
            if request.tool == 'mcp_session_open':
                self.engine.bind_http_watch_grant(grant.grant_id, self.store.database)
            # Reuse the peer namespace and lookup checks; grants are reloaded per dispatch.
            bridge = RemoteAgent(
                self.engine, {grant.grant_id: grant.tools - ROUTER_TOOLS
                                                - SUBCHAT_GATEWAY_TOOLS}, transport="http",
            )
            return Reply.model_validate_json(
                await bridge.dispatch(grant.grant_id, request.model_dump_json().encode())
            )

        async def catalog() -> list[JsonValue]:
            granted = current().tools
            subchat = (await self.subchat_gateway.catalog(current().grant_id, granted)
                       if self.subchat_gateway is not None else [])
            if self.device_directory is None or not granted & ROUTER_TOOLS:
                return [*await local_catalog(), *subchat]
            router = DeviceRouter(self.device_directory, local_catalog, local_execute)
            try:
                return [item for item in await router.catalog()
                        if isinstance(item, dict) and item.get("name") in granted] + subchat
            finally:
                router.close()

        async def execute(request: Request) -> Reply:
            grant = current()
            if request.tool in SUBCHAT_GATEWAY_TOOLS:
                if self.subchat_gateway is None or grant.owner != self.subchat_gateway.owner:
                    return Reply(operation_id=request.operation_id, state="failed",
                                 error="Subchat gateway is unavailable")
                return await self.subchat_gateway.execute(grant.grant_id, request, grant.tools)
            if request.tool not in ROUTER_TOOLS:
                return await local_execute(request)
            if self.device_directory is None or request.tool not in grant.tools:
                return Reply(operation_id=request.operation_id, state="failed",
                             error="Device routing is not granted to this connection")
            # Saved target credentials can be shared by multiple HTTP grants. Keep their
            # routed operation IDs separate even when the target uses one credential.
            namespace = "device-route:" + grant.grant_id
            arguments = dict(request.arguments)
            nested = arguments.get("arguments")
            if (request.tool == "devices_call" and isinstance(nested, dict)
                    and "request_id" in nested):
                return Reply(operation_id=request.operation_id, state="failed",
                             data={"dispatched": False}, error=NESTED_REQUEST_ID_ERROR)
            lookup = None
            try:
                if request.tool == "devices_call" and arguments.get("tool") == "operations_get":
                    lookup = OperationId.model_validate(arguments.get("arguments")).operation_id
                    arguments["arguments"] = {
                        "operation_id": RemoteAgent.internal_id(namespace, lookup),
                    }
                forwarded = Request(
                    operation_id=RemoteAgent.internal_id(namespace, request.operation_id),
                    tool=request.tool, arguments=arguments,
                )
            except ValueError:
                return Reply(operation_id=request.operation_id, state="failed",
                             error="Invalid routed operation lookup")
            router = DeviceRouter(self.device_directory, local_catalog, local_execute)
            try:
                result = await router.execute(forwarded)
            finally:
                router.close()
            data = dict(result.data)
            recovered = data.get("result")
            if lookup is not None and isinstance(recovered, dict) and "operation_id" in recovered:
                data["result"] = {**recovered, "operation_id": lookup}
            return result.model_copy(update={"operation_id": request.operation_id, "data": data})

        instructions = MCP_INSTRUCTIONS
        if self.subchat_gateway is not None:
            instructions += (
                " For Subchat, choose the request_id before subchat_send or "
                "subchat_message. Save that exact operation ID. A running or submitted "
                "reply is not a final answer. Use subchat_status, subchat_recover, "
                "or subchat_wait with the saved ID. Never resend an uncertain input. "
                "The selected Chrome profile supplies browser preparation; generation "
                "uses browser-prepared HTTPX, not independent provider authorization."
            )
        session_type = SubchatHTTPSession if self.subchat_gateway is not None else MCPSession
        return session_type(catalog, execute, instructions=instructions)
