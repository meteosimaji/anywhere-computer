"""Bind verified HTTP grants to one owner's device and its current tool permissions."""

from pydantic import JsonValue

from .authorization import AuthorizationStore, GrantIdentity
from .engine import Engine
from .mcp_server import MCPSession
from .models import Reply, Request
from .remote_bridge import RemoteAgent


class AuthorizedDeviceMCP:
    def __init__(
        self, store: AuthorizationStore, engine: Engine, *, owner: str, device: str
    ) -> None:
        self.store = store
        self.engine = engine
        self.owner = owner
        self.device = device

    def _matches(self, grant: GrantIdentity | None) -> bool:
        return grant is not None and grant.owner == self.owner and grant.device == self.device

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
            return self.engine.catalog(current().tools)

        async def execute(request: Request) -> Reply:
            grant = current()
            # Reuse the peer namespace and lookup checks; grants are reloaded per dispatch.
            bridge = RemoteAgent(self.engine, {grant.grant_id: grant.tools}, transport="http")
            return Reply.model_validate_json(
                await bridge.dispatch(grant.grant_id, request.model_dump_json().encode())
            )

        return MCPSession(catalog, execute)
