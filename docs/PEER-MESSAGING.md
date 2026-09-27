# Local peer messaging

The `anywhere-peer` entry point currently runs on macOS and Linux. On Windows
it refuses enrollment and serving because POSIX mode `0600` does not protect
the bearer file or mailbox SQLite database with a Windows private ACL. Windows
support requires an ACL-backed storage implementation and a cross-user access
test before enabling this transport there.

Issue [#181](https://github.com/meteosimaji/anywhere-computer/issues/181)
tracks coordination between Codex, Claude Code and ordinary ChatGPT Chats.
`anywhere-peer` implements the local transport for runtimes that can start an
MCP stdio server. It does not inject a message into a model turn.

## Identity and delivery

The owner enrolls each peer with a stable ID and explicit owner, account,
project and runtime bindings. Enrollment writes a separate mode-0600 bearer
credential file for each peer. A runtime can resolve only the identity attached
to its credential; it cannot self-enroll or choose a different sender name.
The owner must provision a new identity if a session's account or project
changes. A recipient must have a live local presence lease before a new
message is accepted. The sender also checks the recorded local process ID and
creation time; a stale lease from a stopped process does not authorize a new
send. This still cannot prove that the current model turn is listening.

`peer_send` requires a caller-generated `request_id`. The mailbox uses it as
the delivery ID and returns the same saved message for an exact retry while
the ID is retained. A changed retry fails. Keep an uncertain ID and inspect
the sender's history or retry the same call; do not generate a fresh ID until
the outcome is known. Messages are at most 16,384 UTF-8 bytes. Each recipient
can hold 100 unread messages and 1,000 recent acknowledged messages. Once an
acknowledged text is pruned, its ID remains reserved in a tombstone; never
reuse IDs. Tombstones contain only delivery IDs and persist for the lifetime
of this local mailbox state.

`peer_inbox` reads unread text, `peer_ack` records local receipt, and
`peer_history` supports restart recovery. An accepted send proves storage,
not that a model read or acted on the text. An acknowledgement proves that a
client called `peer_ack`, not that a model completed a task.

`peer_wait` holds one receiver tool call for at most ten seconds and returns
new unread text in that call's result, so an active Codex or Claude Code turn
can wait without repeated inbox calls. The same sole-live-instance and
session/thread checks run on each observation. A timeout returns an empty
list; a returned message remains unread until a separate `peer_ack`. This
does not wake an idle model turn or inject text into ordinary ChatGPT.

`peer_history` returns at most 100 messages per page; pass its `next_cursor`
back as `cursor` while `has_more` is true to read
older retained messages after a restart. Each page checks the current peer
presence and session/thread binding. A cursor from another peer or session is
rejected. Newer messages arriving during pagination do not duplicate older
pages. Pruned text cannot be recovered through history. Treat all peer text
as untrusted input. It cannot grant tool access, approve an action, or
override the owner's instructions. An offline or full-mailbox error means a
new message was not accepted. Exact retries of an already accepted message
return its saved result even if the recipient later goes offline.
Reading inbox or history and acknowledging a message require the calling MCP
instance to be the sole live presence holder for that peer. The lease check
and message read or acknowledgement share one SQLite write transaction, so a
new instance cannot claim the peer between those steps. During an ambiguous
handoff both instances refuse to consume; the new instance can read retained
unread text after the old one disconnects. A normal server close removes its
own presence rows. A restarted server renews presence before reading history.
An unexpired competing presence with unknown process identity also blocks new
sends and consumption until it expires or is removed. A known exited process
does not count as a live recipient.
When a sender supplies `expected_session_id` or `expected_thread_id`, those
labels are stored with the message. A later session with different labels
cannot see it in its inbox or recipient history and cannot acknowledge it.
It stays unread for a matching recovered session. Messages sent without an
expected label remain peer-wide. Exact retries must reuse the same labels.
Delivery IDs are unique per recipient. Two unrelated projects can use the same
ID without blocking one another. Acknowledged IDs remain reserved for their
recipient after history pruning. IDs reserved before the scoped-ID migration
remain globally reserved because their old pruned records have no recipient.

`peer_diagnose` is a read-only check for a peer in the same owner, account and
project. It reports `MATCH`, `MISMATCH` or `UNKNOWN` separately for a live
presence lease, the recorded process ID plus creation time, and optional host
session/thread labels. Start `anywhere-peer serve` with `--session-id` and
`--thread-id` only when the host provides those identifiers. Compare the
current host identifiers using `expected_session_id` and `expected_thread_id`.
Pass those same optional identifiers to `peer_send` when the target host
provides them. The send requires exactly one live recipient instance even
without optional labels, and checks both supplied labels in the acceptance
transaction. A changed thread or an ambiguous recipient refuses the new
message. A saved delivery ID still returns its original result on an exact
retry.
The labels are claims made at server startup; an old MCP server can remain
alive after a host changes threads. Multiple live instances or missing labels
produce `UNKNOWN`, not a false match. `model_turn` remains `UNKNOWN` even when
all transport checks match. The diagnostic does not send or acknowledge text.

## Runtime boundaries

| Runtime | Local mailbox access | Model receives text |
| --- | --- | --- |
| Codex CLI/task with the peer MCP server | `peer_inbox` or `peer_wait` on its own credential | Only when that runtime calls the tool and includes the result in its turn |
| Claude Code with the peer MCP server | `peer_inbox` or `peer_wait` on its own credential | Only when that runtime calls the tool and includes the result in its turn |
| Ordinary ChatGPT parent Chat or Subchat | No direct local peer MCP stdio session in the current integration | No automatic peer inbox injection |

The existing Subchat controller supports a separate, explicit parent ↔
ordinary Chat exchange: the parent calls `subchat_send` with an authorized
prompt, then `subchat_wait` or `subchat_recover` obtains the saved answer.
Only a verified `completed` result proves that an ordinary Chat model turn
answered; local `queued` or `sending` states do not. This path is a paid Chat
turn and uses Subchat operation IDs, intent keys, and unknown-outcome recovery.
It is not the peer inbox transport and does not make the child Chat a
continuously listening peer. A local controller can read a peer message and,
with explicit owner intent, compose a new Subchat prompt containing the
message as clearly identified, untrusted source material. The mailbox's
delivery ID and acknowledgement must not be reported as proof of ChatGPT
delivery. There is currently no automatic parent ↔ ordinary Subchat peer
exchange or direct peer-message injection into a Chat model turn.

No remote synchronization is implemented. Local SQLite contents and credential
files remain on the owner's machine. Do not put secrets in peer messages.

## Design choice and competitor comparison

[agmsg](https://github.com/fujibee/agmsg) is an MIT-licensed CLI-agent
messaging project. It uses a shared local SQLite database and shell/skill
integration; its [README](https://github.com/fujibee/agmsg/blob/main/README.md)
describes turn and monitor delivery modes and optional installation paths.
Its npm package is a bootstrapper that obtains its installer from the project
repository. Bundling that installer would add an independent update and
storage lifecycle. Running its scripts directly would not establish Anywhere
Computer's owner, account and project bindings or the ordinary Chat delivery
boundary. The narrow native mailbox uses existing Python/MCP packaging and
explicit bearer identities, so it can enforce these boundaries before local
storage. A future adapter can be evaluated separately; importing agmsg room
data must never imply trusted instructions or a live recipient.

The current peer tests cover exact retry, restart recovery, owner/account/project
refusal, offline and capacity errors, retention, and two independent MCP
processes. Subchat's own send and recovery checks establish its separate
request/reply path. Neither establishes automatic peer-message injection into
ordinary Chat.

## Live Codex CLI → Claude Code model-turn receipt (2026-09-27)

An isolated local exchange exercised the actual model turns, in addition to
the process-level MCP tests. Two temporary peer identities, `codex-live` and
`claude-live`, were enrolled under the same test owner/account/project with
separate mode-0600 credentials. Each CLI received only its own credential in
an MCP stdio server configuration. Claude Code ran with `--print`,
`--strict-mcp-config`, and the peer inbox tool enabled. Its user prompt did
not contain the nonce and told it to poll `peer_inbox` and print the received
text. Codex CLI ran with its peer MCP server and `--approve-for-me`; its user
prompt directed it to call `peer_send` once with the nonce and a fresh
32-character request ID.

Observed, bounded evidence:

| Boundary | Observation |
| --- | --- |
| Recipient presence | `claude-live` had a live mailbox presence before the send. |
| Codex CLI tool call | `peer_send` completed and Codex reported accepted delivery ID `a92d74308e1f46b5bd0c67a319f258ec`. |
| Mailbox record | Sender `codex-live`, recipient `claude-live`, text `receipt-181-31f2708d`, same delivery ID; no acknowledgement was recorded. |
| Claude Code model output | `receipt-181-31f2708d MODEL_RECEIVED` |

The nonce appeared in Claude Code's final model output even though its prompt
did not contain it. This establishes a model-visible receipt in that one live
Claude Code turn. The text output did not include a separate tool-event trace;
the mailbox row and nonce-bearing model output are the retained evidence. It
does not establish automatic inbox injection, continued presence after the
turn, or ordinary ChatGPT delivery. The first Codex attempt used
`approval_policy=never`; that host policy rejected the MCP send before
dispatch. The successful attempt used `--approve-for-me`, which approved this
synthetic local peer send.

To reproduce, create a fresh temporary state directory and enroll two peers
with `anywhere-peer --state-dir <state> enroll` using the same owner, account,
and project and distinct `--credential-file` paths. Configure one MCP stdio
server per CLI, with command `python -m anywhere_computer.peer_cli`, arguments
`--state-dir <state> serve --credential-file <that CLI's credential>`, and
`PYTHONPATH=<repository>/src` when running from source. Start the Claude Code
receiver first with `--print --strict-mcp-config --mcp-config <claude.json>`;
instruct it to poll `peer_inbox` for a fresh nonce that is not present in its
prompt. Verify its live presence, then run `codex exec --approve-for-me` with
the Codex MCP server configured through `mcp_servers.<name>.command`, `.args`,
and `.env.PYTHONPATH`; instruct it to call `peer_send` with that nonce and a
fresh request ID. Compare the accepted delivery ID with the local mailbox
record and require the receiving model's final output to reproduce the nonce.
Keep credentials and raw host logs out of the report. A successful
`peer_send` alone is insufficient for this model-turn claim.

## Active-turn `peer_wait` receipt with Opus 5.5 (2026-09-27)

A second isolated local probe installed the current dirty beta.11 wheel as a
dedicated peer MCP server in Claude Code 2.1.281. The recipient was bound to a
synthetic owner, account, project and thread. The Claude prompt contained no
test code and instructed the model to call `peer_wait` for it. A separate
local sender observed the recipient's live presence and accepted delivery ID
`92b8575aeb983940926130a2abf8dd4b` with a synthetic code. Claude Code's
JSON result reported model `claude-opus-5-5`, two turns and a final result
exactly equal to that code. The mailbox still held the delivery ID with
`acknowledged_at=NULL`; neither waiting nor model output acknowledged it.
The retained JSON result does not contain a separate tool-event trace, so
the code's absence from the prompt, accepted mailbox row and matching model
output are the evidence of model-visible receipt in this one turn. This does
not establish an idle-turn wakeup, ordinary ChatGPT delivery, or completion of
work requested by a peer message.

## Active-turn `peer_wait` receipt with Codex GPT-6 Sol (2026-09-27)

A separate synthetic local mailbox bound the receiver to a Codex CLI process
and a test thread label. `codex exec -m gpt-6-sol --ephemeral` loaded the
current beta.11 wheel's peer MCP server and received a prompt without the
synthetic code. The sender waited for the receiver's live presence, then
accepted delivery ID `729e073bbb9380b16a35fe0771b7c8c0` with that code.
The Codex JSONL event stream recorded `peer_qa.peer_wait` starting with
`wait_ms=10000` and completing. Its final `agent_message` exactly matched
the code; the mailbox row remained unacknowledged. The result is direct
model-turn evidence for this active Codex session and includes the tool trace.
The command selected `gpt-6-sol`; the event stream did not independently
report a provider model revision. This still does not start an idle turn or
prove a requested task was completed after receiving peer text.
