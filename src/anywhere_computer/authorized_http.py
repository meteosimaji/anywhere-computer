"""Bind verified HTTP grants to one owner's device and its current tool permissions."""

import uuid
from pathlib import Path

from pydantic import JsonValue

from .authorization import AuthorizationStore, GrantIdentity
from .connection import exchange_remote
from .engine import Engine
from .mcp_server import MCPSession
from .models import Reply, Request
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
    ) -> None:
        if (engine is None) == (agent_directory is None):
            raise ValueError("Select exactly one embedded engine or shared agent directory")
        self.store = store
        self.engine = engine
        self.agent_directory = agent_directory
        self.owner = owner
        self.device = device
        self.client, self.allowed_tools = client, allowed_tools

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

        async def catalog() -> list[JsonValue]:
            if self.engine is not None:
                return self.engine.catalog(current().tools)
            reply = await execute(Request(operation_id=uuid.uuid4().hex, tool="__catalog"))
            tools = reply.data.get("tools")
            if reply.state != "completed" or not isinstance(tools, list):
                raise ConnectionError("Shared engine catalog is unavailable")
            return tools

        async def execute(request: Request) -> Reply:
            grant = current()
            if self.agent_directory is not None:
                return await exchange_remote(
                    self.agent_directory, grant.grant_id, grant.tools, request,
                )
            if self.engine is None:
                raise RuntimeError("No engine was configured")
            # Reuse the peer namespace and lookup checks; grants are reloaded per dispatch.
            bridge = RemoteAgent(self.engine, {grant.grant_id: grant.tools}, transport="http")
            return Reply.model_validate_json(
                await bridge.dispatch(grant.grant_id, request.model_dump_json().encode())
            )

        return MCPSession(catalog, execute)
