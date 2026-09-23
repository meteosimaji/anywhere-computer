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
reconciliation. The expiry time and independent login have not been established.
Localhost tests verify the transport and ledger contract; they are not provider
acceptance tests.

On 2026-09-23, a separate live Plugin acceptance run used a successful Chrome
generation as its explicit in-memory handoff, closed Chrome, selected the model
and effort from the live HTTP catalog, then sent a new Chat and a queued follow-up.
Each dispatched once and reached `completed` through independent HTTP history;
the follow-up remained in the same conversation. The trial used a temporary
ledger and did not save or print header or body values. This confirms acceptance
for that observed session and request shape; handoff expiry remains unmeasured.

A separate 2026-09-23 lifetime trial waited 1,800 seconds after Chrome closed,
then used the previously observed handoff for HTTPX Sentinel preparation,
conversation preparation, generation SSE, and final history recovery. The new
Chat and follow-up reached `completed` with saved final answers. This demonstrates
that those exact observed values worked for at least 1,800 seconds on that session;
their expiry time is still unmeasured. That trial preceded this change to a single
HTTPX client, so it does not establish live provider acceptance of the unified
transport.

The frontend's current JavaScript includes a cookie-backed `/api/auth/session`
refresh flow that validates the returned access-token account and user before
updating browser auth state. One HTTPX request using the observed session Cookie,
`oai-did`, and target-path/route headers received 403 with no `Set-Cookie` and no
`accessToken` in the response. A successful browser refresh request was not
observed, so the HTTPX rejection cause and whether the endpoint renews a standalone
HTTP session remain unresolved. Credential refresh is not implemented.
