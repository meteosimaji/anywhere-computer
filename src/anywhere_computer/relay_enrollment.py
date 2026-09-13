"""Authenticated enrollment boundary for an isolated relay.

Signing keys are supplied by trusted operator configuration, never token URLs.
No HTTP endpoint, issuer discovery, token issuance or PC transport is provided.
"""

from collections.abc import Mapping

import jwt
from pydantic import BaseModel, ConfigDict, Field

from .authorization import validate_authorization_url
from .relay_registry import RelayAccount, RelayDevice, RelayRegistry


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
        validate_authorization_url(issuer)
        validate_authorization_url(audience)
        if not client or not public_keys or len(public_keys) > 16:
            raise ValueError("Invalid relay enrollment configuration")
        self._registry = registry
        self._issuer, self._audience, self._client = issuer, audience, client
        self._keys = {}
        for key_id, pem in public_keys.items():
            if not key_id or len(key_id) > 128 or len(pem) > 16384:
                raise ValueError("Invalid relay enrollment key")
            key = jwt.algorithms.RSAAlgorithm(jwt.algorithms.RSAAlgorithm.SHA256).prepare_key(pem)
            if hasattr(key, "private_numbers") or key.key_size < 2048:
                raise ValueError("Enrollment requires RSA public keys of at least 2048 bits")
            self._keys[key_id] = key

    def register(self, token: str, *, enrollment_id: str, name: str) -> RelayDevice:
        owner = self.account(token)
        return self._registry.register(owner, enrollment_id=enrollment_id, name=name)

    def account(self, token: str) -> RelayAccount:
        """Resolve only the authenticated enrollment identity, without mutation."""
        try:
            if not token or len(token) > 16384:
                raise ValueError("Invalid enrollment token size")
            header = jwt.get_unverified_header(token)
            key_id = header.get("kid")
            if (header.get("alg") != "RS256" or not isinstance(key_id, str)
                    or key_id not in self._keys
                    or any(k in header for k in ("jku", "jwk", "x5u", "crit", "b64"))):
                raise ValueError("Unsupported enrollment token header")
            claims = _EnrollmentClaims.model_validate(jwt.decode(
                token, self._keys[key_id], algorithms=["RS256"], issuer=self._issuer,
                audience=self._audience,
                options={"require": ["iss", "sub", "aud", "azp", "typ", "exp", "iat", "scope"],
                         "strict_aud": True},
            ))
            if (claims.azp != self._client or claims.typ != "Bearer"
                    or claims.scope != "device:enroll" or not 0 < claims.exp - claims.iat <= 900):
                raise ValueError("Unsupported enrollment grant")
            owner = RelayAccount(issuer=claims.iss, subject=claims.sub)
        except (ValueError, jwt.PyJWTError, TypeError):
            raise EnrollmentRejected("Enrollment authorization was rejected") from None
        return owner
