"""Bind verified HTTP grants to one owner's device and its current tool permissions."""

import uuid
from pathlib import Path

from pydantic import JsonValue

from .authorization import AuthorizationStore, GrantIdentity
from .connection import exchange_remote
from .device_router import ROUTER_TOOLS, DeviceRouter
from .engine import Engine
from .mcp_server import MCPSession
from .models import OperationId, Reply, Request
from .remote_bridge import RemoteAgent


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
            if self.engine is not None:
                return self.engine.catalog(current().tools - ROUTER_TOOLS)
            reply = await local_execute(Request(operation_id=uuid.uuid4().hex, tool="__catalog"))
            tools = reply.data.get("tools")
            if reply.state != "completed" or not isinstance(tools, list):
                raise ConnectionError("Shared engine catalog is unavailable")
            return tools

        async def local_execute(request: Request) -> Reply:
            grant = current()
            if self.agent_directory is not None:
                return await exchange_remote(
                    self.agent_directory, grant.grant_id, grant.tools - ROUTER_TOOLS, request,
                )
            if self.engine is None:
                raise RuntimeError("No engine was configured")
            # Reuse the peer namespace and lookup checks; grants are reloaded per dispatch.
            bridge = RemoteAgent(
                self.engine, {grant.grant_id: grant.tools - ROUTER_TOOLS}, transport="http",
            )
            return Reply.model_validate_json(
                await bridge.dispatch(grant.grant_id, request.model_dump_json().encode())
            )

        async def catalog() -> list[JsonValue]:
            granted = current().tools
            if self.device_directory is None or not granted & ROUTER_TOOLS:
                return await local_catalog()
            router = DeviceRouter(self.device_directory, local_catalog, local_execute)
            try:
                return [item for item in await router.catalog()
                        if isinstance(item, dict) and item.get("name") in granted]
            finally:
                router.close()

        async def execute(request: Request) -> Reply:
            grant = current()
            if request.tool not in ROUTER_TOOLS:
                return await local_execute(request)
            if self.device_directory is None or request.tool not in grant.tools:
                return Reply(operation_id=request.operation_id, state="failed",
                             error="Device routing is not granted to this connection")
            # Saved target credentials can be shared by multiple HTTP grants. Keep their
            # routed operation IDs separate even when the target uses one credential.
            namespace = "device-route:" + grant.grant_id
            arguments = dict(request.arguments)
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

        return MCPSession(catalog, execute)
