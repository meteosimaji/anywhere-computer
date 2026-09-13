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
below. Expired-grant reauthorization and switching accounts need an
explicit recovery transition; this version preserves pending state instead of
silently creating a new registration under a different attempt.

Restart tests also cover expiry after both confirmed registration and lost reply.
A confirmed cached receipt remains readable without a bearer request; an
unconfirmed record retains its original identity and fails before another request.
Neither path deletes or rewrites the stored grant. Reauthorization must bind the
new grant to the original relay account before resending pending registration:
the registry's idempotency key includes issuer and subject, so reusing only the
enrollment ID under another account would create a distinct device. Clearing the
vault and starting a new login is therefore not a complete recovery implementation.

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
seven fixed commands to its registration card. The native host selects Python,
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
Active or uncertain attempts, existing vault grants and saved registrations are
not reset or deleted. A fresh worker restores the attempt ID of an unexpired, correctly bound grant
from the OS vault before registration, without exposing or redeeming its token.
Expired grants remain preserved and unavailable; their reauthentication workflow
is still unfinished.

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
