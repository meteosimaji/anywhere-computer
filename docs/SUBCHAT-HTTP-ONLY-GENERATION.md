# Explicit HTTP-only ordinary Chat generation

`anywhere-subchat --http-only` remains read-only by default. Generation is an
opt-in process mode. Start it with `--http-session-stdin --http-generation-stdin`
and supply two bounded JSON lines through a trusted anonymous stdin pipe before
CLI commands or MCP framing. The first line is the existing observed read session.
The second line contains a complete, successful Chrome generation header set and
request templates held only in memory. Treat every value as sensitive, since the
full captured body may contain fields whose sensitivity is unknown. Do not place
either line in argv, logs, files, MCP tool calls, or shell history. The controller never opens
Chrome, discovers credentials, renews a session, or produces Sentinel proof or
Turnstile values.
The plugin's `mcp` dependency set includes the pinned Playwright Python package
for its existing APIRequestContext catalog/history reads; it does not install
or launch a Chrome browser.

The generation handoff has four fields:

| Field | Required content |
| --- | --- |
| `headers` | All 28 recognized names from the observed successful generation request, with their actual values. Authorization and account must equal the read-session line. Origin and referer must be `chatgpt.com`. |
| `sentinel_p` | The observed `p` value for `POST /backend-api/sentinel/chat-requirements/prepare`. |
| `prepare_template` | The full body observed on a successful conversation preparation, including `client_prepare_state: "sent"`, `partial_query`, timezone, and other observed fields. |
| `generation_template` | The JSON body template observed on a successful ordinary Chat generation. Message fields outside its input ID, text parts, and creation time are preserved. |

For each submission, the backend validates the exact HTTP catalog selection and
labels, saves a new outgoing input ID and account in the existing `SubchatSubmissions`
ledger, then sends Sentinel and conversation preparation once. The preparation
body retains observed fields and updates only input, model, effort,
conversation, and parent. It changes only
`accept` to `application/json` for preparation. The original successful generation
headers, including the handed-off Sentinel values and `accept: text/event-stream`,
are used for generation. A new Sentinel response token is observed but is not
substituted into the copied proof/Turnstile header set: that combination has not
been verified. No provider value is fabricated.

A queued follow-up rechecks its saved final answer against HTTP history and checks
the singular conversation endpoint's `current_node` against that exact assistant
final immediately before generation. New Chat omits conversation and parent IDs.
After preparation and branch checks, the durable dispatch claim commits before one
generation POST. SSE supplies only a conversation candidate. The existing flat
history projector must verify the saved input, unique final answer, and terminal
markers before the ledger reports `completed`.

Preparation failures leave the operation `sending` because remote effects are not
known. The same operation is never prepared or posted automatically again. A lost
generation response likewise remains uncertain; use `recover` with the original
operation ID. A new Chat without a conversation candidate may require manual
reconciliation. A handoff can expire, and long-term validity or independent login
has not been established. Localhost tests verify the transport and ledger contract;
they are not provider acceptance tests.

On 2026-09-23, a separate live Plugin acceptance run used a successful Chrome
generation as its explicit in-memory handoff, closed Chrome, selected the model
and effort from the live HTTP catalog, then sent a new Chat and a queued follow-up.
Each dispatched once and reached `completed` through independent HTTP history;
the follow-up remained in the same conversation. The trial used a temporary
ledger and did not save or print header or body values. This confirms acceptance
for that observed session and request shape; handoff expiry remains unmeasured.

## Private transport diagnostics

The local ledger keeps at most 64 HTTP diagnostic events per operation in
`subchat_http_events`. Each event contains only the operation ID, a fixed stage
name, an optional numeric HTTP status, and a Unix timestamp. The stage sequence
covers Sentinel preparation, conversation preparation, follow-up branch check,
the durable generation claim, generation HTTP status, SSE conversation candidate,
and history receipt, final, or unknown observations. It does not store headers,
cookies, proof values, prompts, answers, account IDs, URLs, or response bodies.
`SubchatSubmissions.http_events(operation_id, owner=...)` reads the bounded
events after checking ledger ownership. There is no MCP or CLI command that
exposes these private diagnostics.

A `*_request` event records a local attempt and is not evidence that the
provider received a request. `dispatch_claimed` is committed atomically with
the one-time generation claim; it never grants replay after a crash. A 200
generation response or SSE candidate does not establish a saved answer.
`history_final` is recorded only after the ledger has saved completion.
