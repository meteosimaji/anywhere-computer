# Device authorization implementation boundary

`authorization.py` and `authorized_http.py` provide the internal authorization
store and the binding between verified HTTP tokens and one device's MCP tools.
OAuth discovery and token redemption HTTP endpoints are implemented in
`oauth_endpoints.py`. A browser login, consent page and production service are
not yet provided. There is no authentication bypass
or anonymous mode in the HTTP binding.

## What is implemented

An embedding application registers public clients with exact HTTPS callback URLs
(or explicit IP-loopback callbacks for native clients), and enrolls a device
under its owner's stable identifier with a set of allowed tool names. Registration
and enrollment are trusted administrative APIs, not unauthenticated HTTP handlers.
The embedding application must verify the owner and obtain consent for the
selected device, client and tools **before** calling `approve`.

Approval creates a short-lived authorization code bound to the client, exact
callback, one canonical HTTPS resource, the selected device, tool set and an
S256 PKCE challenge. The verifier is checked against RFC 7636's syntax and S256
transformation; plain challenges are not supported. Codes expire after 120
seconds. Redemption and token creation use one SQLite write transaction, so
parallel redemptions cannot both issue a token. A valid replay is rejected and
revokes the original grant and its tokens. Invalid verifier/client/callback
attempts never issue a token and do not consume a still-valid code.

Opaque access tokens contain 32 random bytes before URL-safe encoding and last
15 minutes. A grant lasts at most 24 hours; refresh tokens are not implemented.
Only SHA-256 digests of codes and tokens are stored in SQLite. Raw codes, tokens
and verifiers are not persisted. AccessToken's repr excludes the token value.
The caller must also avoid logging response bodies or serializing secrets to
configuration. The resource URL is bound to the database and cannot silently
change when it is reopened.

Every HTTP request verifies its token against the database and its resource.
`AuthorizedDeviceMCP` also checks the owner and device identity before creating a
session. The catalog exposes only granted tools. Execution checks current grants
again and reuses the existing remote operation namespace/lookup logic. A grant
cannot look up another grant's operation IDs. Revoking a grant or its device
rejects future requests, including those with existing MCP sessions. Revocation
does not undo a previously dispatched action. Token expiry is checked at request
authentication; work already dispatched is allowed to finish.

Tool permission sets are not filesystem sandboxes: `files_read` can read paths
accessible to that device's OS account, and terminal permissions grant the
corresponding shell capability. All clients attached to an engine operate for
that device owner. Different owners must be routed to separate authorized
engines; the binding rejects an owner or device mismatch. Filesystem/path-level
policy and process/session isolation are still separate implementation tasks.

## Evidence and remaining work

Tests cover the RFC 7636 Appendix B vector, exact binding failures, expiration,
parallel redemption through separate SQLite connections, replay revocation,
reopening the store, absence of raw secrets in stored files, and owner checks.
A real HTTP integration exercises a token for the wrong device, a read grant
that cannot write, an approved write grant, operation lookup across grants, and
immediate grant/device revocation. These are local integration tests, not an
internet OAuth login or a ChatGPT account connection.

Before public use, implement authenticated login and consent with request/CSRF
binding, client registration/authentication policy, HTTPS,
request throttling and data retention. Reconnect renewal and refresh-token
rotation/reuse policy are also outstanding. Public clients are the only client
type in this internal store; it must not advertise confidential-client support.
No public listener is enabled by this change.

References:
- [RFC 7636: PKCE](https://www.rfc-editor.org/rfc/rfc7636.html)
- [RFC 6749: authorization code grant](https://www.rfc-editor.org/rfc/rfc6749.html#section-4.1)
- [MCP authorization](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization)

## HTTP discovery and token exchange

An embedding application can mount `OAuthEndpoints.routes()` as `HTTPMCP.public_routes` and
set its `auth_challenge`. It provides:

- `GET /.well-known/oauth-protected-resource` and its `/mcp` variant, including
  the canonical resource URI and authorization server location.
- `GET /.well-known/oauth-authorization-server`, declaring S256, authorization
  code grants, registered public clients (`none`) and the token endpoint.
- `POST /oauth/token`, accepting UTF-8 form data with one each of grant_type,
  code, code_verifier, client_id, redirect_uri and resource. Duplicate/malformed
  parameters are rejected; unknown extension parameters are ignored. Responses
  include no-store/no-cache headers. No refresh grant or confidential-client
  authentication method is advertised.

The caller supplies an actual authorization URL; this module does not implement
that login/consent route or automatically redirect a user to an unverified URL.
The current endpoint configuration is a colocated issuer at the origin of a
canonical HTTPS `/mcp` resource. Local tests intentionally use example metadata
URLs while transporting requests over loopback; they do not prove public HTTPS
or automatic end-to-end client discovery over those example domains.

Tests validate both metadata documents against the official MCP SDK models,
redeem a code through a real HTTP socket, and initialize MCP with the resulting
token. They also verify replay revocation, wrong verifier/client/callback/resource,
duplicate fields, malformed percent encoding, unsupported methods/types and
body limits. Browser authorization, dynamic registration and public deployment
remain unimplemented.

Public extension routes bypass MCP bearer authentication by design: metadata
is public and the token route validates the code/PKCE contract. Never register
a privileged route there without its own authentication and request protection.
This adapter intentionally rejects resource URLs containing queries; RFC 8707
permits such resources, but the current deployment contract fixes `/mcp` without
a query.
