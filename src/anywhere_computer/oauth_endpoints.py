"""OAuth discovery and public-client code redemption over the loopback HTTP adapter.

The authorization URL must be supplied by an embedding application with a real
login/consent flow. This module does not implement that flow or enable a listener.
"""

import re
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from pydantic import JsonValue

from .authorization import AuthorizationError, AuthorizationStore, validate_authorization_url
from .http_mcp import HTTPResult, HTTPRoute


class OAuthEndpoints:
    def __init__(
        self, store: AuthorizationStore, *, authorization_endpoint: str,
        authorization_response_iss_supported: bool = False,
    ) -> None:
        validate_authorization_url(authorization_endpoint)
        resource = urlsplit(store.resource)
        if resource.path != "/mcp":
            raise ValueError("This HTTP adapter requires a canonical /mcp resource path")
        self.store = store
        self.issuer = urlunsplit((resource.scheme, resource.netloc, "", "", ""))
        self.authorization_endpoint = authorization_endpoint
        # External embeddings must opt in only when their consent responses
        # actually return the same issuer on both success and denial.
        self.authorization_response_iss_supported = authorization_response_iss_supported
        self.metadata_url = self.issuer + "/.well-known/oauth-protected-resource"

    @property
    def challenge(self) -> str:
        return f'Bearer resource_metadata="{self.metadata_url}"'

    def routes(self) -> dict[str, HTTPRoute]:
        return {
            "/.well-known/oauth-protected-resource": self.resource_metadata,
            "/.well-known/oauth-protected-resource/mcp": self.resource_metadata,
            "/.well-known/oauth-authorization-server": self.server_metadata,
            "/oauth/token": self.token,
        }

    async def resource_metadata(
        self, method: str, headers: dict[str, str], body: bytes, query: str = ""
    ) -> HTTPResult:
        if method != "GET":
            return 405, None, {"Allow": "GET"}
        return (
            200,
            {
                "resource": self.store.resource,
                "authorization_servers": [self.issuer],
                "scopes_supported": list(sorted(self.store.known_tools)),
                "bearer_methods_supported": ["header"],
            },
            {},
        )

    async def server_metadata(
        self, method: str, headers: dict[str, str], body: bytes, query: str = ""
    ) -> HTTPResult:
        if method != "GET":
            return 405, None, {"Allow": "GET"}
        return (
            200,
            {
                "issuer": self.issuer,
                "authorization_endpoint": self.authorization_endpoint,
                "token_endpoint": self.issuer + "/oauth/token",
                "response_types_supported": ["code"],
                "grant_types_supported": ["authorization_code", "refresh_token"],
                "token_endpoint_auth_methods_supported": ["none"],
                "code_challenge_methods_supported": ["S256"],
                "authorization_response_iss_parameter_supported": (
                    self.authorization_response_iss_supported
                ),
                "scopes_supported": list(sorted(self.store.known_tools)),
            },
            {},
        )

    async def token(
        self, method: str, headers: dict[str, str], body: bytes, query: str = ""
    ) -> HTTPResult:
        no_cache = {"Cache-Control": "no-store", "Pragma": "no-cache"}

        def error(code: str, status: int = 400) -> HTTPResult:
            return status, {"error": code}, no_cache

        if method != "POST":
            return 405, None, {**no_cache, "Allow": "POST"}
        if query:
            return error("invalid_request")
        if len(body) > 16384:
            return error("invalid_request", 413)
        content_type = headers.get("content-type", "").split(";")[0].strip().lower()
        if content_type != "application/x-www-form-urlencoded":
            return error("invalid_request", 415)
        if "authorization" in headers:
            # Only registered public clients with token_endpoint_auth_method=none are supported.
            return error("invalid_request")
        try:
            text = body.decode("utf-8")
            if re.search(r"%(?![A-Fa-f0-9]{2})", text):
                return error("invalid_request")
            pairs = parse_qsl(
                text,
                keep_blank_values=True,
                strict_parsing=True,
                max_num_fields=20,
                encoding="utf-8",
                errors="strict",
            )
        except (ValueError, UnicodeError):
            return error("invalid_request")
        params = dict(pairs)
        if len(params) != len(pairs) or not params.get("grant_type"):
            return error("invalid_request")
        if params["grant_type"] not in {"authorization_code", "refresh_token"}:
            return error("unsupported_grant_type")
        required = {"client_id", "resource"}
        if params["grant_type"] == "authorization_code":
            required |= {"code", "code_verifier", "redirect_uri"}
        else:
            required.add("refresh_token")
        if not all(params.get(key) for key in required):
            return error("invalid_request")
        if "client_secret" in params:
            return error("invalid_client")
        if params["resource"] != self.store.resource:
            return error("invalid_target")
        try:
            if params["grant_type"] == "authorization_code":
                issued = self.store.exchange_code(
                    code=params["code"],
                    verifier=params["code_verifier"],
                    client=params["client_id"],
                    redirect=params["redirect_uri"],
                    resource=params["resource"],
                )
            else:
                issued = self.store.refresh(
                    refresh_token=params["refresh_token"],
                    client=params["client_id"],
                    resource=params["resource"],
                    scope=frozenset(params["scope"].split()) if "scope" in params else None,
                )
        except AuthorizationError as failure:
            return error("invalid_scope" if str(failure) == "invalid_scope" else "invalid_grant")
        result: dict[str, JsonValue] = {
            "access_token": issued.value,
            "token_type": "Bearer",
            "expires_in": issued.expires_in,
            "scope": issued.scope,
            "refresh_token": issued.refresh_value,
        }
        return 200, result, no_cache
