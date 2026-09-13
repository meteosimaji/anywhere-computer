# Isolated relay development

The relay is not deployed. The native manager has a development registration
card, but its end-to-end pairing workflow has not passed acceptance. Local MCP
and personal SSH/HTTP connections remain unchanged.

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

The isolated `test_registered_certificate_revocation_after_tls_connect` connects
the registry to the existing mutual-TLS listener. A real verified client
certificate resolves to its registered device; after revocation, a frame sent on
an already authenticated socket is rejected before the test handler receives it.
The listener's static peer entry is deliberately retained, so the assertion
exercises the durable registry check rather than merely closing enrollment in
the listener. Certificates are disposable test fixtures. This proves the lookup
boundary over TLS, not outbound WebSocket transport, production provisioning,
engine execution, or a Windows-to-relay acceptance test.

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
production concurrency or rate-limiting design. The native worker uses the
persistent registration client described below; a registered device still cannot
execute operations until the separately authenticated outbound link is implemented.

The client-side `https_enrollment_registration` transport now sends JSON and a
bearer grant over verified HTTPS. It shares the form flow's bounded exchange,
duplicate-response-field rejection and no-redirect/no-retry behavior. Local TLS
tests cover Japanese/emoji names, bearer delivery, malformed-header rejection
before dispatch, and response loss after one request without replay. This is
transport only: the registration client and native worker described below bind
the configured endpoint, saved grant and persistent enrollment ID for the UI.
An unconfirmed response is not permission to allocate a new enrollment ID.

## Client registration recovery

`RegistrationClient` persists the original enrollment ID, authorization attempt,
endpoint and normalized name before its first registration request. The database
contains only those bindings, the hashed OS-vault reference and the eventual
device receipt; it does not persist bearer or refresh tokens. The existing grant
store verifies the exact authorization attempt and scope before a network call.

An explicit retry after response loss reuses the saved enrollment ID. A record
without a device receipt means unconfirmed: the server may already have created
the device. Changing the endpoint, name or authorization attempt while recovering
is rejected without another request. A successfully saved receipt is returned
without repeating registration, but remains cached registration evidence, not a
connection check or proof that authorization is still valid. Native callers must
run this synchronous client on its owning worker thread and close its database.

Tests exercise restart after simulated post-registration response loss, stable
identity and cached receipt reuse, conflicting recovery inputs, invalid/rejected
responses and absence of the synthetic token from the local database. The HTTPS
wire has separate actual TLS tests. The native worker/UI integration is described
below. Expired-grant reauthorization uses the explicit recovery transition
described below; switching accounts never silently creates a new registration
from an existing pending request.

Restart tests also cover expiry after both confirmed registration and lost reply.
A confirmed cached receipt remains readable without a bearer request; an
unconfirmed record retains its original identity and fails before another request.
Neither path deletes or rewrites the stored grant. Reauthorization must bind the
new grant to the original relay account before resending pending registration:
the registry's idempotency key includes issuer and subject, so reusing only the
enrollment ID under another account would create a distinct device. Clearing the
vault and starting a new login is therefore not a complete recovery implementation.

The isolated adapter now offers `POST /enrollment/account` with an empty JSON
object and the enrollment bearer. It uses the same signature, issuer, audience,
client, lifetime and exact-scope validation as registration, and returns only the
verified issuer/subject. It neither writes device records nor opens MCP sessions.
Native configuration may select `account_endpoint` on the same HTTPS origin as
registration. The client verifies issuer/subject before its first registration,
persists that binding before dispatch and checks it on a pending retry. A changed
account or removed/changed lookup endpoint cannot resend the pending registration.
Existing pending records without identity are not silently assigned the current
account. Registration storage advances to schema 3; old records remain readable,
while older binaries reject the newer database version. The owner binding stays
out of native progress responses. Account lookup alone does not renew an expired
grant; the native worker must obtain a fresh grant through explicit authorization.
Account identifiers are not bearer credentials, but callers should keep them out
of routine diagnostics. This remains an isolated endpoint, not public deployment.

The combined HTTP recovery test connects `RegistrationClient` to this adapter
over an actual loopback socket with signed enrollment tokens. It drops the first
successful registration response, reopens the client, rejects a different verified
account, then recovers the original enrollment using the original account. It
checks the exact request sequence and one stored device. Its credential vault is
in memory and its loopback transport is HTTP: this test does not establish native
OS-vault behavior, public HTTPS readiness or expired-grant reauthorization.

`RegistrationClient.recover` can now recover an existing account-bound request
using a separately saved, current enrollment grant from the same provider and
client. The trusted caller supplies that credential store and its new attempt ID;
the method verifies the account before resending the original enrollment ID and
name. It preserves the original request's authorization attempt, both OS-vault
slots and pending state after another lost response. A confirmed result is cached
under the original registration. Legacy records without a verified account cannot
use this transition. Tests cover grant expiry, a different account, mismatched
provider/client/attempt, changed lookup endpoint and repeated response loss with
restart. These tests use an in-memory vault and injected registration wire.

The native worker's `reauthorize` command now records a fresh vault-slot identifier
before starting authorization and uses a separate profile in the existing OS
credential store. Reopening the worker selects the latest recorded slot and
restores its saved grant without redeeming the code again. The manager offers
this command for an account-bound pending registration, disables it during an
active authorization exchange, and keeps the original name fixed. Registration
then calls `recover` with the newly saved grant. Neither a token nor the slot
identifier is accepted from the WebView.

Schema 3 adds a history of slot identifiers, retaining older slots for explicit
credential cleanup. Once registration is confirmed, the manager's `cleanup`
command deletes only those recorded recovery profiles. Each stored grant must
match its provider, client and enrollment scope. Deletion is read back before
removing its slot record; failed cleanup retains that record for retry. Already
missing entries can be reconciled without deleting anything else. The original
grant and registered device receipt remain intact. No automatic deletion is
implemented. A process loss before
the grant is saved still loses the in-memory device code; the user can explicitly
start a new authorization while preserving the original registration. Worker
tests cover expiry, reauthorization, termination after grant save, restoration
and recovery, using an in-memory vault and injected provider/relay wires. Native
IPC and UI state tests cover the new fixed command. Real-provider/native OS-vault
reauthorization remain acceptance work; this is not automatic
refresh-token renewal or a verified public pairing service.

On 2026-09-14, a separate macOS acceptance check used the real
`keyring.backends.macOS` backend and three successive Python processes: the first
saved the original grant and retained a pending request after injected reply
loss; the second reauthorized into a separate Keychain slot; the third restored
that grant and recovered the original registration. The authorization attempt
matched between the second and third processes and the enrollment ID matched
across all three. Both test-only Keychain entries were deleted and their absence
verified afterward. Provider and relay responses were injected fixtures and grant
expiry used a controlled clock. This verifies the native credential and process
restart boundary, not rendered manager UI, real-provider reauthorization,
production cleanup behavior or public relay connectivity.

A subsequent run repeated the three-process native Keychain check with the
worker's `cleanup` command after recovery. The command removed the recovery
credential and its slot record, preserving the original grant and device receipt;
the test harness then removed the one remaining original test grant. This verifies
that cleanup path on macOS with synthetic grants. Failure/readback/retry cases
remain covered by injected-vault tests, and rendered UI and Windows cleanup
acceptance have not been established by this run.

## Native host lifetime

The current native manager launches a short-lived Python command for each status
or startup action. That model cannot retain `DeviceAuthorizationClient` state
between start and poll. Enrollment therefore needs a separate host-owned worker,
not additional independent status subprocesses or private codes in WebView state.

`EnrollmentWorker` and `serve_enrollment` provide the fixed-command worker
boundary: start, progress, poll, cancel, retry_save and register. The trusted host
supplies the configured clients. Line requests cannot select endpoints, paths,
executables or arbitrary engine commands. Responses exclude the OS-vault
reference and private grants; user codes and verification links remain available
for the explicit authorization UI. Calls are serial, progress is local-only,
and EOF closes the registration database/cancels the local authorization attempt
without stopping the computer agent. Transport failures return a generic error;
the host must inspect progress rather than replay a state-changing command.

This worker is not yet wired into the Tauri process lifecycle or management UI.
The native host still needs bounded pipe I/O, pending-call handling, verified
worker exit and trusted provider configuration. Its tests use the real client
state machines with synthetic provider responses; they are not a rendered native
authentication or public-service acceptance test.

The worker now has a native-process entry point:
`python -I -X utf8 -m anywhere_computer.enrollment_worker --state-dir ABSOLUTE_PATH
--config ABSOLUTE_PATH`. Its bounded, non-symlink JSON configuration contains
`provider` (the existing `EnrollmentProvider` schema, exactly `device:enroll`)
and `registration_endpoint` (HTTPS, no query). This is trusted host configuration,
not a WebView argument or a general-user installation step. Invalid startup or
request errors do not echo the configuration or provider responses.

On macOS, a separate Python process using the installed OS-vault backend started
with a disposable directory and synthetic `.invalid` endpoints. Two pipe commands
returned `new` then `cancelled`, both with `connection_state: not_checked`; EOF
terminated the child with exit code 0. No real-account authorization was started.
This establishes the native Python entry point, not Tauri pipe integration,
Windows worker execution, or browser-based enrollment acceptance.

## Native manager integration (development)

The Tauri manager now owns one persistent enrollment worker and exposes only
nine fixed commands to its registration card. The native host selects Python,
the state directory and `enrollment-provider.json`; the WebView cannot choose
an executable, endpoint, state path or arbitrary command. Requests are serial,
responses are bounded to 32 KiB and an exchange has a 45-second deadline.
A normal rejected command preserves the worker and its authorization state.
A broken pipe, invalid reply or deadline discards the worker without automatically
resending a potentially dispatched registration.

The card displays the public user code and verification URL, obtains a device
name and distinguishes a stored registration from a live connection. It does
not expose access tokens or credential references. After an uncertain response,
only explicit state inspection is enabled; a durable pending registration reuses
its original name and enrollment ID. Denied, expired, cancelled or failed attempts can be explicitly restarted in
the same manager. A fresh authorization object isolates late cancelled replies.
Active exchanges cannot be replaced. Existing vault grants and saved registrations
are not deleted. A fresh worker restores the attempt ID of an unexpired, correctly bound grant
from the OS vault before registration, without exposing or redeeming its token.
Expired grants remain preserved and unavailable; the separate-slot reauthorization
flow above can recover account-bound pending registrations.

Rust framing/cleanup tests, Python worker tests and JavaScript state tests cover
these layers separately. They do not establish a rendered Tauri-to-provider
end-to-end acceptance result. The feature requires an explicitly selected state
directory and native provider configuration; default installation/pairing and
public relay transport are not complete.

On 2026-09-14, the macOS Tauri preview's actual registration status button
returned the Python worker's initial state and enabled authorization start.
This used a disposable state directory and a temporary environment installed
from the checksum-verified bundled wheel and locked dependencies. The initial
development-venv attempt timed out while Python was initializing its paths
(sampled in `getpath_readlines`/`open`); the precise environmental cause remains
unconfirmed. No OS permission was changed. The successful UI check did not
start provider authorization, save a grant or register a device.

The worker reports `authorization.can_retry_save` only while it still holds an
unexpired received grant awaiting vault publication. The card does not offer a
save retry merely because existing credentials are unreadable or expired.
Progress re-evaluates the pending grant's lifetime without another token request.

The native pipe regression test uses a real child process: its first request is
rejected, its second succeeds in the same process with a request counter of two,
and an oversized third response causes worker disposal. Reintroducing the old
condition that discarded a worker on an ordinary command rejection makes this
test fail. The fixture tests native IPC lifecycle, not provider authentication;
it is launched by the parent test rather than run standalone in the suite.

## Next transport boundary: implementation decision

The current registration receipt is not a PC channel credential. No outbound
channel is implemented by this document. The next isolated implementation must
connect to the existing engine rather than introduce another executor or result
database.

The existing `remote_transport` provides verified mutual TLS, bounded frames and
stream cleanup, but its listener runs on the PC and handles one inbound request.
Using it unchanged would retain the requirement for PC-side reachability.
`DeviceRouter` also opens SSH/HTTP backends on demand; a registered account alone
cannot turn either backend into an outbound connection. Its operation binding
and no-repeat semantics remain requirements, not a ready-made reverse transport.

Evaluate a PC-initiated WebSocket channel for the deployed path, because it can
share an HTTPS ingress without opening a port on the PC. Raw reverse TLS would
reuse more framing code but introduces a distinct ingress protocol and proxy
compatibility burden. Do not install a WebSocket dependency or claim proxy
compatibility until its supported version and an actual loopback exchange have
been checked. AI-facing MCP remains a separate protocol and authorization layer.

An isolated 2026-09-14 evaluation used websockets 17.0.1 in a disposable Python
environment, without adding it to the project runtime. Verified-server WSS on
loopback carried requests from a test relay to two sequential PC-initiated
connections. Two real `Engine` instances with separate state directories created
Japanese/emoji files and recovered each write through `operations_get`; the
other engine's ledger did not contain that operation. Both engines ran in one
process, and grants were synthetic. PC authentication, reconnect/reply-loss,
concurrent routing, public proxy compatibility and separate-process acceptance
were not tested by this experiment. It establishes feasibility of reusing the
engine behind an outbound channel, not completion of the relay.

The first channel slice must bind an authenticated PC credential to an existing,
non-revoked registry device. A channel replacement must invalidate the old
connection generation. Tool requests require a separately verified, expiring
AI grant bound to account, device and allowed tools; the PC must validate that
grant too. The registration token is rejected by both operation boundaries.
`RemoteAgent` checks allowed tools and namespaces operation IDs. Its trusted
`grant` API now accepts an absolute `expires_at`, checked for each incoming
request including catalog and result lookup. Once observed expired, the entry
is removed, so clock rollback does not resurrect it. Refresh is explicit and
retains the original namespace only when the caller verifies the same grant.
Invalid refresh parameters leave the previous grant intact. Existing enrolled
TLS peers can still omit expiry; a relay integration must supply verified expiry
and must not silently use this legacy unlimited form. This helper does not
validate tokens, bind accounts/devices or cancel work already dispatched.

Keep only bounded in-flight correlation in the relay. Offline requests fail
before dispatch; a lost in-flight reply is unknown and is never queued for
automatic delivery after reconnect. Recovery queries the original operation ID
through the same account/grant/device binding. Existing engine ledger records
remain the authority for execution and results. Do not interpret reconnected
transport as restored terminal or REPL state.

Before exposing the channel to the manager, exercise two real local engine
processes through a loopback relay: explicit target selection, file mutation and
readback, same-ID recovery after reply loss, disconnect without delayed writes,
expired/revoked grants, cross-account device IDs and cross-grant result lookup.
Synthetic identity fixtures establish protocol behavior only; fresh Mac/Windows
installation, real OAuth client integration and public ingress require their
own acceptance evidence.

### Durable PC certificate binding (isolated building block)

Registry schema 2 adds a one-to-one binding from a verified SHA-256 peer
certificate fingerprint to a registered device ID. `bind_channel` requires
the authenticated account and rejects revoked devices, cross-account binding,
certificate reuse by another device and silent replacement of an existing
binding. An identical provisioning retry is idempotent. Existing device records
and revocation tombstones survive migration from schema 1.

`channel_device` resolves the account and device from that fingerprint and
rechecks current revocation on every call. Its caller must obtain the fingerprint
from an authenticated TLS peer, not a client-supplied message field. These are
trusted storage APIs, not public enrollment endpoints or proof-of-possession
verification. No private key or bearer credential is stored in this table.
Certificate issuance, OS-vault private-key handling, explicit rotation and the
outbound WebSocket integration are still unimplemented.
