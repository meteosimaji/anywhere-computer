# ChatGPT subchat transport investigation

Status: experimental investigation, not an available subchat feature.

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
EOF explicitly shuts down this controller and its browser. This entry point is
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
