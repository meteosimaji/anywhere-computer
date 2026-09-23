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
  framing begin. This path does not open Chrome.
- `--chrome-login-profile PATH` opens an already logged-in, dedicated Chrome
  profile headlessly and obtains a read session through HTTP. The profile must
  be closed elsewhere. For generation, also pass
  `--http-generation-stdin --expected-account-id ID`; the generation handoff is
  still required. Use a separate profile for each Chat account.

Start in read-only mode and inspect `capabilities` (or
`subchat_capabilities` in MCP) for the authenticated account and available
transport. For Chrome-login generation, pin the reported account ID exactly;
startup rejects a mismatch before accepting a generation handoff. The
controller has no independent login, automatic session renewal, or automatic
acquisition of Sentinel, proof, or Turnstile values. An expired Chrome login
requires the owner to log into the dedicated profile again.

The handoff has exactly four fields:

| Field | Content |
| --- | --- |
| `headers` | Recognized headers from a successful generation request. Authorization and account must match the read session; origin and referer must be `chatgpt.com`. The `openai-sentinel-chat-requirements-prepare-token` and `openai-sentinel-chat-requirements-token` headers are optional, according to what the observed request actually sent. |
| `sentinel_p` | The observed value for Sentinel preparation. |
| `prepare_template` | The complete successful conversation-preparation JSON body. |
| `generation_template` | The complete successful ordinary-Chat generation JSON body. |

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
values into the handed-off headers.

A current ChatGPT UI control sent `openai-sentinel-chat-requirements-token` on
generation, together with the proof and Turnstile headers. The observed Sentinel
and conversation preparation requests carried no `openai-sentinel-*` request
headers. This controller therefore sends those handed-off headers only on the
generation request. The UI completed Sentinel prepare/finalize while generation
was in flight; a later turn used the prior finalize response token. The
controller does not implement that token lifecycle or the finalize request.
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

An HTTP success or streaming response alone does not prove completion. The
controller correlates the saved input, final answer, and terminal markers in
HTTP history before reporting `completed`. If an acknowledgement or answer is
missing, use `status` or `recover` with the original operation ID. A
preparation failure or lost generation response can leave the operation
`sending` because remote effects are uncertain. Never resend it under a new ID
as an automatic recovery step. A new Chat whose conversation ID was not observed
may require manual reconciliation.

The HTTP-only path uses HTTPX for catalog and history reads, preparation,
branch checks, and generation. It does not automatically fall back to a
browser or replay rejected requests. The browser-assisted controller described
in the [Subchat guide](SUBCHAT-PROBE.md) is a separate mode.

`subchat_download_file` over MCP can return up to 512 KiB of base64 bytes for
one exact sandbox link in a saved, verified final answer. It rechecks the
account and answer, and stores no downloaded file locally. File exchange
between Chats is not automated by this controller: a local path is not a Chat
attachment. Verify any target Chat's saved file state and actual bytes before
relying on a file handoff.
