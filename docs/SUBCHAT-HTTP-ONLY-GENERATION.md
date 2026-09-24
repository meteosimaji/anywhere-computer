# HTTP-only ordinary Chat generation

`anywhere-subchat --http-only` is a local, experimental controller for an
ordinary ChatGPT Chat. It reads by default. Sending requires a separately
observed, successful generation request handoff. The Codex Plugin's Subchat
server is read-only by default; its separate opt-in `browser-send` mode can send
through Chrome. Plugin installation does not configure this HTTP-only controller.
Use this transport only within the account owner's permission and account scope.
It is not the public OpenAI API, and provider behavior may change.

## Start and check the account

Install the optional `browser` extra and use a private ledger directory with
`--state-dir`. Choose one session source:

- `--http-session-stdin` consumes an observed read-session JSON line from a
  trusted stdin pipe. Add `--http-generation-stdin` to consume a second JSON
  line containing the generation handoff before JSON-line commands or MCP
  framing begin. This path does not open Chrome. Before a production send is
  reserved, it uses the supplied Cookie for a separate auth GET and checks
  that the returned account ID and access token match the selected session.
  A mismatch stops before any preparation or generation POST.
- `--chrome-login-profile PATH` opens an already logged-in, dedicated Chrome
  profile headlessly and obtains a read session through HTTP. The profile must
  be closed elsewhere. For generation, also pass
  `--http-generation-stdin --expected-account-id ID`; the generation handoff is
  still required. Use a separate profile for each Chat account.
- `--chrome-login-source-profile PATH` on macOS selects an existing ordinary
  Chrome profile directory such as `~/Library/Application Support/Google/Chrome/Default`
  or `Profile 1`. It takes a private, temporary snapshot of that profile's
  ChatGPT cookies and opens only the snapshot headlessly. The ordinary Chrome
  process can stay open and is not activated or changed. The snapshot is
  removed after startup; HTTPX retains the read session in memory. Select the
  profile explicitly and check the returned account. For generation, pin it
  with `--expected-account-id ID` and still provide the separate handoff.

Start in read-only mode and inspect `capabilities` (or
`subchat_capabilities` in MCP) for the authenticated account and available
transport. For Chrome-login generation, pin the reported account ID exactly;
startup rejects a mismatch before accepting a generation handoff. The
controller has no independent login or automatic acquisition of Sentinel,
proof, or Turnstile values. In the Plugin's read-only Chrome mode, an
authenticated GET that returns 401 triggers one new headless snapshot of the
selected profile, checks the same account through auth and catalog GETs, then
retries that GET once. A 403, generation POST, or deletion PATCH is never
retried this way. The `subchat_refresh_auth` tool requests the same read-session
refresh explicitly without sending or recovering a Chat. If Chrome itself is
logged out, the owner must sign in there; a startup authentication failure
requires `subchat_refresh_auth` after login or a controller restart. A fresh
profile with a different account is rejected when the controller has already
bound an account. The existing-profile snapshot is verified on macOS;
Windows and Linux retain the dedicated-profile or explicit-session paths.
On 2026-09-24 the macOS snapshot path returned HTTP 200 for both auth and model
catalog GETs, without opening a visible Chrome window. This is not generation
acceptance. A separate headless navigation using the same selected `Default`
profile snapshot returned HTML HTTP 403 with `cf-mitigated` present and no Chat
composer. Thus this snapshot can supply authenticated HTTP reads while the
headless Chat page cannot prepare a UI send. The test does not isolate whether
the challenge is caused by headless browser characteristics or omitted profile
state. A browser-assisted send still requires a verified ready composer;
authenticated GETs alone must not be used to claim that readiness.

The handoff has exactly four fields:

| Field | Content |
| --- | --- |
| `headers` | Recognized headers from a successful generation request. Authorization and account must match the read session; origin and referer must be `chatgpt.com`. If the request has no `chatgpt-account-id` header, a Cookie matching the authenticated HTTP session must be supplied and checked. The `openai-sentinel-chat-requirements-prepare-token` and `openai-sentinel-chat-requirements-token` headers are optional, according to what the observed request actually sent. |
| `sentinel_p` | The observed value for Sentinel preparation. |
| `prepare_template` | The complete successful conversation-preparation JSON body. |
| `generation_template` | The complete successful ordinary-Chat generation JSON body. |

The current controller uses the Sentinel preparation token received immediately
before generation. A requirements token from a previous finalized turn is a
different, stateful value; the controller does not renew it through Sentinel
finalize. A handoff captured from such a later turn is therefore not equivalent
to a fresh first-turn handoff.

In a 2026-09-24 Chrome 6 Pro new Chat and follow-up, both generation POSTs
returned HTTP 200 and the requested answers appeared. Both conversation
preparation responses had `conduit_token: null`, and neither generation sent
`x-conduit-token`. The new Chat preparation used `client_prepare_state: none`;
the follow-up used `success`. Neither preparation body contained
`is_do_not_remember`, and both contained `parent_message_id`. The handoff
validator and generation builder now accept these observed shapes, including
an absent conduit header. This corrects a local preflight rejection; it does
not prove that the independent HTTPX generation path is accepted. The
follow-up still needs the finalized Sentinel state that this controller does
not acquire.

Treat both stdin lines and every handoff value as sensitive. Pass them through
a trusted anonymous pipe; keep them out of argv, logs, files, MCP tool calls,
and shell history. The handoff is held in process memory, not the ledger.

## Send and recover

Select the model and effort from the current account's catalog. A send needs a
caller-generated 32-character lowercase hexadecimal `operation_id`, an exact
prompt, and the observed model and effort labels. A follow-up also specifies
the saved `conversation_id`. The controller validates the catalog selection,
prepares the request once, checks the current branch for a follow-up, and
dispatches generation once. It does not substitute newly observed protection
proof or Turnstile values into the handed-off headers. It does use fresh
conversation and Sentinel preparation response tokens for the generation POST.

A ChatGPT UI control sent `openai-sentinel-chat-requirements-token` on
generation, together with the proof and Turnstile headers. The observed Sentinel
and conversation preparation requests carried no `openai-sentinel-*` request
headers. This controller therefore sends those handed-off headers only on the
generation request. The UI completed Sentinel prepare/finalize while generation
was in flight; a later turn used the prior finalize response token. The
controller does not implement finalize or renewal of that requirements token.
It sets observed route headers such as `x-openai-target-path` and
`x-openai-target-route` to each request's actual endpoint. A new turn trace ID
is shared by conversation preparation and generation; Sentinel preparation
omits the previous conduit token and turn trace.
In two successful turns in one hidden in-app Chat, the observed request starts
were conversation prepare, generation, Sentinel prepare, then Sentinel
finalize; all four responses were HTTP 200 and both final answers arrived.
For the second turn, an in-memory comparison confirmed that its generation
requirements header equaled the previous turn's finalize response token, and
its proof and Turnstile headers equaled the previous finalize request values.
No token, Cookie, request body, or answer content was retained by this check.
That Sentinel prepare response marked Turnstile, proof of work, and the extra
collector as required. These observations show a concrete previous-turn
protection dependency, but do not establish a browser-free way to produce or
renew its values or prove the cause of a later HTTPX 403 response.

In a separate Chrome UI turn on 2026-09-24, the generation POST returned 200
and the final answer appeared in saved history. CDP `requestWillBeSentExtraInfo`
showed the actual sent header names, including Cookie and Origin, without
retaining their values. At observation time, the request had 11 application
header names outside the then-current handoff allowlist, including
`oai-device-id`, `oai-session-id`, `oai-client-version`, `x-conduit-token`, and
`x-openai-target-route`; it lacked several then-mandatory names, including
`chatgpt-account-id` and `oai-did`. The a19 allowlist was updated afterward.
The a19 handoff validator accepts these observed header names, but the current
UI request shape has not passed the complete handoff and generation path. In
particular, a request without `chatgpt-account-id` requires a matching Cookie
binding. The explicit stdin session now accepts a bounded Cookie header for
that check; this parser-level compatibility has not passed a live all-HTTPX
generation test and does not explain the HTTPX 403. The
observed request starts also differed: conversation
prepare and one Sentinel prepare/finalize cycle started before generation; a
second Sentinel cycle started while generation was in flight. These are dated
observations, not a stable protocol specification.

An explicit session Cookie is sent on this controller's exact-origin Chat
catalog, history, and deletion requests as well as the handed-off generation
requests. Supply only a Cookie authorized for those paths; explicit handoff
does not reproduce browser cookie path filtering.

An earlier live acceptance on 2026-09-23 used an explicit in-memory handoff
from a successful Chrome generation, then closed Chrome. The controller used
Playwright's APIRequestContext for Sentinel and conversation preparation and
HTTPX for the generation POST. With the observed headers and request templates,
it sent one new Chat and one queued follow-up. Both generation POSTs returned
200, and independent HTTP history checks verified each saved input and final
answer. This establishes acceptance for that particular session and request
shape. It does not establish independent acquisition or renewal of the handoff.
In a separate experiment, sequential HTTPX Sentinel and conversation
preparation POSTs returned 200 while the HTTPX generation POST returned 403.
Successful preparation responses and authenticated GETs do not guarantee
generation acceptance. Several session and request conditions differed between
the accepted handoff and rejected attempts, so the cause of the 403 is
unisolated.
In a fresh UI turn on 2026-09-24, value-free CDP comparison showed that the
conversation preparation response's `conduit_token` matched the generation
request's `x-conduit-token`, and the first Sentinel preparation response's
`prepare_token` matched its requirements-prepare header. The UI requested
conversation preparation, then Sentinel preparation, then generation; all
returned HTTP 200 and the answer was visible. The HTTP-only controller now
uses those fresh response values and that order. It has not yet passed a live
all-HTTPX generation POST or history-final check after this change, so the 403
cause remains unconfirmed.
For a generation 401/403, the local log records only the status, HTTP version
category (h1/h2/other), presence of `cf-mitigated`, and response content-type
category (JSON/HTML/other). These fixed categories help distinguish a likely
edge challenge from an application rejection; they do not prove the cause.
The response body and credential or protection values are never logged.

To compare the protocol shape of an accepted and a rejected generation request
without handling values, write two files of the form
`{"headers": ["accept", "..."], "body_keys": ["action", "..."]}`, containing only
header names and top-level JSON body key names. Then run
`python scripts/compare_chat_shapes.py accepted.json rejected.json`. It lists
missing (in the first, not the second) and added names per field. Exit status is
0 for matching shapes, 1 for differing shapes, and 2 for refused input. It refuses
mappings, non-name entries, duplicates, and some credential-like strings, never
prints file contents, and sends no requests. Its filter cannot recognize every
value. Build the name lists yourself from observed metadata at the same capture
layer; never copy header or body values into these files.
Caveat: identical shapes do not show that a request is acceptable, and a
difference does not prove the cause of a 403. Values, session state, TLS/HTTP
fingerprint, and timing are outside the comparison.

An HTTP success or streaming response alone does not prove completion. The
controller correlates the saved input, final answer, and terminal markers in
HTTP history before reporting `completed`. If an acknowledgement or answer is
missing, use `status` or `recover` with the original operation ID. A
failed preparation before the durable generation claim ends as
`preflight_failed`, with no generation POST and no automatic retry. A lost
generation response after that claim remains `sending` because its outcome is
uncertain. A process crash during preparation can also leave `sending` without
a claim; the controller does not infer a safe retry from that state. Never
resend an uncertain operation automatically. A new Chat whose
conversation ID was not observed may require manual reconciliation.

The HTTP-only path uses HTTPX for catalog and history reads, preparation,
branch checks, and generation. It does not automatically fall back to a
browser or replay rejected requests. The browser-assisted controller described
in the [Subchat guide](SUBCHAT-PROBE.md) is a separate mode.
The only accepted HTTPX generation test above used Playwright for preparation;
an all-HTTPX generation has not yet returned 200 in the recorded tests.

`subchat_download_file` over MCP can return up to 512 KiB of base64 bytes for
one exact sandbox link in a saved, verified final answer. It rechecks the
account and answer, and stores no downloaded file locally. File exchange
between Chats is not automated by this controller: a local path is not a Chat
attachment. Verify any target Chat's saved file state and actual bytes before
relying on a file handoff.
