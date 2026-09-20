# ChatGPT subchat transport investigation

Status: historical transport investigation plus experimental subchat development.
The browser/stdio prototype exists; individual capability and live-acceptance
claims below have separate scopes. This is not a stable desktop-steering API.

Product name: `subchat` (Japanese: サブチャット). A subchat is an ordinary
ChatGPT Chat created for a delegated task. It is not a ChatGPT Work task.
Historical `AC_CHAT_WORKER_...` test markers below are retained as evidence.

For current use, start with the [CLI](#packaged-local-json-lines-controller),
[MCP entry](#stdio-mcp-entry), [HTTP answer recovery](#experimental-saved-http-answer-recovery),
[explicit resources](#explicit-send-resources-experimental), and
[capability boundaries](#coverage-boundary-for-the-next-http-increments).
The chronological investigations below preserve earlier failures and superseded
assumptions; they are not separate current feature inventories.

The requested subchat uses ordinary ChatGPT Chat, not a Codex task, ChatGPT
Work task, or paid OpenAI API. Creating a new conversation and recovering its
identity are required; appending to an existing conversation is insufficient.

## Evidence on 2026-09-18

- The owning Codex app's `list_threads` and `read_thread` tools returned an
  existing ordinary ChatGPT acceptance conversation, including message IDs.
- The installed app provides `send_message_to_thread`, but its model override
  applies only to Codex tasks. Follow-up submission was subsequently tested as below.
- Both the installed `create_thread` schema and its dispatch implementation
  accept `project`, `projectless`, and `chatgptWorkCloud`. There is no ordinary
  Chat target in that interface. Work is not a substitute for Chat.
- The bundled `codex-app-tools` MCP server uses the app-provided
  `CODEX_APP_TOOLS_PIPE_PATH` and requires executor thread metadata for calls.
  A separate MCP client initialized successfully, but `tools/list` failed with
  `ENOENT`: its provided Unix socket pathname was absent. The app still held
  open socket descriptors for that pathname. The deletion cause is unknown.
- Initial browser discovery timed out. A fresh inventory recovered Chrome, and
  the ordinary Chat trial below succeeded. No Work task, paid API call, or
  Codex inference was requested by these probes.

Run the read-only probe in the owning Codex environment:

```sh
python scripts/probe_subchat.py --server /absolute/path/to/codex-app-tools/server.mjs
```

Optionally add `--read-thread` with an explicitly selected acceptance conversation
ID. The probe prints capability schemas and read-result metadata, not conversation
text. Missing endpoints fail before dispatch. It does not guess other sockets,
invent a caller ID, or bypass the existing plugin recursion guard. Creating
another Codex task to invoke Chat is not an inference-free substitute.

## Required next acceptance

1. Establish a supported live connection for the selected adapter.
2. Create an ordinary Chat and verify the requested model before submission.
3. Capture conversation ID and the submitted user message ID.
4. Recover the corresponding assistant response and continue the same subchat.
5. Simulate a lost acknowledgement without creating a second conversation or
   repeating a submitted prompt. Report an unknown outcome when reconciliation
   cannot establish whether submission occurred.

CoS currently uses a browser companion for normal Chat creation and submission;
an API-shaped external interface does not eliminate that browser dependency.
Anywhere may need that adapter for creation even if the Codex app's existing
conversation read/send tools can be reused. This remains unverified, and no
production subchat tool should advertise support on the strength of this probe.

## Ordinary Chat live trial

A new Chrome tab opened normal ChatGPT with `Chat` selected and `Work` unselected.
GPT-5.6 Sol was selected explicitly, and the power slider confirmed medium.
One prompt requesting `AC_CHAT_WORKER_20260918_A 42` was sent through the browser.
The provisional `WEB:` URL changed to a persisted conversation ID; the assistant
returned the exact marker. Codex's `read_thread` returned the same prompt and
response with their message IDs.

One follow-up requesting `AC_CHAT_WORKER_20260918_B 43` was then submitted through
Codex's `send_message_to_thread`, without browser typing. The tool acknowledged
with the conversation ID. A browser reload showed both turns and the exact second
answer. Its response-model menu said `5.6 Sol`.

However, subsequent `read_thread` calls continued returning only the first turn,
including the old updated timestamp, even after the second answer was visible in
the browser. Treat this as stale read evidence, not a missing-send conclusion;
do not replay the follow-up. A subchat result collector must match the expected
new user/assistant message pair and cannot accept an arbitrary idle snapshot.

This establishes browser creation plus Codex-app follow-up submission. It does
not establish headless creation, fresh tool-only result recovery, or the complete
Chat -> Anywhere -> Codex app -> subchat path. The external MCP endpoint was
missing in that executor environment (see the later recheck below). The two socket-preflight tests
exercise absent caller context and an unlinked-but-open Unix socket; they are not
subchat end-to-end acceptance tests.

### Follow-up recovery and bounded waiting

A later Codex read recovered the second turn with a new user-message ID and
assistant-message ID. The user independently confirmed both turns in the mobile
ChatGPT app. The installed reader uses a cache-aware `getOrFetch`, while send's
preflight uses explicit `refetch`. This supports delayed read visibility; it does
not prove the exact cache expiry or notification responsible for the delay.

The probe accepts either `--after-user-id` (a baseline before submission) or
`--user-message-id` (the observed ID of the submitted user message), plus
`--expected-prompt-file` and `--wait-seconds` (1–300) with `--read-thread`.
The explicit submitted ID also supports the first reply in a new Chat, which has
no previous user message. Capture this ID from an actual submission observation;
do not manufacture it or substitute the conversation ID. These only read an already-submitted
prompt. They never send or resend it. The collector requires the selected Chat,
a visible baseline turn and exactly one subsequent matching prompt, or exactly
one turn with the explicitly selected user-message ID. Both modes require a distinct
answer ID, an idle thread, completed turn and untruncated text. Ambiguous,
truncated, active, missing-baseline or stale snapshots remain unconfirmed. The
current prototype deliberately does not accept multi-item/tool-bearing turns;
that broader contract remains unimplemented. Prompt file contents are compared
exactly, including trailing newlines. Output contains IDs and character counts,
not the response text. A timeout exits nonzero with `reply_unconfirmed`.

Twenty targeted tests cover endpoint context, the unlinked socket, old snapshots,
wrong Chat/prompt/baseline, active or incomplete responses, duplicate matches,
truncation, eventual freshness and bounded read timeout. They validate collector
logic, not the complete external subchat connection. Initial-reply tests also
cover a missing baseline, absent or conflicting message IDs, incomplete answers,
duplicate turns, delayed visibility and mutually exclusive identity selectors.
Baseline matching counts matching submissions before examining their answers.
A second identical prompt remains ambiguous even if its answer is absent,
failed or truncated. Three regressions reproduced selection of the other answer
before this fix. An explicitly observed user-message ID can disambiguate the
selected submission without replaying either prompt.

The matching function was also run against a fresh real `read_thread` response
from the two-turn acceptance Chat. It selected the second user and answer IDs
and matched the expected `B 43` text. This verifies real response compatibility;
the separately launched MCP connection remains unverified.

The explicit submitted-ID mode was separately checked against a fresh real read
of the first `A 42` turn, with user-message ID
`fa837d30-8eeb-45fa-83d1-6e075fe4225e`. It recovered assistant-message ID
`10cfb9ae-c7de-4e22-be4b-8a97c5b4ff91` and the exact 28-character response,
without requiring a preceding conversation turn or sending another prompt.

A further source check found `staleTime: ONE_MINUTE` on the installed Chat
conversation query. The probe defaults to a 120-second deadline and a five-second
read interval so the default deadline does not coincide with the nominal cache
expiry. This is a version-specific observation, not a public timing guarantee.

## Compatibility recheck on 2026-09-19

The installed desktop app was version `26.915.31945`, build `9922`, with bundled
Codex CLI `0.155.0-alpha.9.2`. Its newly provided external socket existed. The
separate MCP probe initialized, but tool discovery ended with a closed app pipe;
the corresponding app log recorded `dynamic_app_tools_peer_rejected` with reason
`missing-code-signing-identity`. The selected bundled Node executable had a code
signature, so this observation does not establish that unsigned Node caused the
rejection. The exact peer-identity failure remains unresolved. No caller identity,
signature check, development flag or socket-selection rule was bypassed.

This failure is specific evidence about the external desktop-app-tools path,
not proof that every Codex adapter is unavailable. A normal Anywhere Plugin
session successfully ran `node_repl` twice, preserving a variable from 40 to 42,
and closed with confirmed cleanup. A separate current-source test explicitly
selected the updated bundled CLI and repeated 40 to 42 successfully. These use
the Codex app-server tool path without requesting a model turn; they do not prove
ordinary Chat creation or external desktop-app peer authorization.

The live installed Anywhere engine reported `0.1.0b1`, while the installed skill
package and development bundle used `0.2.0-alpha.1`. Keep engine, plugin and Codex
versions separate in acceptance records. The new diagnostic `codex_selection`
uses the execution resolver, including its explicit executable override, without
launching it. Resolution alone cannot certify compatibility or authentication.

Future Codex or ChatGPT updates are not covered by this one-version trial.
Subchat must remain experimental until its actual creation, submission and fresh
result-recovery path passes acceptance after an update. Existing independent
file/terminal and direct MCP operations should not require a working subchat
adapter. A rejected app connection is not a reason to replay an uncertain send.

## Thinking and resumed observation

The read deadline is not a generation deadline. An active/Thinking response,
including a partial draft, stays unconfirmed. This probe has no stop, cancel-
generation, resend, or compaction call. On a read deadline it ends only the local
observation wait. Resume with the same Chat ID, submitted user-message ID and
exact prompt file; do not create a replacement Chat or send the prompt again.
The user may choose a longer bounded read interval via `--wait-seconds`, or run
another observation interval later. No assumption is made about how long a model
should think. A synthetic regression keeps returning an active partial draft
through a timeout, then resumes reading the same submission and obtains its
completed answer. This verifies collector behavior, not provider generation.

### Repeatable transport failure results

A later recheck again initialized MCP and failed during `tools/list`. The probe
now returns `state=transport_failed`, a failure stage and content-free diagnostic
codes, with a nonzero exit status. The live result was catalog-stage RPC `-32603`.
It does not infer peer authentication as the cause from a closed pipe alone.
Expected nested MCP/OS transport exceptions are classified using the existing
plugin diagnostics helper; unexpected programming errors still propagate.
No submission is attempted and no alternative socket or caller identity is used.
This improves automation of a failed compatibility check; it does not repair the
external app transport. Diagnostic tests cover payload omission, stage/code
retention, timeout and unknown-error propagation.

The overall CLI deadline also bounds startup, catalog discovery and cleanup.
Expiry returns `state=probe_timeout` and exits nonzero, instead of escaping as an
unstructured timeout traceback. It neither resends nor requests cancellation of
Chat generation. This result does not identify which transport stage stalled.
A hanging local probe test verifies deadline cleanup; a separate test preserves
normal results and propagation of programming errors.


## Independent browser startup, 2026-09-20 JST

A dedicated persistent Chrome profile was launched through Playwright CLI 0.1.21,
without the Codex app socket, executor identity or a copied browser login. The
CLI returned a running browser and a current accessibility snapshot from
`https://chatgpt.com/`. The snapshot explicitly contained Login buttons and a
logged-out composer. No message was submitted and no model/effort availability
was inferred from this unauthenticated page.

This proves that the independent browser control path starts in this environment.
The next gate is a one-time interactive login into this dedicated profile, then
fresh ordinary-Chat creation, model/effort discovery and receipt recovery. The
user was asked to log in; authentication and subscription-backed generation are
not yet verified. The ordinary existing Chrome profile was not modified.

The existing npm cache produced EACCES during CLI resolution. A separate temporary
npm cache allowed the probe to run; no cache ownership or global package settings
were changed. This is a development probe prerequisite, not the intended product
installation flow. No production dependency or advertised subchat capability was
added on the strength of browser startup alone.

## DOM submission receipt and owner-read correlation, 2026-09-20 JST

The dedicated browser's current transcript has no `data-message-id` attributes.
Its `data-turn-key`, however, matched the submitted user-message ID returned by
an independent owning-Codex `read_thread` call for the same persisted Chat.
`subchat_submission.js` now extracts that observed identity from a unique matching
user bubble in the explicitly selected conversation. It excludes caller-supplied
previous IDs and rejects duplicate IDs, duplicate matching submissions, nested or
unsupported turns, quoted assistant text, and provisional or different URLs.
It returns neither assistant text nor a completion claim. Virtualized/missing
rows remain unconfirmed; absence must never trigger another submission.

The optional read-only CLI owns a dedicated minimized browser process:

```sh
uv run --extra browser python scripts/probe_subchat_receipt.py \
  --profile /absolute/dedicated/profile \
  --conversation-url https://chatgpt.com/c/PERSISTED-CONVERSATION-ID \
  --expected-prompt-file /absolute/exact-prompt.txt
```

Use only the authorized dedicated profile with its other browser process closed.
The URL must be the actual persisted ordinary Chat URL, not a temporary local
route. The prompt file must match exactly, including newlines. Repeat
`--previous-user-id` for known prior submissions when collecting a follow-up.
A successful result is `submission_observed`, `resend=false`, and
`completion_confirmed=false`. This reads an already selected conversation; it
neither creates a Chat nor reconstructs a lost conversation URL. It never
selects a model, sends, stops generation, or changes another browser's profile.

In live acceptance, this CLI recovered the submitted user ID from the persisted
single-turn test Chat after browser restart. Feeding the observed conversation
and user IDs plus the exact prompt into `matching_reply`, with a fresh owning-app
read, recovered the distinct assistant ID and the exact 32-character answer.
The two paths agreed without another send. This is a composed browser/owning-app
acceptance, not proof that the external Codex app transport is repaired. A
standalone adapter still needs a working result-read transport and lifecycle,
submission persistence, model selection, and uncertain-send recovery integration.

The real-DOM fixture exercises the extractor against local intercepted pages;
it uses no account or network. It skips without the optional browser dependency
or Chrome and does not replace the authenticated live trial. The current DOM
contract is version-sensitive and deliberately fails unconfirmed on changed
markup; its turn key is an observation, not a documented provider API identifier.

## Multiline serialization trial, 2026-09-20 JST

The dedicated authenticated ordinary Chat accepted a single test containing
Japanese, emoji, a Windows path, fenced Python, Markdown punctuation and line
breaks with GPT-5.6 Sol / medium explicitly observed before submission. Native
HTML editing with text nodes, hard breaks and the editor's literal-paste mark
preserved the exact submitted text. An independent owning-app read returned the
original text and a completed answer `AC_SUBCHAT_LITERAL_20260920_01`; no resend
was needed. The browser remained open after this trial.

In contrast, whole-text fill/insert produced separate paragraphs and extra blank
lines in `innerText`; line-by-line Shift+Enter triggered code-block formatting.
Those preliminary trials stopped before sending. The experimental
`subchat_input.js` implements the successful native-edit shape with empty-editor,
focus and selection checks. It does not send, select a model, or claim that a
draft observation establishes saved-message serialization. A local real-browser
test covers literal markup, internal empty lines and rejection of an existing
draft. Authenticated trial evidence is separate from that fixture.

The transcript renders the submitted Markdown: its visible `innerText` omits
fences even though the independent saved message retains them. Consequently the
current exact-visible-bubble receipt extractor does **not** establish submission
identity for this multiline case. Production integration must resolve this
without stripping punctuation or accepting a merely similar user message.

Follow-up: `subchat_copy.js` uses the selected user message's Copy action to
obtain its original text. The clipboard write is captured within the dedicated
page and restored in `finally`; the successful live trial did not replace the
OS clipboard. The experimental receipt CLI exposes this as `--copy-message`.
Unlike the default DOM-only mode, this clicks a UI action. It never sends or
regenerates a message. Copy behavior is UI-version-sensitive; a changed or delayed
implementation needs revalidation, not a claim of a stable clipboard-free API.

Recovery compares exact original text for every candidate outside the supplied
baseline, bounds candidate count, and rejects missing copies, ambiguous matches,
changed turn identities or a changed transcript. The live multiline message was
recovered with the same user ID as the independent owning-app read. Local tests
also cover timeout cleanup and identity changes during the copy action. This is
still an experimental receipt path, not the completed production subchat lifecycle.

## Durable lifecycle implementation boundary

`subchat_state.py` stores intermediate records in the existing ledger database.
It persists the send stage before invoking an adapter, binds records to the
transport owner, retains selected model/effort and exact prompt, and refuses
changed arguments or replacement answers. A restart does not reset a possibly
sent record to prepared. These records contain conversation content in the same
private state directory as operation results; they are not diagnostic history.

`subchat.py` composes that storage with an adapter contract for send, submission
lookup and answer reading. A duplicate request returns its saved stage without
calling send. An uncertain send raises a dedicated outcome-unknown error;
Thinking/missing answer observation leaves the submitted stage intact. Recovery
checks prompt and conversation/message identity before committing an answer.
There is no stop-generation operation and no automatic resend scheduler.

Six focused tests use real SQLite close/reopen and a deterministic effectful
backend to cover response loss, restart, repeated Thinking reads, completion,
cancellation, ownership and mismatched answer identity. They do not establish
browser compatibility or CoS worker connectivity. The lifecycle is not yet
registered in Engine/MCP: an actual adapter and outcome-unknown mapping must be
connected and accepted before advertising a callable production subchat tool.

For a follow-up, the requested conversation is stored at preparation, before
dispatch. Reusing its operation ID with a different conversation or as a new-Chat
request is rejected. The requested identity is kept separately from the observed
conversation created by a new-Chat send, so later receipt recovery does not change
the original request's meaning.

## Browser answer extraction

The same selected-turn Copy path now reads assistant text only when exactly one
assistant content unit and visible final-response controls are present and no
visible stop-generation control or busy turn is observed. The gate is checked
again after copying. It returns the DOM's `answer_reference`, explicitly not an
invented backend message ID, and labels its evidence `visible_response_controls`.
This UI evidence must not be described as a native provider completion event.

The authenticated marker trial returned the expected answer through this path.
During initial diagnosis, the answer Copy action used `clipboard.write` with
plain-text and HTML items, unlike the previously tested user `writeText` path.
That initial attempt copied the synthetic marker to the OS clipboard; its prior
contents were not read or restored. The helper now captures/restores both methods.
The real-browser fixture verifies zero calls to either OS-writing spy and rejects
an answer while the Stop generating control is present. Delayed/changing UI and
long live Thinking still require end-to-end adapter acceptance.

### Anywhere-owned browser lifecycle integration (2026-09-20)

The development adapter `scripts/subchat_browser_backend.py` implements the
existing Anywhere `Subchats` backend protocol against an explicitly supplied,
dedicated browser context. It creates an ordinary Chat, selects the requested
model and effort from observed UI labels, submits literal text once, and recovers
receipts and answers through the exact-message copy probes. It does not own or
close the supplied browser context. It is not registered as a production tool.

An authenticated fresh-Chat trial selected GPT-5.6 Sol and the observed medium
effort label, obtained the requested synthetic marker, and independently checked
the ordinary Chat through the owning client's read operation. Closing and
reopening SQLite recovered the saved answer without another submission. This
establishes completed-result persistence, not pending-turn browser restart
recovery.

`tests/test_subchat_browser_backend.py` exercises this adapter in real Chrome
with all requests fulfilled by an offline DOM fixture: dynamic model/effort
labels, literal Japanese multiline input, one send, repeated pending reads,
answer copy, SQLite reopen, and zero writes to the clipboard spies. The fixture
is not evidence of compatibility with future ChatGPT UI versions. Its response
explicitly declares UTF-8; the initial missing charset reproduced mojibake in the
fixture's JavaScript answer literal and was corrected without weakening the
text assertion.

Remaining gates include distinguishing preflight rejection from uncertain send,
checkpointing the new conversation before receipt loss, follow-up baselines,
pending-turn restart recovery, and product CLI/MCP integration. Answer references
are explicitly namespaced DOM content-unit references, not provider message IDs.

### Product direction: redesign for Anywhere

CoS is a source of requirements and failure cases, not a required runtime or
ownership authority. Subchat identity, operation ownership, delivery state, and
saved results belong to Anywhere's durable store. The browser adapter remains a
replaceable implementation of that contract. No synthetic CoS Prime identity or
CoS worker API is required. Ordinary Chat remains distinct from Work and paid API
backends. Requested models and effort labels come from live discovery; this
trial's selected model is an acceptance input, not a hardcoded model catalog.

### Preparation and dispatch boundary

The backend protocol now separates `prepare` (must not submit) from `send`.
Model/effort availability and literal draft preparation happen before the store
commits `sending`. A preparation failure leaves `prepared`, so the same immutable
request can be retried without claiming a possible submission. Once `sending` is
committed, exceptions/cancellation remain uncertain and never permit automatic
resend. A compare-and-swap still permits only one caller to enter that stage.
The browser adapter rechecks its page URL and exact draft immediately before the
send click; it does not silently replace intervening user input.

The 10 related lifecycle/state/browser tests passed after this change, including
an unavailable-model preflight with zero sends and a changed-draft rejection in
real Chrome against the offline fixture. Existing-conversation baseline storage,
shared work context, and pending browser restart acceptance remain outstanding.

### Follow-up baseline integration

The preparation contract returns the observed prior turn IDs. `begin_send`
persists them before dispatch; recovery excludes those turns even if they contain
the same prompt, and the store refuses to accept a prior user-message ID as the
new receipt. Existing-conversation preparation navigates only to a validated
ordinary Chat URL and requires an idle empty composer. Dispatch rechecks both
exact input and the observed history before clicking Send.

Browser fixture integration now covers fresh and existing conversations. Twelve
related tests passed, including persisted baselines after SQLite reopen and an
older JSON record without the new field. The compare-and-swap compares the
validated old record but matches the original stored JSON bytes, allowing additive
model defaults without incorrectly treating an older serialization as a race.
Real Chat follow-up acceptance and shared workspace tool access remain separate
gates; the fixture does not prove those product requirements.

### Actual plugin work and follow-up UI defect

A new ordinary Chat selected GPT-5.6 Sol / medium and used the installed Anywhere
HTTP plugin to create and execute a Python file in a dedicated temporary workspace.
The returned result was `{"sum":16,"mean":5.333333333333333}`. The file was
independently read and executed on the host with the same result. The Chat reported
installed version `0.2.0a1`; that is the existing file/terminal runtime, not proof
that the development subchat adapter is installed as a product tool.

The first follow-up preparation failed before dispatch: existing conversation
pages have no new-Chat Chat/Work toggle. Requiring that toggle on both page types
was incorrect. The adapter now requires the toggle only for a fresh Chat; an
existing conversation requires the exact validated `/c/` URL and ordinary Chat
composer markup. The offline existing-conversation fixture removes that toggle
and exercises the corrected path. Retrying the same prepared operation succeeded
in observing a new user-message receipt, with the prior message ID persisted as
its baseline. Completion of the requested file update is a separate observation.

The follow-up then completed: the Chat read the existing file, reported using its
SHA-256 for replacement, changed values to `[3,5,8,10]`, added `count`, and returned
`{"sum":26,"mean":6.5,"count":4}` with exit code zero. Independent host read and
execution confirmed that updated code and result. This is actual same-conversation
file/terminal continuity for the selected model, not a blanket pass for all models,
parent/subchat coordination, or simultaneous editing by multiple Chats.

## Packaged local JSON-lines controller

Install the optional `browser` extra and Chrome. `anywhere-subchat` accepts
`--browser-profile` (an explicitly selected, dedicated logged-in Chrome profile)
and `--state-dir` (its local ledger directory). Do not use the ordinary personal
Chrome profile or open the same dedicated profile in another process.

The process reads one JSON object per stdin line and writes one JSON result per
stdout line. Keep stdin open between commands: the same browser remains open.
The browser starts lazily on the first operation that requires it. Saved status,
completed-answer recovery and unsent cancellation do not launch Chrome or touch
its profile. Once started, browser operations reuse the dedicated context.
First launch still uses headed Chrome and may display its window; this is not a
fully browser-free or guaranteed background-only Chat creation path.
EOF explicitly shuts down this controller and its browser, if started. This entry point is
local-only; it is not an authenticated HTTP/MCP endpoint or a shared-work router.

HTTP access diagnostics are separate from generation state. An observed 401
returns `authentication_required`; an observed 403 returns `access_denied` (not
proof of an expired login). CLI uses these as `state`; MCP uses `error_code`.
Both report `automatic_retry=false`. Restore the dedicated account's login/access,
then recover the saved operation ID instead of submitting the prompt again.
The ledger is preserved. A timeout, 429, server failure or missing authenticated
request is not relabeled as a login failure. A logged-out page that makes no
observed API request may still produce a generic observation error; first-login
onboarding is not completed by these diagnostics.

Only observed authorization/account/language headers are held in process memory;
Cookies remain in the dedicated persistent browser context. After controller
restart, the same profile may retain login, but headers must be observed again.
Profile persistence is not a guarantee that the remote session remains valid.
This path does not import Codex credentials or refresh tokens independently.

A `send` command requires `action`, a caller-generated 32-character lowercase
hexadecimal `operation_id`, exact `prompt`, and observed `model` and `effort`
labels. Optional `conversation_id` targets a follow-up. Obtain current labels
using the packaged catalog probe (`python -m
anywhere_computer.subchat_browser.catalog --profile PATH --headed`); do not
hard-code a model or silently substitute one. Close that probe before starting
the controller against the same profile.

`recover` and `status` commands take only `action` and `operation_id`. Poll
`recover` while the state is `sending` or `submitted`; it never stops Thinking.
A `submission_unconfirmed` error is a recovery instruction, not permission to
create another send ID. Repeating `send` with the original ID does not resubmit
an already reserved operation. Provider error bodies are not printed, because
they may contain account or page data. `command_failed` with its error type
requires diagnosis; it is not proof that no prior operation ran.

The packaged adapter uses browser UI and therefore still depends on the provider's
current markup and authentication. Packaging does not turn the Chat subscription
into an official API. Shared device/workspace binding and remote MCP registration
remain separate unfinished work.

## Stdio MCP entry

Add `--mcp` to the same `anywhere-subchat` invocation to use MCP framing instead
of the JSON-lines command format. This reuses Anywhere's MCP session and stdio
transport. A configured direct-MCP session can launch that executable with its
explicit dedicated profile and ledger paths; callers do not pass a profile in
individual tool requests. Do not expose this local stdio endpoint as an
unauthenticated network service.

The tools are `subchat_send`, `subchat_recover`, and `subchat_status`. Select a
32-character lowercase hexadecimal `request_id` before sending. Recover using
`operation_id` equal to that send ID; a recovery call's own MCP request ID is a
different transport operation. Successful tool handling can return a pending
submission in `data.state`; inspect it rather than assuming the Chat answer is
finished. `unknown` after a send means recover without creating another send ID.
Browser interactions are serialized to avoid interleaved drafts and clipboard
interception. This endpoint still does not assign file permissions, bind shared
workspaces, or automatically enable an Anywhere connection inside the Chat.

The official MCP SDK acceptance uses a real subprocess and SQLite across process
restarts with a deterministic provider fixture. It verifies protocol framing,
exact input, result recovery and single dispatch; it is not evidence of live
ChatGPT UI or platform-policy behavior.

## Authenticated packaged MCP acceptance, 2026-09-20

A real official-MCP-SDK client launched `anywhere-subchat --mcp` in a separate
process against the previously authenticated dedicated profile. The prior probe
released that profile once before this intentional handoff; ordinary personal
Chrome was not used. Initialization and tool discovery succeeded. The process
read an existing completed submission from the same SQLite ledger, then created
a fresh ordinary Chat through `subchat_send` and recovered its answer through
`subchat_recover` without a send replay or a generation-stop action.

The live menu listed GPT-5.6 Sol; selecting it exposed the observed medium label
`中程度、5 件中 2 番目。`. Those exact values were supplied by the test client,
not defaults in the implementation. Send operation:
`93350f1e2ed24f1a9671183db351a278`; conversation:
`6aaf69d0-f98c-83e8-9a50-165bc919a56c`; user message:
`b1c8a089-71b4-451b-b070-a4456b659690`.

The answer reported Anywhere version 0.2.0a1, Darwin, and installed runtime
`a82ce393e115c5b1025a0c8777823d74357ee120f660892d27aca78d71084c53`, together with
the existing test Python file's exact contents and SHA-256
`7250838d3ea07e80080e512c6ce2e94ddc2a2b1543155a5a773e1bb959ff0a3e`.
An independent host hash matched. The owning app's conversation read separately
confirmed kind `chatgpt`, completed state, the same user message, and the answer
under provider message `8f84b661-7603-42af-b1db-b63033b48bf0`. That read did not
include individual plugin RPC traces; the tool-use description remains the
answer's report, supported by the independently matched file contents/hash.
The installed runtime above is not the newly built development runtime.

No file mutation or terminal execution was requested in this trial. It validates
the new packaged MCP-to-ordinary-Chat delivery/recovery path, not all plugin
capabilities, automatic shared-work routing, or general restart/update acceptance.
After completion the client intentionally ended its stdio session and browser.

One live catalog limitation was also observed: with the automatic `最新` model
selected, effort discovery was unconfirmed and the combined catalog call did not
return its already observed model list. Selecting an explicitly observed model
with an effort control worked. Partial model availability should remain visible;
this limitation is still open rather than being treated as no models available.

## Model catalog through MCP

The configured browser-backed stdio entry now also exposes `subchat_catalog`
with empty arguments. It observes current model labels and the selected model's
effort choices without submitting a message. A `catalog_partial` result retains
the verified `models` list and reports `efforts_for_selected_model` as unconfirmed.
It does not mean that no models exist, or that arbitrary effort values are valid.
The model selection is rechecked before returning either a full or partial
catalog, and the picker is closed afterward. Do not infer that every model
supports the same effort options. Selecting a model without a verified effort
control for sending remains unsupported by the present sender.

This resolves the model-list loss described in the authenticated acceptance
record above. The regression removes the selected model's effort control in a
real Chrome DOM fixture: the previous implementation returned no model list;
the corrected implementation preserves it and reports partial availability.

Live debugging also confirmed that reopening the picker can preserve the model
list view. In that state, the effort-view toggle matched `:visible` but had inert
and aria-hidden ancestors; attempting to click it timed out. Catalog collection
now re-observes the list before deciding whether a view switch is necessary.
The regression covers remembered model-list view with an inert toggle. An
unobserved effort control is not evidence that the model lacks effort support.

### HTTP catalog observation

`subchat_catalog({"source":"http"})` is an opt-in alternative to the default
UI catalog. Omit `model`: this observes the browser app's own authenticated
`/backend-api/models` response without selecting a model or moving an effort
slider. It returns version groups with separate model IDs, display generations,
efforts and availability. Work presets are excluded; duplicate identities,
unknown model references and changed/missing required schema are rejected.
An HTTP 200 without an observed Authorization header is insufficient because a
cookie-only request returned a reduced catalog in the live trial.

This path still opens and closes its own observation tab in the configured
dedicated browser; it is not a browser-free client or a guarantee that the OS
will never activate a window. It does not copy credentials, change the picker
default or silently fall back to UI interactions. Transport model IDs are not
accepted as UI labels by the current `subchat_send`; its exact model/effort
verification remains unchanged. A 20-second bound covers lazy browser startup,
observation-tab creation and HTTP collection, followed by at most five seconds
to close that owned tab. Startup timeout releases the factory lock; it does not
claim that an independently launched browser process has exited.
Controlled real-browser tests cover authenticated and cookie-only responses,
zero send/picker operations and preservation of unrelated tabs. Live catalog
GET evidence is recorded separately in the dated shared-work acceptance file.

### Bounded answer waiting

The local MCP adapter adds `subchat_wait(operation_id, wait_ms=1000)`, bounded
at 10 seconds to leave headroom under direct MCP's 30-second read deadline.
Longer requests are rejected before browser observation, not silently clamped.
It returns a completed tool response containing the saved
submission state; `submitted`/`sending` still mean no final answer was confirmed.
Call again with the same submission ID when more waiting is useful. It does not
stop generation, select another model, or resend. The browser lock covers only
individual observations, not the intervals while a model is Thinking, so other
subchat requests can progress. Provider errors remain failures and are not
silently converted to normal timeout/pending results.

Deterministic lifecycle/MCP tests verify pending timeout, another catalog call
while waiting, later answer recovery, and one send only. These tests use a
controlled provider and real SQLite; they do not establish live Chat scheduling
or immediate steering of another active Codex turn. The existing Codex app
message tool does not expose a queue/steer switch. A future delivery-mode feature
must verify the owning runtime and accepted turn ID before claiming immediate
delivery; it must not emulate steering by interrupting or resending.

### Receipt waiting and the pre-identity recovery limit

Review from the ordinary Chat `競合調査と実装検討` identified a distinct bottleneck:
submission held the browser lock while waiting up to 120 seconds for a receipt,
not for generation completion. The browser adapter now clicks Send once and
observes once. No receipt yet returns durable `sending`; callers use recover/wait
without another send. Input and individual observations remain serialized.

If a new conversation's process disappears after click but before its conversation
ID is persisted, a fresh adapter has neither its page nor a URL to reopen.
`Sending` with no conversation ID therefore remains unresolved. Duplicate sends
are still prevented, but automatic receipt recovery is not guaranteed. Preserve
this state for manual reconciliation; do not enumerate unrelated history or
send again. This differs from reopening a known `submitted` conversation while
its answer is still Thinking, which has a saved identity to recover.

Real DOM fixture tests defer the first receipt observation after an actual click,
verify `sending`, show that a fresh adapter cannot invent the unknown conversation,
and recover with the original page without a second click. Existing tests cover
known-identity restart and final answer recovery. They do not simulate the live
Chat service or prove recovery from every crash point.

### Work provenance on submission receipts

`subchat_send` and the JSON-lines `send` command accept optional `work_context`:

```json
{"task_id":"ffffffffffffffffffffffffffffffff","device_id":"local",
 "workspace":"/example/project",
 "inputs":[{"reference":"source.py","sha256":"0000000000000000000000000000000000000000000000000000000000000000"}]}
```

These are caller-supplied labels/references, not verified filesystem facts or an
access grant. The controller does not open those paths, verify the hashes,
change cwd, enable tools, or insert context into the exact prompt. Describe the
work explicitly in the prompt; retrieve its recorded provenance via status,
recover, or wait after reconnecting. A task ID groups work descriptively; it is
not an authenticated child identity and does not isolate sibling Chats.

Optional `parent_operation_id` must refer to an existing submission owned by the
same transport owner. Repeating a submission ID with different context is rejected
before backend input. Existing receipts without context continue to load. The
local stdio endpoint still has owner=None; it does not establish per-child
credentials or read-only isolation. Up to 32 input references are stored.

### Explicit queued follow-ups (development)

`subchat_message` accepts `mode`, `target_operation_id`, and the exact `prompt`;
use its `request_id` as the new message operation identity. `mode: queue` binds
the follow-up to an already confirmed subchat submission and inherits its exact
model/effort and descriptive work context. It persists immediately as `queued`.
The JSON-lines CLI uses the same durable queue, without starting Chrome:

```json
{"action":"queue","operation_id":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","target_operation_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","prompt":"Review the result and report remaining issues."}
```

The target must already have a confirmed conversation and user message. Model,
effort, and context are inherited; CLI queue input rejects overrides. Repeating
the same request returns the saved record, while changing its prompt or target
under the same operation ID fails. Use CLI `recover` on the new operation to
progress delivery; this may open the dedicated browser. Queue registration is
not immediate steering and does not stop generation or send in the background.

Call `subchat_recover` or bounded `subchat_wait` on the new operation to progress
it: merely creating the record does not start a background dispatcher. Once
a recover/wait call starts queued preparation, that work survives the observer
timeout in the same MCP process. Repeated observations share the same pending
work (at most eight recoveries); closing the session cancels and joins it before
closing browser resources. Process loss still requires reconciliation. The target's answer must complete before dispatch. If a newer visible
turn appeared, the browser rejects the stale target before entering the draft.
The current API permits competing queued proposals, not an implicit FIFO chain;
a proposal is never automatically retargeted after another follow-up wins.

`queued` means local acceptance, `sending` means reserved/possibly sent,
`submitted` means the external message was observed, and `completed` means an
answer was observed. Receipt is not an independent consumption acknowledgement.
Unknown sends stay reserved across restart and are never automatically replayed.
Same-ID changes to target/prompt are rejected. Two dispatchers cannot claim the
same saved message for sending twice. Browser draft preparation can still need
manual reconciliation after a pre-dispatch interruption.

`mode: steer` currently returns `error_code: unsupported`, `dispatched: false`,
and `queued: false`. No ordinary Chat immediate-steering transport has been
verified. This explicit rejection is not completion of the requested live steer
feature, and does not imply that the Codex desktop message tool can steer its
active turn. Queue is never substituted for steer; Stop is never used.
The browser also checks for generation immediately before ordinary Send, so a
prepared idle submission does not knowingly become a provider-side queued input.
UI checks cannot supply a server-side atomic expected-turn guarantee.

These are local stdio controls and inherit the existing operator connection.
They do not establish a child principal, revoke a connector grant, or provide
read-only isolation. Remote delegated-child authorization remains separate work.

### Tool-boundary delivery: source audit and acceptance gate

The CoS source audit is pinned to `8f76ccc790917b01ee758da6687a1cf9b576ba8a`
(package 2.1.14), not a claim about a future release. The supplied audit memo
has SHA-256 `d11bbe37b765b83d1da04d9bef8734a61507eae662e029fd0019d0e66d7af148`.
The reviewed paths are:

- `src/main/session/input.ts`, `enqueueInput` and `offerToolInput`: save input,
  bind explicit tool delivery to a turn, then select input for an eligible reply.
- `src/main/mcp/kernel.ts`, `dispatchTracked`: append input to `ToolResult.content`
  and, for core structured results, `supplemental_context`.
- `input.ts`, `toolInputReceipt`: a later invocation start in the same mapped
  session/conversation is treated as receipt. This is inferred receipt, not an
  explicit acknowledgement of the input body or proof that the model consumed it.
- `extension/content.js`, `acceptDesktopInput`: a separate conditional direct-turn
  path stops generation and sends a new user message. Do not adopt this as a
  transparent replacement for non-interrupting delivery.

These are distinct capabilities: `queue_after_turn`, `message_at_tool_boundary`,
`native_steer`, and `interrupt_then_send`. They must not silently substitute for
one another. Only queued follow-ups are currently implemented here; tool-boundary
message delivery remains an acceptance-gated design, not an advertised capability.

CoS correlates an inbound HTTP request header with companion-observed conversation
metadata. That mechanism is provider/companion-specific, not standard MCP child
identity. Anywhere's current authenticated grant does not distinguish multiple
ordinary Chats sharing that grant. Neither a client-supplied task/conversation ID
nor an MCP session ID alone establishes this binding. Do not add an unconditional
response-appending hook while that ambiguity remains. A dedicated restricted
connection is another possible design, but has not been qualified with ordinary
Chat here.

A future delivery record must retain the exact target, payload digest and observed
transport evidence. `awaiting_tool_boundary`, `offered`, response-written evidence,
and explicit receipt must be distinct; a later unrelated tool call cannot prove
consumption. Tool-free Thinking remains waiting, and an ended/mismatched target
must fail without next-turn delivery. An ambiguous write after a crash must not
trigger blind re-offer. Original tool data stays unchanged; text and structured
supplemental projections share a stable message identity and are not two sends.
Result recovery must not attach the same message to a new unrelated invocation.

Required integration cases before enabling this capability:

| Case | Required evidence |
| --- | --- |
| Missing binding or same-account different Chat | No supplemental message returned |
| Turn changes immediately before projection | Stale target; no fallback |
| Permission revoked during tool execution | Delivery reauthorized before projection |
| Response still pending while another call starts | No inferred consumption |
| Crash after offer/write; result recovery | Ambiguous outcome preserved, no blind replay |
| Text-only and structured-only clients | Same identity and content, original result preserved |
| Target ends without another tool | Undeliverable for that target, not sent next turn |

The external auditor reported seven isolated assertions against CoS's extracted
receipt predicate; they are not Anywhere HTTP integration or live Chat tests.
Live tool-boundary delivery and authenticated caller/turn correlation remain
unverified. The existing 74 subchat tests do not establish those capabilities.

### Cancel an unsent input

`subchat_cancel(operation_id=...)` (or JSON-lines CLI `action: cancel`) changes
only a local `queued` or `prepared` record to `cancelled`. Repeated cancellation
returns that same terminal record. Recovery, wait and replay of the original
queue request cannot revive it. Cancellation uses the same owner check and
compare-and-swap transition as send reservation: if cancellation wins during
preparation, Send is not called; once the record reaches `sending`, cancellation
is rejected because dispatch may already have happened.

This operation does not click Stop, cancel the parent answer, clear browser
drafts, or undo commands. Preparation may have left a draft that requires manual
inspection. It is deliberately separate from provider-side generation stopping.


### Codex message route recheck (2026-09-20)

The owning app exposes `send_message_to_thread(threadId, prompt, hostId?, model?,
thinking?)`. The observed schema has no delivery mode or expected-turn argument;
a successful call must not be advertised as verified immediate steering. The
read-only external probe of installed codex-app-tools 0.1.4 reached catalog
discovery but failed with RPC -32603. No send or inference was requested. The
redacted failure does not identify the root cause; previous endpoint/caller
failures are historical evidence, not a diagnosis of this new result. The probe
now includes the live send schema when discovery succeeds, without guessing
capabilities from version numbers.

Official Codex source pinned at
`5c5308fc9a9ee789049d646ef11e5400384b9c6f` implements `turn/steer` in
`codex-rs/app-server/src/request_processors/turn_processor.rs`. It requires an
expected active turn ID and rejects no-active-turn, mismatch, and non-steerable
review/compact turns. Its `tests/suite/v2/turn_steer.rs` checks a matching client
message notification after acceptance. These upstream tests were read, not run
here. This API targets Codex runtime turns, not ordinary ChatGPT conversations.

Anywhere's current codex_context adapter reads context; codex_plugins opens an
ephemeral tool context. Neither is a verified transport to an existing desktop
task's active turn. Starting a separate app-server is not evidence of access to
that turn. Keep desktop steering unavailable until the owning-runtime connection
and expected-turn behavior are verified; do not spoof executor identities,
replace ordinary Chat with Codex inference, or silently map steer to queue/Stop.

Source: https://github.com/openai/codex/blob/5c5308fc9a9ee789049d646ef11e5400384b9c6f/codex-rs/app-server/src/request_processors/turn_processor.rs#L1020

### Saved submission inventory

`subchat_list` and the JSON-lines CLI command `{"action":"list","limit":20}`
read saved submission summaries without browser interaction. They return operation
IDs, saved state, model/effort and confirmed conversation IDs; prompts, answers and
potentially large work-context references are omitted. Use status/recover on the
selected ID for details. This is saved state, not a fresh provider observation.

Results are newest-created first. Supply the returned `next_before` as `before`
to retrieve older records; `null` marks the end. The limit is 1–100. Pagination
filters the authenticated store owner before selecting records. The local stdio
adapter retains owner=None and does not create child-specific authorization.
The existing ledger is reused; no browser or second task database is introduced.
Saved list/status, local queue/cancel, and recovery of already terminal records
also work without the optional Playwright dependency installed. The browser
extra is loaded only when an operation actually requests browser access; this
does not make live Chat send/recovery browser-free.

Follow-up preparation reuses a unique, open conversation tab already owned by
the same adapter without reloading it. It does not discover or claim arbitrary
user tabs. A draft or active generation still prevents preparation; ambiguous
owned matches fail rather than opening another copy. New conversations and
recovery in a fresh adapter can still open tabs. This reduces avoidable tab
creation, but does not guarantee background window behavior or browser-free
operation on every browser/OS.

### Experimental saved HTTP answer recovery

`anywhere-subchat --http-read --minimized --browser-profile … --state-dir … [--mcp]`
bootstraps from the dedicated browser application's authenticated conversation-history
GET. It retains the observed authorization, account and language headers in
process memory, bound to that authenticated session. Subsequent CLI reads use an
independent HTTP client without copying cookies, opening tabs, navigating or
inspecting DOM. Directly constructed adapters use the browser context's HTTP
client unless supplied an independent request factory; see
[Standalone read transport](#standalone-read-transport) for lifecycle and evidence.
No credentials are written to the ledger, exported, or returned to the caller.
The initial owned observation tab is closed with bounded cleanup; existing
submission tabs are not reloaded. This is a browser-session-backed HTTP reader,
not a browser-independent login client or a public provider API.

Only the fixed HTTPS conversation-history GET is requested. Redirects and
transport retries are disabled. A 401 clears cached authorization and rejects all
later reads. A 403 from an authenticated HTTP read rejects only that exact resource
URL; other conversation
and catalog reads remain available. Repeated reads of the rejected scope make no
requests or new tabs. Initial browser-bootstrap rejection remains session-wide
because no authenticated HTTP session has been established. Repair login/access
explicitly and restart the adapter with the same
profile and state directory to bootstrap a new reader; recover the original
operation ID rather than sending again. Restarting does not itself repair access.
Other failures do not cause a generation replay or automatic browser fallback.
The overall read remains bounded and response objects are disposed after use.
`--minimized` verifies OS window minimization before page work, using the existing
minimization helper. If verification fails, no Chat page work starts. Initial
window creation may still briefly show on some desktops; it is not headless mode.

The response must identify the exact conversation and original user message,
match the original prompt, and provide matching request, exchange and working
turn identities for exactly one final assistant response. Normal completion
requires nonempty text, successful status, end-turn, completeness and an observed
`finish_details.type=stop`. Unknown or missing evidence remains pending. An
`interrupted` finish returns the explicit `reply_interrupted` CLI state or MCP
error code, with automatic retry disabled. Provider exception details are not
returned. The saved submission remains submitted, cannot be resent, and cannot release a
queued follow-up as though it completed. No DOM fallback is used in this mode.
Pagination without the original input, alternative finals and future schemas
are not guessed. The default DOM path remains available without this option.

Live read-only history inspection on 2026-09-20 found that a manually stopped
response retained `finished_successfully`, `end_turn=true` and `is_complete=true`,
but had `finish_details={type: interrupted, reason: client_stopped}`. Normal
answers had finish type `stop`. User and assistant `turn_id` differed; their
request/exchange/working-turn identifiers matched. This is provider observation,
not authenticated child identity or an official API stability guarantee.


Live production-reader acceptance (2026-09-20): an existing ordinary Chat's
`AC_MODEL_EFFORT_20260920` final answer and exact message ID were recovered via
`BrowserSubchatBackend(http_read=True)`. After initial bootstrap the test forbade
`context.new_page`; two further reads still returned the identical result and
left the page count unchanged. This used the logged-in dedicated Chrome profile
with its window minimized, not a fixture or the normal user Chrome profile.
It verifies saved-answer retrieval, not HTTP generation dispatch or uninterrupted
operation across all future provider changes. Controlled real HTTP transport tests
separately cover redirect refusal, authorization expiry and explicit rebootstrap.

The actual `anywhere-subchat --http-read --minimized` CLI was also run in a fresh
isolated ledger against that saved conversation. It returned `completed`, the
exact answer text, and exit 0. The repeated-read regression fails on pre-change
source because every recovery navigates; it passes with the HTTP reader.


### Repeated HTTP model discovery

`subchat_http_catalog` now uses the same browser-session HTTP reader as saved
answer recovery. The first catalog request observes the application's exact
models URL (including query parameters); later calls perform only that GET.
Observed authorization, account selection and language headers remain in memory;
unrelated browser headers are not copied. The result source is
`browser_session_http`. Model IDs, display labels and thinking-effort values
remain separate, dynamic fields; Work models remain excluded and unavailable
choices are not silently replaced. Generation still requires the verified UI
selection path (`send_requires_ui_labels=true`).

In live tests, authorization alone preserved identities but changed Japanese
labels to English. Preserving the observed `oai-language` produced exact catalog
equality. The production adapter subsequently read the catalog twice and then a
known final answer with `context.new_page` forbidden after bootstrap. All reads
succeeded with no page-count change. This does not verify switching accounts
mid-session. Controlled transport tests require the observed account/language
headers, reject unrelated-header forwarding, and exercise the same fault matrix
for both catalog and history without duplicating a second transport implementation.

### Explicit send resources (experimental)

`subchat_send` and JSON-lines `send` accept optional `resources` with
`attachments` and `plugins` arrays. Attachments identify files already uploaded
in the selected Chat account (`id`, optional `library_file_id`, `name`,
`mime_type`, `size`). Plugins require the exact observed `label`, `plugin://` URI
and `system_hint` from that account's successful @ selection. These are content
references, not credentials, permission grants or a plugin-discovery service.
Local file paths are not attachment IDs; this API does not upload them.

The browser still selects the observed model/effort and prepares generation.
For exactly one matching generation POST, the adapter adds attachment metadata,
plugin hints and the mention prefix to the browser-authenticated request. It
preserves provider preparation/authentication and uses explicit request
continuation. It does not create an independent HTTP login or replay a prepared
request. Unexpected payloads, conversation changes and pre-existing resource
selections fail closed. `--http-read` is required: recover accepts a receipt only
when the server-saved prompt, complete attachment references and plugin hints
match, then uses the existing final-answer correlation. The resource envelope is
persisted with the operation ID; changing it cannot reuse that ID. Queue follow-ups
do not implicitly reattach resources. A resource mismatch remains unconfirmed,
never a successful send or permission to retry.

For shared work, include the selected device ID, absolute path and expected
SHA-256 in the prompt/work context. The selected Anywhere plugin must actually
read that device. Uploads are snapshots; paths refer to contents at read time.
Use existing hash-conditioned file edits or separate worktrees for collaborators,
not unconditional overwrites. Work context is provenance, not an OS sandbox or
per-child authorization. See the dated shared-work acceptance for existing
conflicting-edit tests.

Live acceptance on 2026-09-20:

- Two ordinary Chat tabs ran concurrently with distinct files and markers;
  answers were recovered in reverse submission order without mixing identities.
- A UI @ selection invoked the actual Anywhere `computer_status` tool. A separate
  Chat read `/tmp/ac-subchat-attachments-20260920/B.txt` through `files_read`, with
  returned content and SHA-256, proving the path alternative separately from upload.
- The first resource interception attempt using routing fallback dispatched but
  saved plain input without resources. Exact history verification rejected it;
  its uncertain ledger record was retained and was not resent. The later explicit
  request-continuation path passed on an existing conversation and on a new one.
  Do not infer that provider preparation alone caused the initial loss.
- New resource send `01b0cb2338b541bc9ee3331fe23b5d3b` reached ordinary conversation
  `6aafdd3f-3e48-83e8-a5a5-0dd9922c575f`, user message
  `9ca9ba33-eb73-4d39-9d23-7c7a13ab1ff7`, final
  `d13d042c-5704-450f-8b5d-ff842667cbc7`. Production HTTP recovery verified its input
  resources and returned `case_id=A_FILE_271, sum=87` with a file citation.
- The current tests separately exercise state persistence, public CLI/MCP
  forwarding, wrong-input/resource rejection and a real browser's draft/Send
  behavior with a controlled network boundary. They are not live-service tests.

This does not establish built-in @ feature selection, file upload by API,
provider-side generation stopping, native steer, or peer-to-peer Chat messaging.
Parent-mediated follow-ups are distinct from direct peer messages. UI and private
HTTP shapes may change; unsupported shapes must not fall back to a different
model, strip attachments or silently report success.

#### Coverage boundary for the next HTTP increments

| Workflow | Current implementation/evidence | Still required |
| --- | --- | --- |
| New Chat / follow-up | Guarded browser dispatch, durable identity, HTTP final recovery; live tested | Independent HTTP generation without browser preparation |
| Model / effort | Dynamic HTTP catalog; UI-selected values observed in generation POST | Explicit HTTP selection with observed availability and UI-equivalence tests |
| Existing uploaded file / @ plugin | Explicit resource envelope and saved-input verification; live tested | Dynamic plugin reference discovery and authenticated upload integration |
| Local path / shared editing | Actual plugin file reads; existing hash-conditioned edits and shared-file acceptance | More real parent/child editing tasks; paths alone confer no authorization |
| Thinking / final / interruption | Pending preserved; exact final correlation; interrupted output rejected | Rich progress projection and explicit provider stop operation |
| Queue / steer | Durable queue and local unsent cancellation; unsupported steer rejected | Verified non-interrupting delivery capability; never stop-and-send silently |
| Built-in @ features | UI can expose search/image features | Observe each feature's own payload; do not treat it as a plugin URI |
| Parallel / inter-chat work | Separate tabs/IDs; concurrent generation observed | Direct peer messaging not implemented; parent-mediated handoff verified below |
| Recovery | Same-ID dedupe, HTTP result recovery, no unknown-send replay | Crash before new conversation ID still needs manual reconciliation |

HTTP coverage is feature-specific. An observed URL or a successful POST is not
proof of equivalent behavior. Each increment requires matching saved input,
actual output/tool effects and failure behavior. Unknown UI/HTTP shapes remain
unsupported rather than being advertised as "all Chat features supported".

The subsequent real parent-Chat acceptance also completed. In parent conversation
`6aafd694-06b0-83e8-91e7-be5b4235defc`, the actual Anywhere direct-MCP tools launched
the local subchat CLI. New child operation `29fa01bc23de456729fa01bc23de4567`
recovered final `CHILD_FROM_CHAT_20260920` from new conversation
`6aafde1e-9b7c-83ee-987b-c5e863d45c52`. Parent relay operation
`13579bdf2468ace013579bdf2468ace0` delivered that result to the existing B Chat,
which retained its own B-file identity and sum 91. A separate child operation
`31415926535897932384626433832795` created the explicitly scoped temporary file
`/tmp/ac-subchat-collaboration-20260920.txt`. The parent independently read its
exact content `COLLAB_B_CREATED` and SHA-256
`fa603b3a3e96437eb680fc90e1d0b646e03d41921fa3b6f993af1b24e4a3931f`;
Codex then independently verified the same actual file and all three completed
ledger records. This proves parent-mediated handoff and a shared local artifact,
not direct peer delivery or conflict-free simultaneous editing.

Earlier parent attempts failed before dispatch because the dedicated new-Chat
composer contained a leftover test draft. The exact draft was preserved locally,
then explicitly cleared for this acceptance. The same prepared operation then
succeeded; no unknown send was replayed. Production code still preserves drafts
and requires inspection instead of deleting them automatically.


### Generated URL decorations during collective review

Real review prompts containing the PR URL exposed two separate serialization
boundaries. The ordinary-Chat editor generated an inline URL icon which added a
layout newline to `innerText`. The generation POST then serialized the URL as
`[URL](URL)`. Neither is a user-requested paragraph or a different destination.
The previous strict comparisons correctly stopped dispatch but prevented these
otherwise ordinary review prompts.

Draft verification now recognizes only the observed paragraph/span/line-break
shape and generated URL wrappers with exact label/target text. It does not trim
or collapse whitespace. The resource request guard accepts identical-label,
identical-target Markdown URL decoration only when it reduces exactly to the
prepared prompt; the outgoing resource envelope still contains the canonical
prompt. Wrong labels, destinations, changed text and added newlines fail.
Saved HTTP input and resource verification remain exact.

The real DOM regression fails with the previous implementation and passes with
the fix. The stopped draft was preserved separately; its unknown operation was
not retried. Two subsequent HTTP resource sends were also rejected before the
URL serialization correction; these remain unknown in the ledger rather than
being relabeled as delivered. This is not evidence of failed model reasoning.

A separate explicitly requested generation-stop acceptance observed
`POST /backend-api/stop_conversation` with `conversation_id` and
`exclude_async_types: []`. HTTP recovery then raised `SubchatInterrupted`.
This is a conversation-scoped observed UI request, not a turn-scoped steer API.
No public provider-stop API is implemented by this change.

Two real GPT-5.6 Sol / high review subchats subsequently completed through the
fixed resource API and HTTP answer recovery:

- Implementation review `f7d2ca098c4a4f3a9bddeaf80e97fe18`, conversation
  `6aafe320-2e84-83e8-9fc1-e149dca6ca94`, user
  `2808a2a8-d819-497b-b9e8-34a03b46a43d`, marker `COLLECTIVE_REVIEW_D`.
  It found the draft check's unconditional matching-innerText fast path could
  skip destination validation. The parent verified the code and added real DOM
  regressions for matching text with a different/conflicting destination, then
  restricted that fast path to drafts without links. The child's test command
  used an interpreter without pydantic; its claimed suite result is not counted.
  The parent used the repository `.venv` and passed all 145 subchat tests.
- Design review `02e71deb94e643a88b9498551388563f`, conversation
  `6aafe323-a588-83e8-9898-3a0b44ad7d3e`, user
  `62a76fcb-0868-4736-872f-a16a895e5f69`, marker `COLLECTIVE_REVIEW_E`.
  It proposed bounded collection of child results using the existing ledger.
  Its real `files_read` operation `1a0babe577284350aaeef4e8b87c7938` returned
  the repository `subchat.py`; the parent verified that tool result in history.

Both generations overlapped; their final IDs, markers and answers were recovered
separately without resending. During the longer tool-using review, the UI exposed
an activity turn key instead of the user-message ID. Receipt recovery stayed
pending until the user turn became observable again after completion. This is a
remaining observation-latency limitation, not a failed generation. Future review
prompts should pin the input revision and exact test interpreter so concurrent
parent fixes and environment mismatch do not make a child's report look current.

### Codex read parity and stopping before an answer

The owning Codex app's `read_thread` independently returned the two completed
review conversations above with kind `chatgpt`, the same user IDs and final
answer IDs (`7f812bfc-066a-4795-aa91-9e5bf9d8a111` and
`1829ae62-3247-4f09-8e98-02c90ee7aa17`), and matching final text. This establishes
read parity for these conversations, not universal freshness of the app's reader
or access from an external MCP process.

HTTP recovery accepts only the correlated, completed final answer. Stopping
before such an answer exists cannot produce a completed answer to retrieve.
Provider interruption is reported separately; a missing final remains pending or
requires reconciliation rather than being fabricated from progress. A dedicated
Thinking-stop attempt `b01574a65bae4628a05cf5daf03e1ece` had already completed
before the stop precondition was checked, so it does not count as a successful
Thinking-stop acceptance. No stop was issued against its completed answer.

A subsequent extreme-effort probe (`b56ee050fb644435b9bd2e54e5efa644`,
conversation `6aafe834-c5b4-83ee-aec2-c77786d2101c`) stopped while the public UI
showed Thinking and no final answer. An HTTP POST to the observed stop endpoint
returned 200; no Stop-button click was used. Fresh HTTP history contained an
assistant `reasoning_recap` with `end_turn=true` and
`reasoning_status=reasoning_cancelled`, matching the saved user's request,
exchange, and working-turn IDs. No final message existed. The owning Codex MCP
reader independently returned the same user ID
`38b3ba0c-729e-44be-b445-be6f3f87488f`, idle/completed, and no assistant answer.
Codex's completed turn therefore does not establish a completed answer, nor does
idle alone establish cancellation.

The original parser left this case pending. The corrected HTTP projection reports
`SubchatInterrupted` using the explicit correlated cancellation marker, without
requiring a local stop receipt or reading reasoning text. Fresh HTTP history from
the stopped conversation passed this check. Controlled regressions also reject
unrelated requests, missing identity, nonterminal recaps, unknown statuses, and
other content types. This supports detecting provider-recorded cancellation even
when a local stop receipt is absent; a human clicking Stop during Thinking has
not been separately exercised in this probe. The public provider-stop command is
still not implemented. Unknown/missing terminal evidence remains unresolved and
must not trigger automatic resending.

The supported output is final text. Visible progress summaries are a different,
not-yet-exposed output contract; nonpublic chain-of-thought is not a supported
retrieval feature. The Codex read parity above returned user/final messages, not
an internal reasoning trace. Generation still uses browser-assisted preparation;
HTTP history reads and a CLI/MCP send interface do not establish a browser-free
independent generation client.

### Upstream waiting/recovery review, 2026-09-20

The latest CoS commit observed through GitHub was
[`04c6a298`](https://github.com/totec448-spec/chat-on-steroids/commit/04c6a298078817cf25c197f2b8bb639f8239243c).
This refresh read the current issue bodies; it is not a fresh full-code audit of
that commit. Earlier source-audit findings remain scoped to their pinned versions.

- [#327](https://github.com/totec448-spec/chat-on-steroids/issues/327) reports a
  stale waiting state after interruption. Anywhere independently reproduced its
  own concrete variant: cancelled Thinking without a final message was pending.
  The correlated cancellation projection above fixes that observed variant; it
  does not claim to fix every stale-session cause described upstream.
- [#326](https://github.com/totec448-spec/chat-on-steroids/issues/326) reports
  indefinitely queued follow-ups. Anywhere preserves queued work when its parent
  is interrupted and returns `reply_interrupted` on recovery rather than sending
  it automatically. Broader queue diagnostics and explicit reconciliation remain
  separate work; no age-based resend or assumed delivery is introduced.
- [#336](https://github.com/totec448-spec/chat-on-steroids/issues/336) reports hours
  of repeated browser recovery without execution progress. Keep HTTP reads bounded
  and distinguish transport failure, unknown generation, and provider cancellation.
  Do not copy automatic reload/retry loops or equate recovery attempts with task
  progress. The reporter explicitly leaves the upstream root cause unresolved.

These reports reinforce existing contracts rather than justify another task
runtime. The accepted change reuses the current HTTP reader, interruption result,
CLI/MCP mapping and queue guard. No upstream code or automatic restart watchdog
was copied.

### User closes the dedicated browser

The adapter recognizes an observed context close or browser disconnect. After
authorization bootstrap, the CLI's independent HTTP client can still recover
known input/answer identities while that controller remains alive. An already
observed HTTP model catalog can also be read. Other browser-dependent work reports
`browser_closed`; this includes unbootstrapped reads and new send preparation.
Neither path launches a replacement or resends because the browser closed.
If browser-dependent work is needed, restart the controller with the same state
directory and dedicated profile, then recover the original operation IDs.
A known conversation can be re-observed; an unknown
conversation ID after a pre-receipt crash still needs explicit reconciliation.
Closing a browser does not prove that server-side generation stopped.

The original closure-error acceptance uses isolated Chrome and the SQLite store,
plus CLI/MCP error projection. Subsequent independent-client and live read-only
recovery evidence is recorded under [Standalone read transport](#standalone-read-transport).
Closing during an in-flight send can still yield `submission_unconfirmed`;
the durable reservation prevents replay. Explicit provider cancellation remains
`reply_interrupted`, separately from browser closure or a read timeout.


### Ordinary Chat audit follow-up (2026-09-21)

The parent recovered final reports from two ordinary Chat conversations:
- Product review: https://chatgpt.com/c/6aaff6f2-ee68-83e8-9212-fef4b9b8d44c
  (GPT-5.6 Sol, high; recovered with the saved operation/user/answer identity).
- Implementation audit: https://chatgpt.com/c/6aaff62a-99a0-83e8-88a1-d02b0fd918e4
  (observed generation model gpt-6-pro; final report read through Codex).

These are review inputs, not automatic proof of their technical claims. The
parent independently reproduced loss of typed browser/access errors during
preparation: three regression cases failed before the fix. Preparation now
preserves those actionable error types while keeping the record prepared and
sending nothing. The same errors during send still yield outcome-unknown,
retain the sending reservation and never permit automatic replay.

Outstanding audit candidates require separate reproduction and changes: short
wait calls cancelling slower observations; external receipt ownership across
operations; DOM-only completion after interruption; durable interruption
explanation; closed-browser diagnosis before a conversation ID exists. No child
permission isolation, browser-independent authentication or native GUI support
is claimed by this change.


### Short waits share observations

A caller's wait deadline does not cancel a sending/submitted observation. The
session shares one recovery task per operation, using the existing eight-task
limit and browser lock. Non-queued observations have a 25-second execution bound
after acquiring that lock. This bounds local receipt/answer retrieval, not model
Thinking time, and never sends a Stop request. Queue preparation retains its
existing provider bounds and durable send reservation.

Completed answers remain in the ledger. Late observation errors remain in the
session until the next recovery/wait collects them; they are not silently dropped
when the original caller timed out. At most eight uncollected/pending recoveries
are retained; collect existing IDs before adding more. Session close cancels and
joins outstanding work. This is not external MCP multiplexing or durable error
storage across process restarts.

The slow-observer regression failed on the previous implementation for answer,
authentication-error and session-close outcomes. It now verifies three short
waits share exactly one provider read, late results/errors remain observable,
and closing a session cancels that read without replaying the submission.

### Receipt ownership at mutation time

Within one saved owner namespace, distinct operations cannot newly claim the
same conversation/user-message pair or conversation/answer-message pair.
Validation and mutation run under one SQLite immediate transaction, including
local owner=None. A rejected receipt leaves its sending reservation intact;
a rejected answer leaves its submitted receipt intact. No provider retry occurs.
Different conversations and different owners remain separate namespaces.

This validates proposed mutations against existing records without deleting or
rewriting history. It does not retroactively repair duplicate records written by
older versions, or authenticate parent/child identities. Read-only inspection of
legacy records remains available. All writers must use this updated store; an
older concurrently running writer does not gain these checks automatically.

Tests reproduce the original duplicate claim and race two real SQLite connections
in separate threads. Exactly one receipt claim succeeds, including owner=None.
This is storage concurrency evidence, not a live Chat misdelivery reproduction.

Closed-context diagnosis also applies before a new conversation ID is known.
An existing closed context returns browser_closed, while the durable sending
record is unchanged. A fresh adapter with no context and no conversation ID
still returns an unconfirmed observation without launching Chrome, searching
history or resending. Isolated real-Chrome tests cover context/browser closure
both before and after receipt identity; the pre-identity cases failed before
this correction. This does not solve manual reconciliation after pre-ID crashes.


### Saved provider interruption

An explicitly correlated provider interruption is now persisted as `interrupted`.
Status/list and bounded wait can distinguish it from a submitted answer still
waiting for observation. Recover reports `reply_interrupted` without reopening a
browser after controller restart. The original prompt, conversation and user
message identity remain unchanged; partial output is not promoted to an answer.
Timeout, unavailable history, authentication rejection and browser closure do
not set this state. Queued follow-ups stay queued and require reconciliation or
explicit cancellation; interruption does not authorize automatic send or resend.

This adds a development submission-state value. Older controllers do not support
reading these new records; do not share the state directory with an older binary.
The SQLite restart/HTTP projection tests use controlled provider payloads, not a
new live Chat stop trial.

September 21 follow-up: fresh HTTP history from the existing Thinking-stop probe
`6aafe834-c5b4-83ee-aec2-c77786d2101c` returned 200 without opening/navigating
pages (13 pages before and after). The current projection and lifecycle persisted
`interrupted` into an isolated SQLite ledger. Closing/reopening that ledger
reported interruption with zero additional provider reads and no final answer.
The captured projection retained correlation/terminal metadata and omitted
reasoning content. This exercises current persistence with freshly retrieved real
provider evidence; it is not a newly generated or newly stopped Chat, a complete
controller-process restart, or browser-independent authentication. The local full
suite on this branch passed 1480 tests with 18 skips.

### Initial HTTP access rejection

The HTTP reader now retains a 401/403 observed during its initial browser login
observation, just as it retains a rejection during subsequent HTTP reads.
Repeated polling returns the same access error without creating another page.
An explicitly replaced reader or browser context permits a fresh observation.
This does not refresh credentials, bypass an access rejection, or establish
browser-independent authentication. Regression tests cover both statuses,
page cleanup, repeated polls and explicit context replacement.

### Outgoing input identity checkpoint

The HTTP-read controller records the browser-generated input message ID in the
existing submission row before forwarding its generation request. This keeps the
state `sending`: a locally observed request is not proof of server acceptance.
Storage failure aborts forwarding; uncertain sends are never replayed. The stored
ID cannot change, and a later receipt must match it. For a known conversation,
recovery uses that exact ID with the existing HTTP receipt and answer projections,
without discovering message IDs from a rendered page. Authentication bootstrap
may still require an observation page.

This avoids treating HTTP messages absent from a partial DOM baseline as newly
sent messages. On September 21, the existing audit Chat exposed four DOM input
IDs but eight HTTP input IDs; all four DOM IDs matched, while four other HTTP
inputs were absent from the rendered baseline. This was read-only live evidence,
not a new generation trial. The baseline-set-difference proposal was therefore
not adopted.

New Chat creation still requires the browser to establish its conversation ID.
An unknown conversation after a crash remains unresolved rather than triggering
history-wide search or resend. Generation preparation, model/effort selection,
and authentication are not made browser-independent by this checkpoint.

September 21 live acceptance at source `f319869` / bundle `77e6ced` reused the
existing dedicated browser and ordinary Chat
`6aaff6f2-ee68-83e8-9212-fef4b9b8d44c`, with observed `GPT-5.6 Sol` and effort
`高、5 件中 3 番目。`. Operation `f8bea83b264a414b8f71781a3bb64758`
returned `sending` with outgoing input `40a87cde-e44e-45bc-bfb7-f9c92a00d00b`.
A subsequent HTTP receipt/answer recovery returned `completed`, final message
`a2d12389-e0e2-4d31-918c-57ed3d85c1ef`, and exact text
`AC_RECEIPT_OK_20260921`. The browser was not restarted; tab count was 13 before
and after. Initial authentication observation may transiently create a page;
these endpoint counts do not prove that no observation page was opened.
This is a plain-message live send and final-answer test. Resource forwarding and in-flight restart remain controlled tests, not live
crash or attachment acceptance.

The existing browser dispatch fixture also fires two identical generation POSTs
concurrently. With the previous post-await `dispatched.done()` guard it forwards
two requests (three regression variants fail); the pre-await claim forwards one.
Storage-failure variants forward none. This is controlled browser interception,
not a claim that the live provider duplicates requests.

For a newly created Chat whose original page has reached a valid conversation
URL, the HTTP-read backend also reuses the checkpointed outgoing input ID for
receipt verification. It does not rediscover that input from rendered history.
This still requires the original page to reveal the conversation URL; a lost
page with no saved conversation identity remains unresolved. A controlled
regression covers a reopened ledger with an available page handle, prohibits
DOM evaluation and verifies the exact HTTP receipt and final projection. It
fails against the previous DOM fallback. This is not live browser-crash recovery.

The completed live result above was subsequently recovered after closing and
reopening its isolated SQLite ledger and constructing a new controller whose
browser factory rejects all access. Recovery and a duplicate send returned the
same saved completed answer without browser access. This establishes completed
result reuse, not recovery of an in-flight process crash or an OS restart.

PR #117 was integrated at `0f4f973b564cb4fff5330da77f4b758703c2c122`. Its five
CI jobs passed; Windows ran 1462 tests with 34 skips and verified the relocated
portable runtime. The first Windows attempt timed out in the unchanged workspace
JavaScript fixture before the full suite; the same source and unchanged deadline
passed on rerun. The initial timeout cause remains unresolved.

### Requested versus reported settings

HTTP final-answer recovery optionally returns and persists `reported_settings`
with `model_slug` and `thinking_effort` from that exact answer's metadata. These
are provider evidence, separate from the requested UI `model` and `effort`.
There is no fixed model list or UI-to-provider mapping and no automatic claim
that the requested settings were honored. Missing, malformed or oversized fields
remain unavailable; only those two bounded strings are retained, not arbitrary
metadata. Existing records and non-HTTP adapters default to no reported settings.
Completed evidence is immutable with the answer and survives ledger reopening.
It is stored in a separate table in the same SQLite transaction, bound to the
operation, owner and answer ID. The original submission JSON is unchanged, so
older runtimes can still read the answer while ignoring the additional table.

A read-only HTTP check of the September 21 follow-up trial found
`model_slug=gpt-5-6-thinking` and `thinking_effort=extended` on its matched final
answer after the UI selected GPT-5.6 Sol/high. This single observation is not a
stable translation table. No reasoning content or authentication values were
exported during that inspection.

### Saved URL decoration and HTTP recovery

The request validator and saved-history projector share the same bounded text
comparison: a Markdown link whose label and target are the identical HTTP(S) URL
may reduce to the literal requested URL. All remaining text must match exactly.
Different labels, different targets, and added text remain rejected; conversation,
input, resource, and response-correlation checks are unchanged.

A live ordinary-Chat audit on 2026-09-21 exposed the previous asymmetry: the
request accepted the decorated GitHub URL, but HTTP recovery rejected it. After
sharing the existing comparison, the same input and final answer were recovered
without resending or editing the ledger. This proves that recovery case, not a
browser-independent authentication or generation path.

### Standalone read transport

The CLI HTTP-read mode lazily owns one standalone Playwright API request context.
After the dedicated browser observes the provider's authenticated history/catalog
request, subsequent GETs use this client with the same in-memory allowlisted
headers, without copying browser cookies or local storage. The CLI disposes it
before closing the Playwright runtime on normal exit or error. Saved-state-only
commands do not start that runtime. Directly constructed browser adapters retain
the existing browser HTTP transport unless given a request factory.

Live read-only trials on September 21 returned HTTP 200 and the exact existing
conversation/final message through a standalone client, both with and without
copied cookies. The implementation uses the no-copy option. Initial authentication
still needs the dedicated browser. Once this adapter has observed authorization,
standalone history/receipt reads and an already observed catalog remain available
after that browser closes, while the controller and its HTTP client stay alive.
Sending and any unbootstrapped browser-dependent operation still report browser
closure. Closing Chrome does not imply cancellation of server-side generation.
This does not yet provide browser-free sending, persistent login or token refresh.
401 and bootstrap rejection remain session-wide; subsequent HTTP 403 is latched
per resource URL until explicit controller/context replacement. Neither
transport failures nor missing answers authorize generation replay.

The browser-close behavior is covered with a real Chrome process, standalone
HTTP client and controlled local HTTP server, including post-close 401/403 and
rejection latching.

A September 21 live read-only trial also recovered the exact saved answer and
input receipt from ChatGPT after closing the trial adapter's real Chrome process.
The standalone client copied no cookies. Observed authorization was seeded in
memory from the existing authenticated session; its 14 tabs stayed unchanged.
Thus this verifies live recovery with the adapter's browser disconnected, not
initial login, token renewal, or absence of every Chrome process on the host.
No generation was sent. The full local suite passed 1506 tests with 19 skips.

### HTTP denial scope audit

A normal Chat review of PR #124 identified that one authenticated history/catalog
403 prevented unrelated reads in the same controller. A controlled transport
regression reproduced this before the fix. Subsequent HTTP 403 is now retained
per exact URL; 401 and initial bootstrap rejection still stop the whole reader.
The existing real Chrome/APIRequestContext test covers both transport modes,
history/catalog rejection, allowed cross-resource reads, and no requests to a
latched denied URL. This is not a live ChatGPT permission-revocation trial.

The same review identified stale account headers after a user switches accounts
inside a BrowserContext. New HTTP-controller submissions now checkpoint the
observed `chatgpt-account-id` with the outgoing input identity before forwarding.
Missing account identity or checkpoint failure aborts the intercepted request.
Before forwarding, the generation account must also match an already established
HTTP reader account. Queued follow-ups revalidate their persisted parent account
at the request checkpoint, including after restart; they cannot replace it with
the currently selected browser account. The immutable parent binding is reused
instead of introducing a second copied expected-account field.
The non-secret account binding is immutable and stored separately from legacy
submission JSON; authorization tokens are never stored in the ledger.

Bound recovery first compares the operation's account with the observed HTTP
account. With no cached authorization, it bootstraps via model-catalog observation
before requesting conversation history. A mismatch rejects the history request;
it does not replace cached credentials or switch another pending operation's
account. A controller for the matching account is needed to recover that operation.
This provides observed request/account consistency, not cryptographic proof of
provider identity, automatic token renewal, or simultaneous multi-account support.
Legacy operations without a binding retain their existing exact input/history
checks; they do not acquire inferred account bindings. Older runtimes can read
legacy JSON but do not enforce this new binding check.

Controlled tests cover atomic rollback, restart, immutable and owner-bound
checkpoint identity, rejection before cross-account history requests, matched
HTTP recovery, and missing-account generation abort. Live account-switch behavior
and workspace identity semantics remain unverified.

A September 21 live read-only check used the new reader with an account binding
seeded from the already observed session. One independent HTTP request recovered
the exact saved answer. Changing only the expected account to a fixture value
was rejected before any additional request. No cookies were copied, messages
generated, browser tabs opened, or real accounts switched. This confirms live
matched recovery plus a local mismatch guard, not new-send binding or live
multi-account acceptance.

Account mismatches reaching the CLI or MCP recovery boundary are reported as
`account_mismatch`, with automatic retry disabled. Recovery must use the original
account and operation ID; it must not resend or reassign the operation. Provider
error details and account identifiers are not included in this diagnostic. A send
whose acceptance remains unknown still reports `submission_unconfirmed`; this
recovery diagnostic does not establish that an uncertain send was rejected.

### Exact HTTP send selection

HTTP-read controllers now require `http_selection` on new sends. Obtain it from
an available choice returned by `subchat_catalog({"source":"http"})`, or send
`{"action":"catalog"}` to the JSON-lines CLI. Copy the choice's complete
`http_selection` object into `subchat_send`/CLI `send`: `version_id`, `preset_id`,
`model_slug`, and `thinking_effort`. Null effort is explicit, not a wildcard.
Continue supplying the observed UI `model` and `effort` labels for preparation.
The adapter does not guess which provider ID a label means. The catalog also
reports `http_selection_send_supported`: true identifies an HTTP-read controller;
false permits discovery only. Restart the adapter with `--http-read` before
attempting constrained sends when this flag is false.

Preparation rechecks that exact choice against the current authenticated catalog
before opening a composer. At the generation POST boundary, its model and effort
must match before forwarding. A mismatch aborts the intercepted request; it never
rewrites the provider request or substitutes a different model. The operation
retains conservative `sending`/unknown semantics after reservation. Matching
request fields prove requested settings, not which model the provider ultimately
executed; correlated answer metadata remains separate evidence.

The selection is an immutable send argument saved in the existing submission,
and queued follow-ups inherit it. Reusing an operation ID with different settings
is rejected. HTTP sends without a selection fail before browser work; saved old
operations remain readable/recoverable. Previously queued work without a selection
cannot be newly dispatched in HTTP mode. Creating a new queue from a legacy
parent without the required selection is rejected before saving a child operation,
through both CLI and MCP. UI-only mode cannot accept this HTTP
constraint. Unconstrained legacy records retain their old JSON shape; constrained
records contain the new field and must not be processed by an older runtime that
does not understand it. Unlike answer evidence, this send constraint must not be
silently ignored on downgrade.

A September 21 live catalog read observed different Pro slugs under latest and
5.6, and different UI/HTTP effort vocabulary. These observations are not hardcoded
mappings. Controlled real-browser tests change the outgoing model or effort and
verify no forwarding, alongside matching dispatch and durable queue tests. This
is controlled transport evidence, not browser-free sending.

A live ordinary-Chat trial then completed with operation
`a6642fe8d853405186e76c5434c42147`, conversation
`6ab057fd-148c-83ee-bcdd-3e4610e6eed0`, input
`fc54bccc-ff38-4b9f-a2cc-b8a013279a7f`, and answer
`f8e31d67-8187-4a31-b999-5445ef6d7d25`. The selected catalog choice was
version 5.6, preset 2, `gpt-5-6-thinking` / `extended`; recovered answer metadata
matched and the final text was exactly `AC_WIRE_SELECTION_OK`. The dedicated
window was minimized before sending. An earlier preparation-only attempt using
the short effort label failed before draft exposure; after observing the full UI
label, that unsent operation was cancelled and a new test operation was created.
The successful operation was sent once and recovered without replay. This proves
live constrained browser-prepared generation plus independent HTTP recovery,
not browser-independent generation or universal provider-model guarantees.


### Call-scoped HTTP recovery observations

A successful HTTP read is not proof that a turn is still Thinking. `recover` and
MCP `subchat_wait` may return an additional `observation` containing the observed
`operation_id`, `source: http_history`, `reason` and UTC `observed_at` timestamp.
This is evidence from that call, not a new saved generation state. `status` and
list remain ledger-only and do not retain or advertise old observations.

| Reason | Meaning and response |
| --- | --- |
| `input_not_observed` | The requested input is absent from the fetched page. History may be incomplete; do not resend. |
| `correlation_unavailable` | Input metadata does not establish answer ownership. Do not select an answer by proximity. |
| `correlation_ambiguous` | Multiple input records share the correlation. Reconcile rather than guess. |
| `final_not_observed` | No correlated final is present. This alone does not prove active Thinking or a stalled model. |
| `final_ambiguous` | More than one correlated final exists. Do not choose a regenerated alternative automatically. |
| `final_not_complete` | A final exists without all required completion markers. Keep the saved submission pending. |
| `final_text_unavailable` | Completion markers exist but the supported final text is unavailable. Do not manufacture an empty success. |

A queued child's recovery may return its predecessor's observation. Its explicit
`observation.operation_id` identifies that predecessor; the outer submission ID
still identifies the queued child. Wait returns the most recent completed
observation within that wait, with its timestamp, unless the saved lifecycle
state of either the outer submission or the observed predecessor advanced in
the meantime. It does not claim an in-flight read completed.

No raw reasoning, partial answer, provider metadata, credentials or guessed
progress percentage is included. Explicit provider interruption still follows
the existing interrupted path. Transport/access errors remain errors. These
observations never authorize replay, automatic page reload, or a terminal-state
transition. Existing adapters returning `None` remain supported but provide no
new observation. The internal answer-reader return contract also accepts
`SubchatPendingObservation`; the final-only `project_history` wrapper is retained.

### Final completion metadata compatibility, 2026-09-21

A live follow-up in conversation `6ab06cc9-20a0-83ee-aaf8-95c01767e4ea`
returned final `a1514d4a-11d1-400e-9639-748b791c00e1` for input
`5fe0a0e1-f927-4eaf-8434-78a8fc05230e`. HTTP history recorded
`status: finished_successfully`, `channel: final` and `end_turn: true`, but
omitted both `is_complete` and `finish_details`. Codex read_thread independently
returned that same input/final identity and text `AC_STREAM_FOLLOWUP_OK`.
Requiring those optional metadata keys left the completed answer pending.

Recovery now requires the existing exact input/correlation, unique final,
finished status, closed turn and nonempty text. If completion metadata is present,
it must still agree: false/null completion, malformed/unknown finish information
and explicit interruption are not accepted as success. Absence alone does not
contradict the provider's explicit finished status. The regression failed before
the change; live recovery then completed the same saved operation without replay.

A separate page-local fetch-clone prototype observed the conversation ID in the
live SSE response but its stream read failed. It was removed after the experiment
and is not part of the product. This does not establish new-chat identity recovery
or browser-independent sending. No reasoning text or authentication data was saved.
