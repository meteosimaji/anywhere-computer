"""Authenticated enrollment boundary for an isolated relay.

Signing keys are supplied by trusted operator configuration, never token URLs.
No HTTP endpoint, issuer discovery, token issuance or PC transport is provided.
"""

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field

from .relay_registry import RelayAccount, RelayDevice, RelayRegistry
from .relay_tokens import SignedRelayToken


class EnrollmentRejected(ValueError):
    pass


class _EnrollmentClaims(BaseModel):
    model_config = ConfigDict(strict=True, hide_input_in_errors=True)
    iss: str
    sub: str = Field(min_length=1, max_length=255, pattern=r"^[\x21-\x7e]+$")
    aud: str
    azp: str
    typ: str
    exp: int
    iat: int
    scope: str


class RelayEnrollment:
    """Registration only. Enrollment tokens never authorize PC execution.

    The initial isolated profile accepts RS256 with explicitly configured RSA
    public keys and one exact audience/client/scope. Public service deployment
    still requires provider key rotation and revocation integration.
    """

    def __init__(self, registry: RelayRegistry, *, issuer: str, audience: str,
                 client: str, public_keys: Mapping[str, str]) -> None:
        self._registry = registry
        self._tokens = SignedRelayToken(issuer=issuer, audience=audience, client=client,
                                        public_keys=public_keys)

    def register(self, token: str, *, enrollment_id: str, name: str) -> RelayDevice:
        owner = self.account(token)
        return self._registry.register(owner, enrollment_id=enrollment_id, name=name)

    def account(self, token: str) -> RelayAccount:
        """Resolve only the authenticated enrollment identity, without mutation."""
        try:
            claims = _EnrollmentClaims.model_validate(self._tokens.decode(token))
            if (claims.azp != self._tokens.client or claims.typ != "Bearer"
                    or claims.scope != "device:enroll" or not 0 < claims.exp - claims.iat <= 900):
                raise ValueError("Unsupported enrollment grant")
            owner = RelayAccount(issuer=claims.iss, subject=claims.sub)
        except (ValueError, TypeError):
            raise EnrollmentRejected("Enrollment authorization was rejected") from None
        return owner
