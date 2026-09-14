"""MCP session adapter for a trusted, request-scoped relay authorization context."""

import uuid
from collections.abc import Callable

from pydantic import JsonValue

from .http_mcp import HTTPMCP
from .mcp_server import MCPSession
from .models import Reply, Request
from .relay_channels import RelayChannels
from .relay_grants import ExecutionRejected, ExecutionVerifier
from .relay_registry import RelayAccount


class RelayMCPBackend:
    """One grant/device binding; fresh bearer supplied for each operation.

    The caller owns authentication and must isolate token providers per request.
    This is not a public HTTP endpoint or a source of granted permissions.
    """

    def __init__(self, channels: RelayChannels, verifier: ExecutionVerifier, *,
                 account: RelayAccount, device_id: str, token: Callable[[], str]) -> None:
        grant = verifier.verify(token(), account=account, device_id=device_id, tool='__catalog')
        self.channels, self.verifier = channels, verifier
        self.account, self.device_id, self.token = account, device_id, token
        self.identity = grant.identity

    async def execute(self, request: Request) -> Reply:
        token = self.token()
        grant = self.verifier.verify(token, account=self.account, device_id=self.device_id,
                                     tool=request.tool)
        if grant.identity != self.identity:
            raise ExecutionRejected('MCP session authorization binding changed')
        return await self.channels.exchange_authorized(
            self.verifier, self.account, self.device_id, token, request,
        )

    async def catalog(self) -> list[JsonValue]:
        reply = await self.execute(Request(operation_id=uuid.uuid4().hex, tool='__catalog'))
        tools = reply.data.get('tools')
        if reply.state != 'completed' or not isinstance(tools, list):
            raise ConnectionError('PC catalog is unavailable')
        return tools

    def mcp_session(self) -> MCPSession:
        return MCPSession(self.catalog, self.execute)


def relay_http_mcp(channels: RelayChannels, verifier: ExecutionVerifier, *,
                   account: RelayAccount, device_id: str) -> HTTPMCP:
    """Loopback HTTP entry for one provisioned device; no token issuer or public deployment."""
    async def authenticate(token: str) -> str | None:
        try:
            return verifier.verify(token, account=account, device_id=device_id,
                                   tool='__catalog').identity
        except ExecutionRejected:
            return None

    def session_factory(owner: str) -> MCPSession:
        backend = RelayMCPBackend(channels, verifier, account=account,
                                  device_id=device_id, token=adapter.current_bearer)
        if backend.identity != owner:
            raise ExecutionRejected('MCP session authorization binding changed')
        return backend.mcp_session()

    adapter = HTTPMCP(authenticate, session_factory)
    return adapter
