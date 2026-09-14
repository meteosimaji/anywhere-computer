"""Isolated AI execution grants, independently verifiable by relay and PC."""

import hashlib
import json
from collections.abc import Callable

from pydantic import BaseModel, ConfigDict, Field

from .models import Reply, Request
from .relay_registry import RelayAccount
from .relay_tokens import SignedRelayToken
from .remote_bridge import RemoteAgent
from .remote_transport import FRAME_LIMIT


class ExecutionRejected(ValueError):
    pass


class ExecutionClaims(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, hide_input_in_errors=True)
    iss: str
    sub: str = Field(min_length=1, max_length=255, pattern=r'^[\x21-\x7e]+$')
    aud: str
    azp: str
    typ: str
    exp: int
    iat: int
    scope: str
    device_id: str = Field(pattern=r'^[a-f0-9]{32}$')
    grant_id: str = Field(pattern=r'^[a-f0-9]{32}$')
    tools: list[str] = Field(min_length=1, max_length=256)

    @property
    def account(self) -> RelayAccount:
        return RelayAccount(issuer=self.iss, subject=self.sub)

    @property
    def identity(self) -> str:
        # Refresh preserves the ledger namespace only for the same issued grant.
        binding = [self.iss, self.sub, self.azp, self.device_id, self.grant_id]
        return hashlib.sha256(json.dumps(binding, separators=(',', ':')).encode()).hexdigest()


class ExecutionVerifier:
    def __init__(self, tokens: SignedRelayToken, *,
                 is_current: Callable[[ExecutionClaims], bool]) -> None:
        self.tokens = tokens
        self.is_current = is_current

    def verify(self, token: str, *, account: RelayAccount, device_id: str,
               tool: str) -> ExecutionClaims:
        """is_current must consult trusted revocation state, never caller claims."""
        try:
            claims = ExecutionClaims.model_validate(self.tokens.decode(token))
            if (claims.azp != self.tokens.client or claims.typ != 'Bearer'
                    or claims.scope != 'device:execute' or not 0 < claims.exp - claims.iat <= 900
                    or claims.account != account or claims.device_id != device_id
                    or len(set(claims.tools)) != len(claims.tools)
                    or any(not name or len(name) > 128 for name in claims.tools)
                    or (tool != '__catalog' and tool not in claims.tools)
                    or not self.is_current(claims)):
                raise ValueError('Execution grant rejected')
            return claims
        except (ValueError, TypeError):
            raise ExecutionRejected('Execution authorization was rejected') from None


class ExecutionEnvelope(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, hide_input_in_errors=True)
    token: str = Field(min_length=1, max_length=16384, repr=False)
    request: Request


class AuthorizedRelayAgent:
    """PC boundary; ownership is fixed by trusted provisioning, not a frame.

    Transport framing/provisioning and an actual revocation provider remain
    separate. Never put bearer tokens inside engine arguments or its ledger.
    """

    def __init__(self, agent: RemoteAgent, verifier: ExecutionVerifier, *,
                 account: RelayAccount, device_id: str) -> None:
        self.agent, self.verifier = agent, verifier
        self.account, self.device_id = account, device_id

    async def dispatch_frame(self, payload: bytes) -> bytes:
        # Reject malformed frames without inventing an operation ID or echoing
        # their input. Transport callers close the channel on this exception.
        try:
            if len(payload) > FRAME_LIMIT:
                raise ValueError('Frame too large')
            frame = ExecutionEnvelope.model_validate_json(payload)
        except ValueError:
            raise ExecutionRejected('Invalid execution frame') from None
        reply = await self.dispatch(frame.token, frame.request)
        return reply.model_dump_json().encode()

    async def dispatch(self, token: str, request: Request) -> Reply:
        try:
            grant = self.verifier.verify(token, account=self.account, device_id=self.device_id,
                                         tool=request.tool)
            self.agent.grant(grant.identity, frozenset(grant.tools), expires_at=grant.exp)
        except ValueError:
            return Reply(operation_id=request.operation_id, state='failed',
                         error='Execution authorization was rejected before dispatch')
        return Reply.model_validate_json(await self.agent.dispatch(
            grant.identity, request.model_dump_json().encode(),
        ))
