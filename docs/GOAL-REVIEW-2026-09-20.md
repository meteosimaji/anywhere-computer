# Goal review, 2026-09-20

The active goal remains competitor-informed implementation, regression tests,
review, documented adoption decisions, and verified integration into main.
Subchat, audio context and reconnection are explicit follow-up requirements;
none is completed merely by finishing the initial CoS comparison.

## Verified baseline

PRs 58 and 59 merged after all five CI jobs passed, including native Windows
runtime tests. PR 59's merge is `72459ab7c52c621b76e2ea72d00a6c7694e52f7b`.
This is source integration, not deployment of these commits to every installed
plugin. See the dated development and practical acceptance records for deployed
runtime identities and the limits of their evidence.

The subchat code is still under `scripts/`, with local browser and receipt tests.
It is not a registered production subchat MCP service. Model/effort discovery,
ordinary Chat messages and independently correlated replies have live evidence;
the external Codex app transport is not repaired by those successful owning-app
calls. Dynamic menu discovery must not silently select a replacement model.

## Ordered remaining work

1. Finish a usable ordinary-Chat subchat lifecycle: create a new Chat, select an
   observed model/effort, submit exact multiline input once, persist conversation
   and submission identity, wait/read without stopping Thinking, and resume reads
   after reconnect. Integrate CLI/MCP using the existing operation ledger instead
   of a second unrelated task database. Test ambiguous submission, duplicate text,
   unavailable models, login expiry and changed markup without automatic resend.
2. Verify that lifecycle with local machine tests and fresh authenticated ordinary
   Chats through the installed Anywhere path. A manual browser send combined with
   a Codex-owned read is useful evidence but not the finished independent path.
3. Complete the requested context inputs: explicitly selected playback/virtual
   audio and microphone sources, bounded capture, result artifacts and actual
   signal verification. Do not select a phone microphone by default. Browser
   playback controls alone do not prove recorded speaker output.
4. Finish recovery acceptance: network interruption, service/app update, user
   login after OS reboot, sleep/resume, and Windows VM stop/start. Verify device
   identity, installed version and historical result recovery without mutation
   replay. Do not promise operation on powered-off hardware or restoration of
   process memory. Windows-specific blockers must not suspend independent work.
5. Reconcile competitor adoption records and publish the verified implementation:
   targeted regressions, applicable cross-platform CI, final diff review,
   push/main merge, package/runtime version alignment, and installed-plugin
   acceptance. Do not claim a beta/release solely from merging development scripts.

## Current input defect and test lifecycle

The authenticated Chat editor converts a multiline `fill` or `insert_text` into
separate paragraph nodes; `innerText` then contains additional blank lines.
Typing lines with Shift+Enter also triggered code-block formatting. These probes
stopped before sending. Exact saved-message serialization remains unverified;
whitespace normalization would hide the requirement rather than satisfy it.

Development probes previously closed their dedicated Chrome context in `finally`
after each observation. This caused visible repeated window closure, not evidence
of a browser crash. Interactive diagnosis now holds one dedicated session open;
intentional restart acceptance should be a separate, explicitly identified step.

## Scope control

The broader PTY/ConPTY, native GUI, browser-provider, onboarding and durable-task
roadmap remains tracked work, not evidence of this goal's completion. Do not
silently discard those requests or turn every new upstream feature into a release
gate. Complete the selected subchat/context/recovery work before opening another
orchestration framework. Review upstream at pinned revisions; current observed
CoS main is `8f76ccc790917b01ee758da6687a1cf9b576ba8a`.

No completion percentage or delivery date is established by test counts. The
largest remaining gap is production integration and end-to-end acceptance, not
another standalone probe.

## Shared work is a subchat acceptance requirement (2026-09-20)

The current `SubchatSubmission` stores prompt/model/effort and message/result
identity. It does not bind a device, workspace, or shared job. Thus a successful
message exchange is not evidence that subchats can collaborate on real files.
Production integration must bind the parent and subchats to the same explicitly
selected device and workspace, using the existing device router and file/terminal
APIs rather than copying project directories or inventing a second filesystem.
This shared context must survive recovery and be returned to callers; model
instructions alone are not an authorization or routing boundary. Each new Chat
must actually discover and invoke its authorized Anywhere connection. Merely
including a filesystem path in its prompt does not provide tool access.

Use existing hash-conditional file writes for conflicting edits: a second writer
with an old digest must re-read and reconcile rather than overwrite. This protects
writes through the file API, not arbitrary shell processes or external editors.
Start with distinct-file assignments or serialized edits to a shared file, not a
new distributed locking service. Sharing a directory does not share terminal
process state or conversation history.

Acceptance must use two ordinary subchats on one test workspace: A creates code,
B reads that actual file and runs it, A changes it, B observes the new contents,
and a stale hash edit is rejected without losing A's change. Recover the work
context after reconnect and verify an unavailable/different device is not
silently substituted. Report actual tool calls and file/run results separately
from conversational claims. This remains unimplemented acceptance work.
