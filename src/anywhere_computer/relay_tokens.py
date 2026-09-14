"""Shared signature boundary for isolated relay reference-provider profiles."""

from collections.abc import Mapping
from typing import Any

import jwt

from .authorization import validate_authorization_url


class SignedRelayToken:
    """Verify fixed issuer/audience/client and operator-supplied public keys.

    This is not an authorization server, discovery or revocation mechanism.
    Consumers must validate their distinct claims and authorization purpose.
    """

    def __init__(self, *, issuer: str, audience: str, client: str,
                 public_keys: Mapping[str, str]) -> None:
        validate_authorization_url(issuer)
        validate_authorization_url(audience)
        if not client or not public_keys or len(public_keys) > 16:
            raise ValueError('Invalid relay token configuration')
        self.client = client
        self.issuer, self.audience = issuer, audience
        self._keys = {}
        for key_id, pem in public_keys.items():
            if not key_id or len(key_id) > 128 or len(pem) > 16384:
                raise ValueError('Invalid relay token key')
            key = jwt.algorithms.RSAAlgorithm(jwt.algorithms.RSAAlgorithm.SHA256).prepare_key(pem)
            if hasattr(key, 'private_numbers') or key.key_size < 2048:
                raise ValueError('Relay requires RSA public keys of at least 2048 bits')
            self._keys[key_id] = key

    def decode(self, token: str) -> dict[str, Any]:
        try:
            if not token or len(token) > 16384:
                raise ValueError('Invalid token size')
            header = jwt.get_unverified_header(token)
            key_id = header.get('kid')
            if (header.get('alg') != 'RS256' or not isinstance(key_id, str)
                    or key_id not in self._keys
                    or any(k in header for k in ('jku', 'jwk', 'x5u', 'crit', 'b64'))):
                raise ValueError('Unsupported token header')
            return jwt.decode(
                token, self._keys[key_id], algorithms=['RS256'], issuer=self.issuer,
                audience=self.audience,
                options={'require': ['iss', 'sub', 'aud', 'azp', 'typ', 'exp', 'iat', 'scope'],
                         'strict_aud': True},
            )
        except (ValueError, jwt.PyJWTError, TypeError):
            raise ValueError('Relay token rejected') from None
