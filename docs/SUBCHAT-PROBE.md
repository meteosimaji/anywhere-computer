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
