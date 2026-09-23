# Browser-free subchat recovery

The HTTP-only adapter retrieves model catalogs and results for known conversation/input
identities without launching Chrome and can optionally send ordinary Chat requests
from an explicit in-memory generation handoff. It reuses the existing ledger and
HTTP projections; it does not implement independent login or credential refresh.
See [live probe evidence](SUBCHAT-PROBE.md) and the [generation contract](SUBCHAT-HTTP-ONLY-GENERATION.md).

The [2026-09-22 consolidated audit (Japanese)](SUBCHAT-HTTP-AUDIT-2026-09-22.ja.md)
records request-contract findings, authentication blockers, transport regressions,
and the sender design before the explicit handoff implementation. Historical conclusions
there should be read with the later generation contract and acceptance below.

The follow-up [HTTP/Codex mechanism study](SUBCHAT-HTTP-MECHANISMS-2026-09-22.ja.md)
traces selected third-party implementations and reports isolated characterization
and localhost streaming tests. Its [research code and evidence](research/2026-09-22-http-mechanisms/README.md)
do not enable a production sender or establish live ChatGPT acceptance.

## Implemented behavior

`HTTPOnlySubchatBackend` reuses `ChatHTTPReader`, existing receipt/final projections
and the existing SQLite submission store. With an explicit in-memory generation
handoff it supports one-shot ordinary Chat dispatch; it has no browser factory. The
original operation, owner namespace, account binding,
prompt/resources, correlation IDs, final-completion and interruption checks remain
in effect. CLI/MCP use the existing local `owner=None` namespace; this does not grant
access to a different owner's records or implement multi-account authentication.
All HTTP-only catalog/history GETs, Sentinel and conversation preparation POSTs,
current-node checks, and generation SSE use one process-owned HTTPX `AsyncClient`. It
disables environment proxy discovery and redirects, configures zero transport retries,
and is closed when the CLI/MCP process exits. The separate browser-assisted mode keeps
its Playwright request context.

`--http-only` needs no `--browser-profile`. It rejects browser-profile, minimized and
`--http-read` options instead of silently changing transport. Status/list/cancel and
saved terminal recovery need no authorization or Playwright runtime. An unknown
conversation/input remains unresolved without a history-wide search or resend.

For authenticated reads, an authorized launcher may supply an already established
session explicitly. `--http-session-stdin` consumes one UTF-8 JSON line, at most
32 KiB including its newline, before the ordinary JSON-lines or stdio MCP protocol.
The fields are `authorization` (an already observed bearer value), `account_id`,
`catalog_url` (the exact observed HTTPS `/backend-api/models` URL with its query),
and optional `language`. No session acquisition tool is included. Do not extract
Codex credentials or replay preparation/proof tokens to populate this interface.

The input schema rejects unknown fields, cookies, protection-token fields, duplicate
JSON keys, foreign origins, userinfo/ports/fragments, control characters, oversized
input and malformed bearer values. Credentials are not command-line arguments,
output fields or ledger data. Secret representations are masked, startup validation
errors are redacted, and no credential file is written. The handoff assumes a trusted
local parent and stdin channel; it is not inter-process caller authentication.
Startup waits for the supplying process's line; it has a size limit, not a promised
startup-time deadline.

GETs use `follow_redirects=False`, zero HTTPX transport retries and a 15-second
request timeout, within the adapter's 20-second read deadline. A 401 is latched for the session;
a 403 is latched for that exact URL. Error response bodies are not read by the
reader. Credentials are not refreshed and failures cannot fall back to a browser.
A fresh controller/session is an explicit operator decision, not an automatic
recovery strategy. No TLS identity or User-Agent spoofing is introduced.

A fresh send may create the existing local `prepared` proposal but fails before
`begin_send`; it is not reported as delivered. A new queue is refused before creating
its child record. Recovery of a pre-existing queued child cannot dispatch it, even
when its parent completes. Repeating an already reserved/completed operation returns
the saved record under the existing idempotency contract. No new ledger, state value
or credential table was added.

The CLI `capabilities` action and MCP `subchat_capabilities` report configured
transport without network activity. HTTP-only mode with no generation handoff reports
`generation_transport: unavailable`; with a valid handoff it reports
`explicit_handoff_http`. Neither mode supports independent login or credential refresh.
The catalog exposes exact dynamic selections required for sending. MCP `source=ui`
reports `ui_unavailable`, without changing source.

## Usage

After installing this revision, saved-state inspection needs only:

```sh
anywhere-subchat --http-only --state-dir /absolute/path/to/original-subchat-state
```

Send `{"action":"capabilities"}`, `{"action":"list"}` or a `status`/`recover`
command with the original operation ID on standard input. For stdio MCP, add
`--mcp` and use the corresponding tools.

For network reads, the launcher adds `--http-session-stdin` and writes its authorized
session envelope to the anonymous stdin pipe before protocol messages. Do not put
credentials in shell command literals, environment variables, reports or tool calls.
The implementation supplies no credential-discovery recipe. Without a session,
network-requiring commands return `http_session_required` with automatic retry disabled.
After a 401, session renewal must happen through an approved authentication path,
then the original operation IDs must be recovered without resending.

## Verification and remaining work

On 2026-09-21, the integrated HTTP-only backend fetched a real known 6 Pro
conversation through a fresh APIRequestContext. It returned the same final ID
independently read through Codex and 8,570 characters. That code constructed no
BrowserContext and launched no Chrome process; existing user browsers were left
untouched. Authorization came from the already established in-memory session.
This establishes live recovery, not cold-start authentication or authentication renewal.

Tests cover CLI and stdio MCP subprocesses, real HTTPX requests against a local
fixture server, identity/account boundaries, unavailable operations, credential
redaction and client disposal. The HTTP-only CLI path does not start Playwright;
Chrome is not required for saved-state recovery, authenticated history GETs or catalog GETs.

The earlier constructed generation POST returned 403 even with an associated browser
request client. It predates the explicit successful Chrome handoff tested below. The
current dispatcher never retries or replaces an uncertain send. Authentication renewal
and provider-side stop remain unsupported.
The browser-assisted sending path also retains its failed fullscreen interference
acceptance; minimized windows are not a guarantee of non-interference.

## CoS comparison refreshed 2026-09-21

The upstream main observed in this review was
[`7777e517`](https://github.com/totec448-spec/chat-on-steroids/commit/7777e517603289696bb5febddb9bbf51cdde9aea).
The following are upstream reports and source observations, not reproduced CoS
runtime failures:

- [Issue 336](https://github.com/totec448-spec/chat-on-steroids/issues/336)
  reports repeated browser recovery without restored execution. Retain bounded
  observations and operation recovery in Anywhere; do not treat reload attempts
  as task progress. HTTP-only mode has no reload fallback. This does not prove
  indefinite provider stalls are resolved.
- [Issue 329](https://github.com/totec448-spec/chat-on-steroids/issues/329)
  reports a partially degraded read-only tool surface. Explicit transport
  capabilities and unavailable-send errors are adopted here so successful reads
  cannot imply working generation. Per-turn health of unrelated connectors is
  outside this change.
- [Issue 327](https://github.com/totec448-spec/chat-on-steroids/issues/327)
  reports stale waiting state. Anywhere's call-scoped pending reason and exact
  input/final correlation remain authoritative; an idle page is not completion.
  The async-final fix addresses our independently observed false pending case,
  not the upstream issue itself.
- The [Stop activity change](https://github.com/totec448-spec/chat-on-steroids/commit/20081243fd02a40de9918abe6524b247abcfe75f)
  separates stop intent from later exact work and preserves canonical final
  evidence. Its recorder can reopen an inferred ended turn only with earlier
  request ownership and later work; a canonical final prevents reopening.
  Anywhere should likewise distinguish a local cancel from provider termination.
  Importing CoS's companion/Fiber/request ownership machinery is not part of this
  HTTP recovery change: it would add a separate identity system without proving
  that its observations exist on our transport.

No upstream code was copied. New generation, authenticated child identity and
non-interrupting message delivery remain independent acceptance requirements;
a read-only recovery mode cannot satisfy them.

Capability responses also expose `queue_dispatch`, `background_dispatcher`,
`native_steer`, `provider_stop` and `cancel_scope`. Browser-assisted queues advance
through recovery/wait; HTTP-only queue dispatch is unavailable. Neither adapter
provides native steer or provider stop. Cancellation applies only to local queued
or prepared inputs. These are configured capabilities, not live service health.

### Product CLI restart acceptance, 2026-09-21

A SQLite backup of the existing real 6 Pro submission was used in an isolated,
owner-only temporary directory. Three separate CLI processes used the same
operation ID and state, with both Playwright browser-launch methods forbidden:

1. Without an HTTP session, recovery returned `http_session_required`.
2. An already established session was passed through anonymous stdin, without
   writing credentials to disk. Real HTTP recovery returned `completed`, answer
   `ba44459e-8666-46ac-a102-fbc491e6d14a`, and 8,570 characters.
3. A new CLI process without credentials returned that same saved final and length.

All three exited 0 with no stderr. This verifies product-CLI recovery and persisted
results across process restart, not authentication renewal, fresh generation,
OS restart or retention of an in-progress provider stream. Original live state
was not modified; no generation was sent and no Chrome launch was permitted.

HTTP-only MCP recovery permits separate operation IDs to be observed concurrently
within the existing eight-recovery limit. Same-operation observers share the
existing task. Browser-assisted recovery and queued dispatch retain serialization.
A controlled slow-read fixture previously blocked a second independent recovery;
it now completes the second result before releasing the first. This is inner MCP
scheduling evidence, not proof that an enclosing direct-MCP transport multiplexes
calls or that live ChatGPT permits any particular parallel generation rate.

Concurrent first use also shares one lazily initialized HTTP client. A controlled
catalog/recovery race reproduced duplicate initialization before the fix; normal
and exceptional shutdown now dispose that client once. Only initialization is
locked, not the independent HTTP reads.
Reads waiting for client initialization recheck session-wide 401 and URL-specific
403 rejection before issuing their GET. Controlled races verify no extra GET is
issued after that rejection; requests already in flight are not cancelled.
