# ChatGPT subchat transport investigation

Status: historical transport investigation plus experimental subchat development.
The browser/stdio prototype exists; individual capability and live-acceptance
claims below have separate scopes. This is not a stable desktop-steering API.

Product name: `subchat` (Japanese: サブチャット). A subchat is an ordinary
ChatGPT Chat created for a delegated task. It is not a ChatGPT Work task.
Historical `AC_CHAT_WORKER_...` test markers below are retained as evidence.

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
