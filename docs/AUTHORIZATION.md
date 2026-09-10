# Device authorization implementation boundary

`authorization.py` and `authorized_http.py` provide the internal authorization
store and the binding between verified HTTP tokens and one device's MCP tools.
OAuth discovery and token redemption HTTP endpoints are implemented in
`oauth_endpoints.py`. Browser owner-password verification and explicit consent
are implemented as embedding routes; production service setup is not yet provided.
There is no authentication bypass
or anonymous mode in the HTTP binding.

## What is implemented

An embedding application registers public clients with exact HTTPS callback URLs
(or explicit IP-loopback callbacks for native clients, with only the port allowed
to vary under RFC 8252), and enrolls a device
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
15 minutes. New grants and rotating refresh tokens have no time deadline;
access can be renewed until consent is revoked or device/tool permissions change.
Legacy finite grants retain their deadlines unless explicitly migrated locally
with `http-retain-grants --state-dir <profile>`. This migrates only still-valid
grants for the configured owner/device/client, without reviving expired tokens.
An expires value of zero denotes no deadline only for grants and refresh tokens;
authorization codes and access tokens always retain their short lifetimes.
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

Before public use, implement the production client registration/authentication
policy, stable HTTPS hosting, deployment-level abuse controls and data retention.
The internal client credential manager
now supports renewal and persistence, and the HTTP connector uses it. Browser
onboarding and production deployment are still outstanding. Public clients are the only client
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
cannot outlive a finite legacy grant deadline. Access tokens issued near
such a deadline have a shorter expires_in; permanent grants issue 900-second tokens. A valid reuse of an already consumed
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
internal adapter. Token renewal itself never replays an MCP operation. The
`http-mcp` connector now invokes the manager and handles an explicit HTTP 401 by
renewing the exact rejected access token once. A late rejection for an already
replaced token uses the current pair instead of rotating again. Browser login and
production deployment remain integration work; the normal local `mcp` command
continues to use local agent credentials.

Tests cover separate-process contention, process death during refresh, response
loss, save failures before and after dispatch, profile separation, real server
grant continuity, and real TLS response validation (including hostname rejection
before sending credentials). Test vaults hold synthetic tokens only. The explicit
internet probe additionally uses the native macOS Keychain for its disposable
pair, reopens the client after renewal, and removes the entry on completion.

## Browser owner authentication and consent

`BrowserAuthorization` exposes GET/POST `/authorize` on the resource's HTTPS
origin. Build discovery with its `authorization_endpoint` property, mount its
routes alongside `OAuthEndpoints.routes()`, and allow that exact origin in
`HTTPMCP.origins`. This adapter supports colocated authorization only; an external
authorization origin or a different authorize path is not supported. The generic
OAuth endpoint class can still be embedded with a different authentication provider.

Trusted initial setup uses `anywhere owner-init --resource https://HOST/mcp
--owner OWNER` from an interactive terminal. The command asks for the password
twice without echo; it accepts neither password arguments nor piped input. The
password must contain at least 16 characters. Only a random salt and scrypt
verification digest are saved in the native OS credential store. Existing owner
credentials cannot be overwritten by initialization. `anywhere owner-change` with
the same resource, owner and state directory requests the current password and
the replacement twice through hidden terminal input. It authenticates under the
same resolved-path writer lock used by setup, verification and deletion, writes
one new salted verifier, then reads it back to confirm the outcome. If the old
record remains, the old password is preserved. If readback cannot establish the
outcome, it reports an unknown result without retrying or rolling back the write.
It does not revoke issued grants or undo already completed password checks; use
`http-revoke` for existing connection revocation. Forgotten-password recovery is
not implemented. See [HTTP service setup and password changes](HTTP-SERVER.md)
for the available persistent service commands. Deleting a verifier alone does
not revoke already issued grants.

The form displays the registered client, exact callback, device, resource and
requested tools. Each approval requires the owner password. Pending requests
expire after five minutes and are lost on restart. A per-request Secure,
HttpOnly, SameSite=Strict `__Host-` cookie, random CSRF value and exact POST Origin
bind the decision to its browser. Only digests of the cookie and CSRF values are
retained. Invalid requests never redirect to an unverified callback. Concurrent
approve/deny submissions consume a request only once; current device/client/tool
bindings are checked again inside the grant transaction. Denial returns the
original state and `access_denied` without requiring a password.

HTML has no external assets or scripts and uses no-store, a restrictive CSP and
no-referrer headers. Wrong passwords are never echoed. Each pending request has
a ten-attempt/60-second limit; failed attempts do not lock other pending requests.
Only two actual password workers can run at once, and excess work returns busy
instead of queueing. At most 64 pending requests are retained. These are bounded
resource protections, not complete public abuse prevention: attackers who can
start new requests can bypass a per-request rate limit or exhaust capacity.
Production edge/account throttling and recovery policy still need implementation.

Tests cover correct/wrong passwords, native-verifier format and secret-free state,
interactive CLI setup, Origin/cookie/CSRF mismatch, concurrent approval, denial,
revocation between form and decision, isolated attempt budgets and busy workers.
The disposable internet probe submits the real HTML form over public HTTPS using
a synthetic owner password in macOS Keychain, then exchanges the code, operates
on its single disposable file and verifies revocation and cleanup. It is an HTTP
form integration test, not visual browser testing, ChatGPT onboarding or a second
physical device test. See the dated browser-authorization receipt in `research/`.

## Native client login

`anywhere login --resource https://HOST/mcp --client-id CLIENT --profile PROFILE
--scope files_read` opens the external system browser, obtains approval, redeems
one authorization code and saves the validated pair in the existing native-keyring
profile. Repeat `--scope` for each tool to request. It uses this product's colocated
`/authorize` and `/oauth/token` endpoints; discovery-selected external providers,
client auto-registration and a complete server provisioning wizard remain work.
The server's client registration and device enrollment APIs remain trusted only.

Register `http://127.0.0.1/oauth/callback` and `http://[::1]/oauth/callback` for this
native client. The client binds an ephemeral IPv4 loopback port before opening
the browser, falling back to IPv6 loopback if IPv4 cannot bind. Ordinary HTTPS
redirects remain exact matches. For registered HTTP IP-loopback callbacks only,
registration matching allows a different port; the literal IP, path and query
remain exact. Code redemption must still supply the exact full redirect used
when the code was issued, including that chosen port.

Each login has fresh in-memory state and an S256 PKCE verifier. The verifier is
not included in the browser URL. The temporary callback requires GET, its exact
Host/port/path, a matching state, no duplicate query fields and exactly one code
or error. An optional issuer must match the configured origin. It rejects bodies,
transfer encoding, duplicate headers and Origin-bearing cross-origin requests.
The listener accepts at most eight active connections, reads at most 8 KiB of
headers with a five-second connection deadline, and closes on success, denial,
failure, cancellation or the five-minute login deadline. Callback HTML never
contains codes, tokens or provider error descriptions and disables caching,
referrers and external resources. The page confirms only receipt; the terminal
reports success after credentials have actually been saved.

Token redemption uses standard-library HTTPS with CA/hostname checks, no redirects,
no retry, JSON validation and a 16 KiB response bound. Received permissions must
exactly match those requested. A lost exchange response requires a fresh login;
an old code is never automatically resent. Failed login leaves existing saved
credentials unchanged. Concurrent explicit logins into one profile use the
existing atomic keyring save; the last completed login becomes that profile's
credential. Outstanding server grants require separate revocation.

Tests exercise real loopback callback sockets, wrong state/Host/path/issuer,
duplicated fields, denial, browser failure/timeout, dynamic-port matching and a
real TLS token endpoint including rejection before secrets are sent to the wrong
hostname. The public probe now drives this native client with an HTTP form driver
standing in for a human browser, uses the actual callback and macOS Keychain,
then continues through HTTP MCP operations and revocation. It does not prove
browser rendering or actual ChatGPT UI onboarding. The dated native-login receipt
records the tested runtime and cleanup.

Reference: [RFC 8252, loopback redirects and native client registration](https://www.rfc-editor.org/rfc/rfc8252).
