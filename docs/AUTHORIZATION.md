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
15 minutes. A grant lasts at most 24 hours. Rotating refresh tokens can renew
access within that original deadline; renewal never extends the consent period.
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
request throttling and data retention. The internal client credential manager
now supports renewal and persistence; wiring it into browser onboarding and the
production remote connector is still outstanding. Public clients are the only client
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
  include no-store/no-cache headers. The refresh_token grant is supported;
  confidential-client authentication is not advertised.

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

## Refresh rotation and connection continuity

Code redemption now returns an access_token and refresh_token. The latter is
32 random bytes before encoding; only its digest is stored. The token endpoint
accepts grant_type=refresh_token with refresh_token, client_id and the exact
resource URI. A valid exchange consumes the refresh token and atomically stores
its replacement plus a new access token. The grant ID remains stable, so an
existing MCP session can continue using the renewed access token.

Every renewal rechecks the current grant, device and tool permissions. Tokens
cannot outlive the original 24-hour grant deadline. Access tokens issued near
that deadline have a shorter expires_in. A valid reuse of an already consumed
refresh token revokes the entire grant, including all derived access and refresh
tokens. Concurrent renewals through different SQLite connections therefore issue
at most one pair, then invalidate that grant when reuse is detected. Clients
must serialize refreshes and must not blindly retry a refresh after losing its
response; renewed credentials need secure, atomic client-side storage. The internal
client manager below implements this storage and serialization workflow.

Permissions are fixed for the lifetime of this grant. An omitted refresh scope
uses the current grant's full tool set; an explicitly identical set is accepted.
Any different set (including a reduction) returns invalid_scope without consuming
the refresh token. Changing permissions requires fresh consent; this is an
explicit policy restriction, not support for general OAuth scope reduction.
Refreshes never expand permissions. Ordinary still-valid access tokens remain
valid until their expiry unless the grant/device is revoked.

Schema version 2 adds the refresh-token table transactionally. Existing version 1
access tokens and grants remain usable; they do not acquire refresh tokens until
a new authorization code is redeemed. Tests cover expiry/near-deadline renewal,
secret-free persistence, concurrent rotation/reuse, migration and continuing the
same HTTP MCP session after an expired access token is renewed.

Reference: [OAuth security BCP, refresh token protection](https://www.rfc-editor.org/rfc/rfc9700.html#section-4.14).

## Client credential storage and renewal

`client_tokens.py` implements the internal client manager using the existing OS
credential service. Its keyring account is separated from the local agent's
credential and binds the state directory, canonical HTTPS resource, public client
ID and a connection profile. Distinct device/account profiles do not overwrite
each other's credentials. Access and refresh tokens, scope, expiry and update
phase are saved together as a single versioned keyring value. State files contain
only empty lock files, not tokens. No runtime dependency was added.

For every new request, `ClientTokens.access_token()` acquires an OS process lock,
then reloads the saved pair. Near expiry it saves `refresh_pending` **before**
sending the refresh. A successful response must contain fresh access and refresh
tokens, the same scope and a valid bounded lifetime; the complete replacement is
saved before the access token is returned. Expiry is measured from the start of
the request, so network delay does not extend the token's locally assumed life.
The early-renewal margin is 60 seconds or 10% of the token lifetime, whichever is
shorter. This avoids immediately re-renewing a short token near the grant deadline.

An interrupted process, lost response, invalid rotation response or failed final
save leaves a pending state. The next process refuses to resend the old refresh
token and reports that authorization is required. If the final keyring write
actually succeeded before the interruption, the next process instead reads and
uses that ready replacement. A failure to save the initial intent dispatches no
request and reports a credential-store error, distinct from missing authorization.
`install()` replaces the state after fresh user authorization; `forget()` deletes
only the local credential, not the server grant. These APIs do not expose secrets
through CLI arguments, files or diagnostic representations.

The default refresh transport uses standard-library HTTPS with certificate and
hostname checks, a 16 KiB response bound, no redirect following and no automatic
retry. It supports this server's colocated issuer at `/oauth/token`, `/mcp`
resource, public clients and 900-second maximum access lifetime. General OAuth
providers and discovery-selected external issuers are not supported by this
internal adapter. Token renewal never replays an MCP operation. A production
HTTP connector, browser login, and handling a revoked token's MCP 401 remain
integration work; the local stdio plugin does not yet invoke this manager.

Tests cover separate-process contention, process death during refresh, response
loss, save failures before and after dispatch, profile separation, real server
grant continuity, and real TLS response validation (including hostname rejection
before sending credentials). Test vaults hold synthetic tokens only. The explicit
internet probe additionally uses the native macOS Keychain for its disposable
pair, reopens the client after renewal, and removes the entry on completion.
