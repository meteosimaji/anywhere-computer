# Isolated relay development

The relay is not deployed and no manager pairing workflow uses it yet. Local
MCP and personal SSH/HTTP connections remain unchanged.

## Device ownership storage

`relay_registry.py` is a separate SQLite store, not an extension of the personal
`devices.sqlite3` registry or its owner-only authorization service. Its caller
must first validate the account's issuer, subject, enrollment scope and token
audience using the selected authorization provider. Constructing `RelayAccount`
does not authenticate anyone. The storage class is not exposed as an MCP tool,
HTTP endpoint, or arbitrary manager command.

The account key is the exact issuer and subject pair. A registration retry uses
the original enrollment ID within that account and returns the same server-issued
device ID. A different name under the same enrollment ID is a conflict. Different
enrollment IDs may use the same display name; names never select an execution
target. The unique constraint and write transaction prevent concurrent retries
from allocating two devices.

Revocation retains a tombstone. Retrying the original enrollment does not restore
it to registered state. Cross-account lookup and revocation both fail without
changing the owner's record. `registered` means only that ownership was stored;
it does not mean transport connected, authentication current, or engine ready.

Stored data includes issuer, subject, display name, device ID, enrollment ID and
revocation state. There are no passwords, bearer tokens, tool arguments or results
in this database. Records currently persist until the isolated database is
removed. A production retention/deletion policy and account removal workflow are
still required before a public service. This is not end-to-end encryption.

Five storage tests cover reopen/retry, duplicate names, both subject and issuer
separation, revocation/replay, and concurrent registration. These prove the
storage boundary only. Separate PC transport credentials, outbound connections,
per-request tool authorization and result
recovery remain implementation work. The existing engine ledger remains the
intended source of execution results; this registry must not become a second
operation engine or an offline write queue.

## Enrollment authorization boundary

`relay_enrollment.py` now validates signed enrollment access tokens before
constructing the registry's account identity. Install the optional `relay` extra
for this module; the core agent does not import it or require a JWT library.
The extra pins the existing lockfile's PyJWT 2.13.0 with cryptography support
(MIT), rather than implementing signing or verification. No new inference,
Java runtime or authorization server is added to the PC agent.

The isolated reference profile uses configured RSA public keys (RS256, at least
2048 bits), an exact issuer, a single exact audience, a single client (`azp`),
the `Bearer` token type and exactly `device:enroll`. Subject, issued-at and expiry
are mandatory; timestamps must be integers, the token must be current and its
issued lifetime cannot exceed 15 minutes. Unsigned tokens, other keys, token
supplied key URLs and unsupported critical headers are rejected before storage.
Account identity is taken from verified claims, not request fields. A valid
registration grant does not authorize tool execution or a PC relay connection.

This is a deliberately explicit reference-provider profile, not a claim of
universal OAuth token compatibility. Other providers need an evaluated profile.
The service currently receives public keys from operator configuration: automatic
rotation, online grant revocation and rate limiting remain unfinished. Short
token expiry does not replace those requirements.

The real-provider runner's `--check-registration` option obtains public signing
keys from the fixed HTTPS loopback fixture issuer, then tests registration with
the actual issued token, idempotent retry and wrong-audience rejection with no
extra device record. It does not deploy a public endpoint or establish PC transport.
See [provider acceptance](ENROLLMENT-PROVIDER-TEST.md) for reproducible setup.

## Isolated HTTP registration

`enrollment_http(service)` in `relay_http.py` mounts `POST /enrollment/devices`
on a separate loopback adapter. It reuses the bounded HTTP framing from
`HTTPMCP`, not the personal owner's grants or engine. Every registration needs
an enrollment bearer token; `/mcp` always rejects and creates no session.
Browser origins are refused by default. There is no listener on a public network
interface, forwarding header trust, automatic CORS policy or deployment command.

The JSON body contains only `enrollment_id` (32 lowercase hex characters) and
`name` (the existing normalized device-name contract); caller-supplied account
fields are rejected. Bodies over 4096 bytes, query strings, unsupported methods
and media types are refused. Successful creation and replay both return HTTP 200
with device ID, enrollment ID, name and registration state. Conflicting names
for an existing enrollment return 409. Responses have `Cache-Control: no-store`;
failure bodies contain no token, claims, request text or database details.

The registry and adapter belong to one event-loop thread. This small loopback
implementation performs synchronous signature/storage work and is not a
production concurrency or rate-limiting design. The manager still needs the
registration client and persistent association; a registered device still cannot
execute operations until the separately authenticated outbound link is implemented.

The client-side `https_enrollment_registration` transport now sends JSON and a
bearer grant over verified HTTPS. It shares the form flow's bounded exchange,
duplicate-response-field rejection and no-redirect/no-retry behavior. Local TLS
tests cover Japanese/emoji names, bearer delivery, malformed-header rejection
before dispatch, and response loss after one request without replay. This is
transport only: the trusted controller must still bind its configured endpoint,
saved grant and persistent enrollment ID before exposing registration in the UI.
An unconfirmed response is not permission to allocate a new enrollment ID.
