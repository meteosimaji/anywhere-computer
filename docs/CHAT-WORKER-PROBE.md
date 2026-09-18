# Ordinary ChatGPT Worker transport investigation

Status: experimental investigation, not an available Worker feature.

The requested Worker uses ordinary ChatGPT Chat, not a Codex task, ChatGPT
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
python scripts/probe_chat_worker.py --server /absolute/path/to/codex-app-tools/server.mjs
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
4. Recover the corresponding assistant response and continue the same Worker.
5. Simulate a lost acknowledgement without creating a second conversation or
   repeating a submitted prompt. Report an unknown outcome when reconciliation
   cannot establish whether submission occurred.

CoS currently uses a browser companion for normal Chat creation and submission;
an API-shaped external interface does not eliminate that browser dependency.
Anywhere may need that adapter for creation even if the Codex app's existing
conversation read/send tools can be reused. This remains unverified, and no
production Worker tool should advertise support on the strength of this probe.

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
do not replay the follow-up. A Worker result collector must match the expected
new user/assistant message pair and cannot accept an arbitrary idle snapshot.

This establishes browser creation plus Codex-app follow-up submission. It does
not establish headless creation, fresh tool-only result recovery, or the complete
Chat -> Anywhere -> Codex app -> Worker path. The external MCP endpoint remains
missing in the current executor environment. The two socket-preflight tests
exercise absent caller context and an unlinked-but-open Unix socket; they are not
Worker end-to-end acceptance tests.

### Follow-up recovery and bounded waiting

A later Codex read recovered the second turn with a new user-message ID and
assistant-message ID. The user independently confirmed both turns in the mobile
ChatGPT app. The installed reader uses a cache-aware `getOrFetch`, while send's
preflight uses explicit `refetch`. This supports delayed read visibility; it does
not prove the exact cache expiry or notification responsible for the delay.

The probe now accepts `--after-user-id`, `--expected-prompt-file`, and
`--wait-seconds` (1–300) with `--read-thread`. These only read an already-submitted
prompt. They never send or resend it. The collector requires the selected Chat,
a visible baseline turn, exactly one subsequent matching prompt, a distinct
answer ID, an idle thread, completed turn and untruncated text. Ambiguous,
truncated, active, missing-baseline or stale snapshots remain unconfirmed. The
current prototype deliberately does not accept multi-item/tool-bearing turns;
that broader contract remains unimplemented. Prompt file contents are compared
exactly, including trailing newlines. Output contains IDs and character counts,
not the response text. A timeout exits nonzero with `reply_unconfirmed`.

Nine targeted tests cover endpoint context, the unlinked socket, old snapshots,
wrong Chat/prompt/baseline, active or incomplete responses, duplicate matches,
truncation, eventual freshness and bounded read timeout. They validate collector
logic, not the complete external Worker connection.
