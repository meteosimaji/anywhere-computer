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
