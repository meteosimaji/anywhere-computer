# Open issue implementation and acceptance plan

This document records the implementation boundary for the four open issues as of
2026-09-27. A local test, a live ChatGPT result, an installed Plugin, and a
published release are separate forms of evidence. Keep an issue open until its
own acceptance criteria have been checked at the required level.

| Issue | Implemented in the current source tree | Remaining acceptance work |
| --- | --- | --- |
| [#198](https://github.com/meteosimaji/anywhere-computer/issues/198) owner passkeys | Local owner-confirmed enrollment; WebAuthn challenge binding and verification; password fallback; reset and revocation; signature, replay, origin, counter, and race tests. Local ticket issuance, registration and CLI removal hold the reset lock; a ticket is bound to the owner verifier and becomes invalid after password change. Assertion verification holds the credential lock through counter update, so simultaneous assertions cannot both verify against one old counter. Code issuance rechecks the verified credential under that lock, preventing removal between verification and issuance. A real Chrome browser with an isolated virtual authenticator completed registration and consent; it also rejected CSRF tampering and replay | Verify the public HTTPS flow with a physical or platform authenticator before calling it deployed |
| [#180](https://github.com/meteosimaji/anywhere-computer/issues/180) delegated child permissions | Child-bound HTTP bearer, tool/device/expiry scope, ledger/audit denial, confined local file read and write, runtime limit checks; real HTTP MCP tests. Local file workers recheck child and parent grants after the async handoff and serialize their operation with revocation. An HTTP remote path now requires a separately issued target child bearer and uses the target's confined child executor instead of the saved parent OAuth route; a two-service integration verifies allowed read and out-of-scope write rejection. Source-side remote results and generic target failures are recorded under the child operation ID; a regression case verifies that a reserved but uncertain ID is not dispatched again. SSH now has a child-only MCP entry point, authenticated stdio bearer handoff, alias-bound OS-vault route, and local subprocess checks for scope, replay, revocation and fail-closed authentication. | Normal ChatGPT Subchat does not currently supply a verified child identity or a safe child-bearer handoff. Remote HTTP needs public HTTPS and target-side owner provisioning acceptance. Test SSH against a real host and cross-host expiry, revocation and reconnection before claiming deployment |
| [#181](https://github.com/meteosimaji/anywhere-computer/issues/181) peer messaging | Local Codex/Claude mailbox with bounded messages, stable IDs, ack/history, restart recovery and destination diagnostics. New sends require a presence lease, matching local process identity and exactly one live recipient instance, so a stale lease or ambiguous same-ID session cannot admit a message. Inbox, history and ack now require the calling instance to be the sole live presence holder in the same transaction as consumption; an old instance cannot consume after a handoff. An unexpired presence with unknown process identity blocks both send and consumption until resolved. Pruned acknowledged text leaves a durable delivery-ID tombstone, preventing its old ID from being accepted again after restart. Existing Subchat send/recover supplies an explicit parent-to-ordinary-Chat request/reply | A live Codex CLI send and Claude Code model output proved receipt in one isolated turn; a mailbox ack alone remains insufficient. Verify host session changes and ordinary Chat delivery separately; ordinary Chat has no automatic peer inbox or linked usage record |
| [#185](https://github.com/meteosimaji/anywhere-computer/issues/185) Subchat improvements | Owner-scoped saved list and prompt preview opt-in; HTTP choice ID; explicit model/version resolution; named macOS account selections and a no-send setup doctor; multiple image selection; bounded file ranges; opt-in durable automatic queue and events; queued model change with revision check in the local controller; explicit already-uploaded resource attachment on queued follow-ups; direct HTTPS download and local cancel; owner-scoped direct file save to a selected device with durable checkpoints and local Engine/Router/Uploads integration tests. A loopback OAuth HTTP MCP service also saved a real Chat file through the current gateway and local upload engine with a durable completed receipt and matching bytes. A parent/child automatic queue, a 600,000-byte ranged file download, and a queued model change passed live ChatGPT transport checks | Verify direct file save from a real Chat account to an authorized physically remote device. Complete Chat upload/Library and platform work, define supported stop/steer semantics, resolve uncertain-turn recovery without replay, then package and update the Plugin. Host delivery of MCP notification into a model turn remains unverified |

## Operational gates

For #181, the local peer MCP server now offers a bounded `peer_wait` call.
It waits up to ten seconds for unread text and returns that text in the active
tool result without acknowledging it. The existing current-instance and
session/thread checks run on each mailbox observation. A two-process MCP test
started the receiver wait, sent from the other process, observed the same
delivery ID in the wait result, and then found it still in the inbox. Empty
timeout and invalid duration were also checked. The peer suites passed 24
tests, Ruff and Mypy passed on 152 modules. This improves active-turn delivery
without claiming an idle model wakes automatically or that a mailbox receipt
proves model processing.
The dirty beta.11 wheel was rebuilt after adding `peer_wait`; the five
development package checks passed. The earlier isolated Codex and Claude
beta.11 caches matched the pre-`peer_wait` candidate, so their hashes do not
validate this newest wheel. A clean version bump and fresh host installation
remain required for final release acceptance.
In the same isolated homes, repeating `codex plugin add` for beta.11 copied
the newly rebuilt wheel and its hash matched the candidate. By contrast,
Claude Code `plugin marketplace update` followed by `plugin update` reported
`updateOutcome=up_to_date` for beta.11 and left the old wheel hash in its
cache. A successful marketplace refresh therefore cannot prove that a
same-version development wheel reached Claude. The final clean release must
use a new version; compare installed artifact hashes and start a fresh session
after either host's update.
The packaged `peer_wait` path was also exercised in a real Claude Code
2.1.281 model turn using the `opus` alias. Claude's JSON output named
`claude-opus-5-5`; its final text exactly matched a synthetic nonce absent
from the prompt and accepted by the local mailbox while the receiver was
present. The original delivery ID remained unacknowledged. This proves
model-visible receipt for that one active Opus turn, while a returned tool
message remains distinct from automatic wakeup or task completion.
An independent Codex CLI probe selected `gpt-6-sol` and loaded the current
wheel's peer server. Its JSONL events explicitly recorded the
`peer_qa.peer_wait` tool starting and completing, followed by a final model
message exactly matching a synthetic nonce absent from the prompt. The
mailbox delivery ID remained unacknowledged. This strengthens #181's
active-turn receipt evidence for both Codex and Claude, but neither probe
establishes idle-turn wakeup, ordinary Chat inbox injection or linked model
usage for a peer message.

The current development bundle now has a package-coherence test that also runs
for a dirty candidate: it checks both artifact hashes, every Python source in
the wheel, and all three Codex and Claude MCP server commands against installed
entry points. The packager updates the new `anywhere-subchat-library` server in
both host configurations. After the time-limited Library MCP authorization
change, 2,536 tests passed, 36 were skipped, and the formal clean-source
release test was deselected. The candidate is still `source_dirty=true` and
has not replaced the user's installed beta.10 Plugin. These package checks
establish artifact coherence, not host reload or an issue's live acceptance.

An end-to-end selected-account Library-to-ordinary-Chat check used a new
synthetic text file. Current-source `anywhere-subchat-upload` operation
`17871567f11bd1122ee3a3d4a6e5c5f6` reached Library `ready` with provider
file ID `file_000000000b488206a4a2e3182af27af8` and Library item ID
`libfile_ef189033153c8191bd99c3d6c093e4bb`; a separate read-only status
call recovered the same IDs. The installed beta.10 Subchat Plugin sent
ordinary Chat operation `850d190970b45a35e1eb1bcff698c7c5` with those IDs
as an explicit attachment. Its final answer exactly reproduced the synthetic
file's `Verification code:` value, which was absent from the prompt. This
proves model-visible attachment content in that one Chat turn. The catalog
choice was `latest` / `GPT-5.6 Sol` / `Instant` with HTTP model slug
`gpt-5-6-instant`; saved reported settings used `gpt-5-6`, so the picker and
reported model identifiers remain separate observations. The caller
accidentally declared 104 bytes while the local file measured 109 bytes; the
provider still accepted and answered. Thus this run does not prove that the
attachment metadata's size was verified against provider bytes. The local
SHA-256 was `93534f027c85a98234db734ac341b7103e492f291a0295e629eb570febba95bd`;
the Library list did not supply an independent remote byte digest. This does
not exercise a physically remote device, Windows/Linux browser upload, or a
freshly installed beta.11 Plugin.
To remove the manual metadata error from the current-source path, the Library
MCP `ready` response now includes a validated `attachment` descriptor derived
from its pinned ledger record. A live read-only status for this exact upload
returned size 109 with the saved IDs and `text/plain`; an `unknown` upload
returns no descriptor. The related upload/resource/MCP suites passed 49 tests,
Ruff and Mypy passed on 152 source modules, and the rebuilt dirty beta.11
candidate passed the five available package checks. This descriptor has not
yet been exercised from a freshly installed beta.11 Plugin in a Chat send.
It has now been exercised from a fresh isolated Codex Plugin installation:
`codex plugin add anywhere-computer@personal` installed the current dirty
beta.11 candidate under a separate `CODEX_HOME`; the cached wheel hash matched
the candidate. Its packaged `anywhere-subchat-library` MCP server returned the
same ready upload's validated 109-byte attachment descriptor. The packaged
`anywhere-subchat` server copied that descriptor unchanged into Chat operation
`1fd6c4184ef3581344569caf6132c541` using the freshly observed `latest` /
`GPT-5.6 Sol` / `Instant` catalog choice. The first checker script mishandled
the shape of an interim `subchat_wait` result and exited after the send had
already been accepted; it did not resend. Exact-ID recovery from the shared
ledger then found a `completed` final answer with the synthetic verification
code. A fresh packaged beta.11 Subchat MCP process read the same completed
operation, exact 109-byte resource descriptor and reported settings
(`gpt-5-6`, null effort). This verifies the installed development package's
Library-status-to-ordinary-Chat path and restart recovery on macOS, while the
user’s installed beta.10 Plugin remains untouched. It is not a formal clean
release or a remote-device acceptance test.
Fresh `anywhere device-status` probes after this installed-candidate check
still returned `unreachable` for both registered SSH targets, `Latitude 5320`
and `Windows VM`; no cross-host delegated revocation or remote file-save
acceptance can be inferred from the local Chat result.
The current candidate's in-place Codex update path was retested from a clean
isolated home. A `git archive HEAD` snapshot supplied the committed beta.10
marketplace and a hash-valid clean beta.10 wheel. The isolated home installed
`anywhere-computer@personal` beta.10, switched only its local `personal`
marketplace source to the current checkout, and repeated `codex plugin add`
for the same ID. `plugin list --json` then showed exactly one enabled beta.11
identity; the installed beta.11 wheel hash matched the checkout's candidate,
and its three MCP server declarations were present. A fresh MCP process from
that updated cache completed initialization, listed the two Library tools,
and read the existing ready attachment's exact 109-byte descriptor. This
proves replacement and a new-session startup in that isolated Codex CLI home.
It does not update a running Codex desktop MCP process, the user's beta.10
cache, or a published clean release.
The equivalent Claude Code update path was also tested in a separate
`CLAUDE_CONFIG_DIR`. That isolated installation first installed the committed
beta.10 plugin, then refreshed the same local marketplace after its contents
were changed to the current beta.11 candidate. `claude plugin update
anywhere-computer@anywhere-computer-local --scope user --json` reported
`oldVersion=0.2.0-beta.10`, `newVersion=0.2.0-beta.11`, and that a restart is
required. A fresh `plugin list --json` showed exactly one enabled beta.11
plugin with all three MCP servers; the installed wheel hash matched the
candidate. A new MCP process using its installed Claude configuration listed
both Library tools and read the saved 109-byte descriptor. This checks the
artifact and new-process path, not model-visible loading in an interactive
Claude Code session or the user's installed plugin.

The 2026-09-27 release audit found an enrollment-ticket reuse window in #198:
the native credential write could finish before the ticket was removed. The
ticket is now consumed under the existing reset and passkey locks before the
credential write; on POSIX hosts its directory update is synced before the
vault call. A fault-injected test commits the vault write and then raises, and
confirms the same ticket cannot enroll another key. Failed enrollment now
requires a new locally authorized link. The audit also found that #181's
bearer file mode is insufficient to secure a Windows mailbox. The
`anywhere-peer` entry point now refuses enrollment and serving on Windows
until private ACL-backed storage is implemented and cross-user tested. This
narrows the supported local peer transport to macOS and Linux. Existing
write-only delegated child grants become inactive by the documented #180
recovery invariant and must be reissued with `operations_get`; old uncertain
remote operations cannot be automatically reconciled without their missing
target identity and request digest.
After these changes, the available full pytest suite passed with 2,526 passed,
36 skipped, and the clean-source release test deselected. The first run hit a
time-sensitive HTTP stream test while other checks were running; that test
passed alone and on the final full run. Ruff, Mypy over 151 source modules,
README link checks, and `git diff --check` passed. The rebuilt beta.11
development wheel matches all 151 modules and its manifest hash, but remains
`source_dirty=true` and is not the installed Plugin.
The passkey credential record now saves bounded redeemed ticket
digests in the same native-vault update that adds each credential. This also
blocks reuse if a Windows filesystem crash restores a deleted ticket while
the vault write survives, including after a later registration overwrites the
ticket file. Register, counter update and removal preserve the records.
A regression restores an old ticket file after two completed registrations and
confirms that neither removal nor a newer enrollment permits old-link reuse.
Digests remain after expiry, so a clock rollback cannot revive an older link;
an owner reset changes the salted verifier before clearing the bounded list.
The list is capped at 128 records to remain below the native-vault record-size
limit even with the maximum credential count and public-key lengths. Reaching
that cap refuses registration before ticket consumption and the browser tells
the owner to reset locally.
Passkey-store read failure during browser registration now returns 503 rather
than an uncaught error or a misleading expired-link response. The focused
passkey, browser, adversarial, HTTP service and authorization suites passed 85
tests; Ruff and Mypy passed on 152 source modules. The dirty beta.11 wheel was
rebuilt, its 152 source modules and manifest hash matched, and four available
package tests passed with the clean-source release check deselected.

The current-source Library uploader was exercised again on 2026-09-27 with a
fresh harmless text file in canonical `/private/tmp`. Operation
`ec16a14236caffb93a9092d774b4c27e` reached `ready` in its first invocation
with both a provider file ID and Library item ID. A separate CLI process used
`--status` with the same operation ID and returned the same ready item with
`automatic_retry=false`, without reading or resending source bytes. This
validates the selected macOS account path including the bounded exact-ID
observation added after the earlier indexing lag. It does not verify remote
file bytes, Windows/Linux browser handling, or a Plugin-installed upload tool.
The same Library workflow is now exposed through a separate local
`anywhere-subchat-library` MCP server. Its upload tool requires a caller-saved
request ID and an explicit path; its status tool reconciles the original ID
without source bytes. An ambiguous upload error returns `unknown` and directs
the caller to status rather than inviting a second upload. The new server was
built into a dirty beta.11 development Plugin, installed into an isolated
`CODEX_HOME`, and completed MCP `initialize` and `tools/list` with both tools.
An actual MCP upload of a harmless text file under operation
`2e8eb5e9463f0b64cd9b97d3f9976d8a` reached `ready` with a provider file
ID and Library item ID. A fresh MCP process called `subchat_upload_status`
for that exact ID and recovered the same ready item with
`automatic_retry=false`. This is real selected-account transport evidence for
the installed development package, not an update of the user's beta.10 Plugin
or a byte-for-byte provider digest.
After adding this Plugin tool, the full available pytest suite passed with
2,529 passed, 36 skipped, and the clean-source release check deselected.
Repository-wide Ruff, Mypy on 152 source modules, README link validation and
`git diff --check` passed. The rebuilt development wheel includes and matches
all 152 modules; its release manifest remains `source_dirty=true`.
An adversarial review of the new MCP entry point separated errors before the
first upload batch from errors whose side-effect stage may be uncertain. A
missing or redirected local path now returns `failed` with
`dispatched=false`; a post-reservation processing mismatch remains `unknown`
and directs the caller to the original operation ID. A real selected-account
preflight with a nonexistent path produced the former result without opening
the upload page. The focused Library MCP and upload suite passed 17 tests,
with Ruff and Mypy on the updated source; the dirty beta.11 development wheel
was rebuilt after this change.
The final available full pytest run after the preflight change passed:
2,530 passed, 36 skipped, and the clean-source release test deselected.
Repository-wide Ruff, Mypy for 152 source modules, README links, source-to-wheel
content and manifest hash, and `git diff --check` passed.
Fresh read-only device-status probes after that run still returned
`unreachable` for both registered SSH targets, `Latitude 5320` and
`Windows VM`. Therefore cross-host delegated permissions and direct remote
file-save acceptance remain unverified.
The Library MCP upload now requires a local `--prepare` reservation of the
exact account, file size, content digest and operation ID. Preparation makes
no browser or provider request. An MCP caller without that reservation, or
with changed file bytes, fails before dispatch. The owner-invoked CLI retains
its existing direct-upload path. In an isolated beta.11 Plugin install, a real
MCP call with operation `3603b45092cff9d7532e7f9012e2ee21` first failed
with `dispatched=false` before preparation. A separate local CLI prepared that
same ID and file. The following MCP call returned `unknown`, so no upload
retry was made. A later read-only CLI status and a fresh MCP status process
both recovered the identical `ready` Library item and provider file ID with
`automatic_retry=false`. This verifies the preparation gate and uncertain
result recovery across processes against the selected account; it does not
prove a remote byte digest or update the user's beta.10 Plugin.
The MCP adapter also now reports outer `unknown` whenever the saved upload is
not `ready`, including a normal return without an exception. Its payload
retains the checkpoint and original ID. This avoids treating an unindexed
Library item as a completed upload; focused adapter tests cover the result.
After the preparation gate and outer-state fix, the full available pytest
suite passed: 2,535 passed, 36 skipped, and the clean-source release test
deselected. Repository-wide Ruff, Mypy for 152 source modules, README link
checks, source-to-wheel content and manifest hash, and `git diff --check`
passed. The built beta.11 package remains `source_dirty=true` and is not the
user's installed Plugin.

For #180, a newly bound remote route records the target owner's supplied
target child ID alongside its source child and device identities. The bearer
remains only in the native credential store. Source revoke reports this ID
with `target_child_grant=not_revoked_here`; the target owner must separately
revoke and confirm that exact target grant. Legacy routes migrate with a null
target ID but cannot dispatch until replaced with a new source child and
route. The target child session exposes an authenticated, read-only identity
tool; before each forwarded request, the source compares its returned child ID
with the saved ID, then rechecks source authorization and route state. A real
two-service HTTP test rejects mismatched and absent IDs before file writes.
Target-side revocation still requires the target owner and confirmation there.
Local delegated file operations now keep a tracked result task after an HTTP
request is cancelled. A cancelled caller does not cancel its running worker;
normal backend close drains the task, and the original operation ID records
the completed or failed result. A regression test cancels the caller while a
write is held in its worker, then verifies the file and ledger agree. Process
termination between the file side effect and ledger finish still yields an
unknown outcome after restart and must never trigger automatic replay.
After this change, the available full pytest suite passed: 2,518 passed,
36 skipped, 1 dirty-package release test deselected. Repository-wide Ruff,
Mypy on 151 source modules, and diff whitespace checks passed. The beta.11
development wheel was rebuilt and its 151 source files and two checksums
matched the current tree; it remains `source_dirty=true`.
Remote delegated calls now bind the source operation ID to the selected device,
tool, authenticated target child ID, and exact target-request digest before
target dispatch. A later terminal target `operations_get` result with the same
ID, tool and digest reconciles a source `running` or
`unknown` record, making exact retries return the saved result without another
send. A two-service HTTP test cancels the source request after target entry,
reopens the source ledger as `unknown`, then recovers the target result and
checks the source ledger. A changed target
child ID or a different target request under the same ID is rejected, and
pre-binding legacy unknown records remain unknown.
Remote write issuance now requires source `operations_get` scope, and the
source checks the target child's current tool catalog for `operations_get`
before forwarding a write. A two-service test removes that target scope and
confirms the file stays absent, then restores it. The focused delegation suite
passed (15 tests), as did Ruff, Mypy over 151 modules, and `git diff --check`.
The target now enforces `files_write` plus `operations_get` as one child-grant
invariant at issuance, session authentication, and the file worker's locked
authorization check. A regression pauses the worker after initial admission,
removes recovery scope, then confirms the write is denied and the file remains
absent. Previously issued child grants without recovery scope become inactive
and require reissuance. The focused delegation and SSH suite passed (20 tests).
After this invariant change, the available full pytest suite passed with
2,523 passed, 36 skipped, and the dirty-package release gate deselected.
Repository-wide Ruff, Mypy over 151 modules, and diff whitespace checks
passed. The rebuilt development wheel matches all 151 current source modules
and both recorded artifact checksums; `source_dirty=true` remains.
After the exact-request reconciliation check, the available full pytest suite
passed: 2,521 passed, 36 skipped, 1 dirty-package release test deselected.
Repository-wide Ruff, Mypy on 151 source modules, and diff whitespace checks
passed. The beta.11 development wheel matches all 151 source files and both
bundled checksums, with `source_dirty=true`.
The source/target identity change passed a full available pytest run with the
dirty-package release case deselected: 2,515 passed, 36 skipped, 1 deselected.
Repository-wide Ruff, Mypy for all 151 source modules, and diff whitespace
checks passed. This does not establish a real cross-host HTTPS or SSH run.

For #198, owner reset now persists a `reset_pending` authorization gate in
the same transaction as device and grant revocation. A native passkey-store
failure leaves the gate set; device reenrollment refuses until a later reset
confirms passkey deletion. The v3-to-v4 store migration and fault-injected
clear failure have regression tests. After this change, the full available
pytest suite passed with the known dirty-package release case deselected:
2,513 passed, 36 skipped, 1 deselected. Repository-wide Ruff and Mypy on all
151 source modules also passed. A physical or platform authenticator on
the public HTTPS path remains an acceptance gate.

For #181, new sends with explicit expected session or thread labels now store
those labels on the message. Inbox, recipient history and acknowledgement
filter against the receiving instance's current labels. A replacement thread
cannot consume a message addressed to the earlier thread; matching restarted
instances can recover it. Existing messages and sends without expected labels
remain peer-wide. The migration and handoff cases have local regression tests.
History now exposes a descending cursor with `has_more` and `next_cursor`.
It can recover more than 100 retained messages across pages after restart,
while each page rechecks the sole live instance and the cursor's peer and
session labels. Shared delivery IDs at the same timestamp are ordered by
recipient as well as delivery ID. Acknowledged text already pruned remains
unavailable. After this pagination change, the available pytest suite passed:
2,518 passed, 36 skipped, 1 dirty-package release test deselected. Repository-wide
Ruff and Mypy on all 151 source modules passed. The beta.11 development package
was rebuilt; its 151 source files and two bundled checksums match this worktree,
but `source_dirty=true`, so this is not a formal release acceptance.
Delivery IDs are now scoped to the recipient, so separate account or project
mailboxes cannot block one another by reusing a caller ID. Existing messages
and legacy globally reserved IDs survive the SQLite migration.
The read-only destination diagnostic now ignores a presence row whose process
is confirmed dead, matching the send and receive decision. It still reports
`UNKNOWN` when any unexpired competing row has unknown process identity. A
process match does not claim that the recipient model read the message.

For #185, a cancellation committed through the HTTPS gateway during browser
preparation now causes the sender to discard its prepared page and return the
saved `cancelled` state without dispatch. A separate SQLite-connection race
test, a `LazySubchatGateway` cancel test, and a real HTTP MCP transport test
verify page cleanup and zero sends.
This does not enable stopping a
provider turn after dispatch.
After this change, the available full pytest suite passed: 2,521 passed,
36 skipped, 1 dirty-package release test deselected. Repository-wide Ruff,
Mypy on 151 source modules, and diff whitespace checks passed. The development
wheel matches all 151 current source files and both bundled checksums.
Each authenticated HTTP model choice now reports
`availability_basis=authenticated_http_catalog` and
`generation_sendability=unknown` alongside the existing catalog `available`
flag. This distinguishes a listed choice from a verified generation result;
it does not change selection or dispatch behavior.
The send-capable local catalog now also supports `source=compare`: it observes
the HTTP catalog and UI model menu in separate read-only pages and marks each
choice's UI row as observed, unconfirmed, or unknown. This does not validate
effort, quota, generation, or final history, and it is not exposed by the
read-only HTTPS gateway. Synthetic controller tests cover the no-send result.
An isolated development MCP session then compared the currently selected
account's HTTP catalog and UI menu without submitting a message. It exposed a
false mapping: `5.6 / Pro` was labeled selectable through the `GPT-5.6 Sol`
row. The picker resolver now refuses a numeric-only Pro fallback when a
different role-specific row is present. A second live comparison returned
`not_confirmed` for `5.6 / Pro` while retaining the observed `latest / Pro`
row. This is UI row evidence, not generation acceptance.

The passkey issuance lock is a nonblocking final check on the HTTP event
loop. A contention regression holds the credential lock after assertion
verification and observes a prompt 503 with no authorization code or grant.
The client must start a new consent request after this busy result.
The selected-device save adapter now rechecks both the OAuth grant and route
after an awaited target catalog, immediately before each upload call. A
paused-catalog regression confirms a grant revocation or simulated route change causes
zero target dispatches. The registered SSH devices were freshly probed and
both remained unreachable, so this is a local race check rather than live
remote save acceptance.

The 2026-09-27 code audit confirmed two host-bound limits. The automatic
queue worker sends without a parent `wait` or `recover` call and persists
completion or failure before emitting an MCP `notifications/message`. Neither
the Codex nor Claude host contract inspected here proves that such a
notification enters a waiting parent model turn; test that in each host before
claiming automatic parent notification. The original SSH backend launched the
target's ordinary `anywhere mcp` under its OS user, without a child bearer.
The source now uses a distinct `ssh-child-mcp` entry point for delegated SSH
routes and never falls back to ordinary MCP after failed child authentication.
The local stdio integration covers scoped read, denial, replay and revocation;
cross-host revocation and reconnection remain acceptance work.
The source remote child router now rechecks the child, parent and bound route
after catalog discovery and just before target transport dispatch. A
catalog-wait regression revokes each boundary and confirms the target executor
is not called, with a failed source operation and a denial audit record.
Requests already inside target transport still require target-side revocation
for independent access to end.
The two-service HTTP integration now pauses a target file worker after the
source has sent the request, revokes the target child, and confirms the write
fails before creating a file. This covers the target-side race locally; public
HTTPS and an independently operated target remain deployment gates.
The notification pump also honors the host's negotiated MCP logging level:
an `info` completion event can be filtered if the host selects `warning`.
The durable queue event cursor remains the authoritative recovery channel.
Fresh `device-status` probes on 2026-09-27 reported both registered SSH
targets (`Latitude 5320` and `Windows VM`) as `unreachable`, so their real
remote acceptance tests have not run.

An isolated host-delivery probe sent the same `notifications/message` shape,
with a unique `notice_code` present only in the notification, while one MCP
tool call was outstanding. Claude Code Opus called the tool and reported that
the code was not visible in its model turn. Codex CLI GPT-6 Sol also completed
the tool call and reported that the code was not visible. The first Codex run
was denied by its no-approval policy; a second run using automatic approval
completed the tool call and is the relevant observation. These synthetic
probes demonstrate that successful MCP notification emission alone cannot
satisfy parent-model delivery in either tested CLI host. They do not prove
that every host version, UI session, or timing behaves identically. Keep
completion in the durable event feed and design a host-specific wake/steer
adapter before claiming automatic parent-turn notification.
The Codex CLI `queue` command was also tested against a live, non-ephemeral
GPT-6 Sol session during a 21-second MCP tool call. The CLI returned a queued
message ID, but the active model turn finished without seeing its unique
code. Reading the saved thread showed that the queued text became a separate
interrupted user turn after the original turn completed. An ephemeral Codex
session rejected the queue request because it had no stored rollout. Thus a
successful `codex queue` receipt is a host mailbox receipt, not proof of
current-turn delivery or an automatic resumed turn. Do not use it as the sole
completion notifier for `subchat_queue_auto`.

1. Keep original operation and intent IDs after an uncertain send. Never create
   another send because an HTTP response, SSE receipt, or notification is absent.
2. Separate catalog eligibility from the UI picker, the intercepted generation
   request, and the model reported in the saved conversation. A label or
   `latest` alias is not a permanent model identity.
3. Keep account, owner grant, child task, and device boundaries attached to
   saved operations and renewed sessions. A credential is never placed in a
   Chat prompt or Plugin resource URI.
4. Distinguish an accepted queue, a started send, a final verified answer,
   an MCP notification, and a host/model-visible notification.
5. After source tests, build a new immutable Plugin version from a committed
   source tree, check its wheel contents and hashes, install/update it through
   the existing Plugin identity, then verify fresh Codex and Claude sessions
   and the engine runtime ID. Source edits do not update a running Plugin.

For an uncertain or interrupted turn, the reviewed continuation is to inspect
the saved operation and attempt exact-ID recovery. If no final answer can be
proved, keep the original conversation blocked, inspect and cancel or disarm
its unsent queued children, and start a new Chat with a new explicit operation
ID. Do not rewrite the original operation as completed or replay its input.
The restart regression confirms that the original conversation remains
blocked while an independent new Chat can enter `sending`. A same-conversation
release would require provider evidence that the original turn cannot later
appear and a verified current branch; that mechanism is not implemented.

The current beta.11 source built as a temporary wheel, and `uvx --from` ran
the packaged `anywhere` and `anywhere-subchat` help entry points. This is a
package startup smoke check, not a release or installed-Plugin update. The
packager also built an explicitly dirty, unverified beta.11 development Plugin
in an isolated copy of the checkout. After the Library reconciler was added,
the development wheel was rebuilt and matched all 149 Python source
files, its release hashes matched the bundled files, the ZIP contained that
wheel, and Claude's manifest validator passed. Starting the Plugin's exact
Subchat MCP command from its `.mcp.json` completed `initialize` and
`tools/list` with isolated state. A second isolated `CODEX_HOME` installed the
checked-in beta.10 Plugin, then replaced its local marketplace source with the
complete development beta.11 package. Repeating
`codex plugin add anywhere-computer@personal` changed the single installed
identity to beta.11. Its cached wheel hash matched the built artifact, and
its Subchat MCP command completed `initialize` and `tools/list`. These checks
do not make the development artifact releasable or prove a send-capable
authenticated session. The
checked-in Plugin still points to beta.10: its package consistency test fails
on the Claude wheel path. The release packager deliberately refuses a dirty
source tree, so the beta.11 Plugin must be generated after the source is
reviewed and committed, then tested as an immutable package.
A later source-only beta.11 wheel contains 151 Python modules, including
`ssh_child.py`, `subchat_library_upload.py`, and `subchat_http_library.py`.
An isolated virtual environment installed that wheel without browser extras:
`anywhere-subchat-upload --help` now starts, and an actual status invocation
reports the explicit browser-extra requirement. With the browser extra, its
help entry point also starts. This wheel remains a development artifact; the
checked-in Plugin bundle and installed Plugin have not been updated.
`codex plugin list --json` currently reports the installed
`anywhere-computer@personal` as beta.10. This Codex CLI exposes
`plugin add` and marketplace refresh, but no `plugin update` command. The
user's installed Plugin was not changed. Claude Code exposes `plugin update`
and requires a new session for the updated MCP processes.
On 2026-09-27, the current beta.11 source passed the full available pytest
suite with the known checked-in beta.10 package-consistency case deselected:
2,491 passed, 36 skipped, 1 deselected. Repository-wide Ruff, Mypy on all
151 source modules, and `git diff --check` also passed. The deselected case
must be restored and passed against the immutable Plugin generated after a
reviewed commit. The locally installed Codex app reported version
`26.924.22138` on its `prod` channel and `up_to_date`; the installed Anywhere
Computer Plugin remained beta.10. Neither observation updates the Plugin.

After the peer mailbox changes, a full suite started before the latest
arming-session notification edit reported 2,500 passed, 36 skipped, and one
package-consistency failure because
the checked-in bundle still pointed to beta.10. `package_plugin.py --allow-dirty`
then rebuilt the local development bundle as beta.11. Its manifest, bundled
wheel, two release hashes, ZIP integrity and all 151 wheel Python sources were
checked against the current tree. The package test now fails only at its
intentional `source_dirty is False` release gate. This development bundle is
marked `source_dirty=true`, `validation=unverified`; it has not been installed
in the user's Codex Plugin cache. A fresh isolated `CODEX_HOME` installed this
bundle as `anywhere-computer@personal` beta.11, matched the installed wheel
hash to the bundle, and started its `anywhere --help` entry point through the
bundled dependency set. The user's installed beta.10 remained unchanged.
A second isolated `CODEX_HOME` installed the committed beta.10 marketplace
snapshot, switched its local marketplace to this beta.11 development bundle,
and repeated `codex plugin add anywhere-computer@personal`. The same plugin ID
then had exactly one installed beta.11 identity and a cached wheel hash matching
the bundle at that point. This verifies the in-place update path on the current
Codex CLI, not a release or an update of the user's live Plugin. The bundle was
rebuilt after the later queue-epoch fix, so those earlier isolated caches do
not match the latest development wheel; final cache verification must use the
immutable release build.
A fresh third isolated `CODEX_HOME` installed the post-epoch-fix development
bundle, and its cached wheel hash matched the latest source package. This is
another isolated development check, not a user installation.
Later repeated `--allow-dirty` builds reused the same beta.11 wheel filename.
An ordinary `uvx --from` launch, even with `--refresh`, displayed an older
cached CLI lacking `--target-child-id`; the wheel archive itself contained the
new option, and `uvx --no-cache --from` displayed it. This is direct evidence
that a reused development version cannot validate what an installer will run.
Dirty development bundles now add `uv tool run --no-cache` to both Codex and
Claude MCP launch configs, avoiding reuse of an older wheel with the same
beta.11 filename. The packager removes that flag for clean releases. An actual
`uv tool run --no-cache` invocation from the rebuilt bundle loaded
`anywhere_computer.__version__ == 0.2.0b11`; `anywhere status` still reported
the separately running beta.10 engine, so this does not claim a live update.
The immutable release must bump the package version, then verify the installed
wheel hash and fresh process behavior under that new version.
Rebuild without `--allow-dirty` after source
review and an authorized commit, then rerun the package test before release.
After the queue-admission and delegation-status changes, the development
beta.11 wheel was rebuilt and its exact bundled dependency command completed
`anywhere --help` in an isolated `uvx` environment. This checks packaged CLI
startup only; it does not replace the clean-source release gate or update the
installed beta.10 Plugin.

Linux has a minimized headed-browser fallback and Windows has a dedicated
Edge/account-pin path in source. The macOS non-activating CDP path is specific
to macOS. CI tests these platform branches, but neither registered remote
machine had a fresh dispatchable authenticated status during this review.
Windows/Linux generation, final-answer recovery, and focus behavior therefore
remain live acceptance gates.

The upload target also had a confirmed parent-directory swap path between
`upload_begin` and `upload_commit`. The candidate source now pins the original
parent directory identity and uses descriptor-relative publication on POSIX.
The Windows candidate pins the full directory ancestry and staging file using
Win32 handles, then checks file IDs before confirming publication. Its seven
Windows-specific tests cannot run on this macOS host; Windows CI and a real
Windows host remain acceptance gates. Do not describe Windows upload as restored
until normal publication and the swap-race tests pass there.

## Competitive issue lessons

- [agmsg #1468](https://github.com/fujibee/agmsg/issues/1468) and
  [#1346](https://github.com/fujibee/agmsg/issues/1346) motivate explicit
  peer session/thread diagnostics. A live process does not prove the intended
  current thread receives a message.
- [Claude Code #90264](https://github.com/anthropics/claude-code/issues/90264)
  motivates separate states for queued input, permission wait, and orphaned
  work. A parent should be able to discover and recover saved operation IDs.
- [Browserbase #191](https://github.com/browserbase/mcp-server-browserbase/issues/191)
  and [#187](https://github.com/browserbase/mcp-server-browserbase/issues/187)
  motivate checking browser/session lifecycle after a successful start and
  releasing owned resources after disconnect.
- [Codex #19425](https://github.com/openai/codex/issues/19425) and
  [#37143](https://github.com/openai/codex/issues/37143) motivate separating
  Plugin catalog visibility from callable tool readiness. Discovery alone is
  not an execution or model-turn delivery test.

These competitor reports inform local tests. They do not establish that the
same defect exists in Anywhere Computer.

## Re-evaluation of the three Pro proposals

An additional read-only review with Opus 5.5 on 2026-09-27 identified three
contracts to investigate before extending autonomous delivery. The shared
local ledger's automatic queue event is owner-scoped rather than bound to the
host session that armed it; the earlier MCP notification could therefore
appear on a different running controller. Event paging has no parent-consumption ack.
The notification is now emitted only to the live stdio session that armed the
queue, even when another controller wins the completion event. A restarted
controller recovers the durable event but does not impersonate that session.
The subscription also checks its arm epoch, so a different controller's re-arm
cannot deliver the newer outcome to the old arming session.
This still does not prove delivery into a parent model turn or provide a
host-level consumption ack.
The peer mailbox now also persists an expected session/thread label and keeps
messages pinned to another session unread after a handoff. A remote delegated
child bearer is opaque to the source;
the source cannot infer its target-side scope or revocation from local state.
These are code-review hypotheses, not verified end-to-end defects. Before
changing contracts, reproduce each with a regression case and distinguish
mailbox receipt from an actual model turn. A durable event ID must remain the
deduplication key for any future host wake adapter.

1. Prioritize reconnection and observable outcomes. The automatic queue is
   already durable and can resume when a send-capable controller restarts, but
   it cannot run while every controller is stopped. A saved terminal event and
   an MCP notification do not prove that the parent model saw the result.
   The next acceptance test should exercise notification delivery in an actual
   parent host session and recovery after a lost notification. Keep the saved
   operation ID as the source of truth.
2. Implement provider stop or steer only after observing an authenticated,
   same-turn request and a reliable receipt on the supported transport. The
   current local cancel is intentionally restricted to work that has not been
   sent. Treat a missing or ambiguous receipt as an unknown operation and
   recover its original ID; do not create another send.

   A 2026-09-27 selected-account UI probe sent three synthetic turns into one
   test conversation and clicked its visible Stop button during each active
   generation. The browser sent `POST /backend-api/stop_conversation`; the
   observed request body contained only `conversation_id` and
   `exclude_async_types`. The request headers had account and browser context
   but no observed turn ID. The endpoint returned HTTP 200 with `status` and
   `last_message_id` keys. An authenticated, read-only history request then
   showed `finish_details.type=interrupted` and
   `finish_details.reason=client_stopped` for the three assistant turns. This
   proves the current UI can stop the conversation and the provider records
   interruption. It does not provide a pre-dispatch exact-turn guard: a
   different turn could start in the same conversation after a history check
   and before the conversation-wide stop POST. Keep operation-scoped stop and
   steer unsupported until that race is addressed or a turn-bound provider
   contract is observed. The test conversation is a benign account fixture;
   it has not been deleted.
3. Expand autonomous scheduling only with per-job opt-in, bounded lifetime and
   usage, and an outcome the owner can retrieve. [ChatGPT Work](https://help.openai.com/en/articles/20001275/)
   already has scheduled and event-triggered tasks, so a new Subchat scheduler needs a
   concrete use case beyond the current predecessor queue. Pro availability
   and usage limits must be checked at dispatch, not inferred from a stable
   label or an older Tasks article.

The present `subchat_refresh_auth` checks that the selected account still
matches. It is not an independent login or renewal protocol. A future renewal
flow must preserve the account and owner boundaries of every saved operation.

## Queued model-change transport check

On 2026-09-27, a source controller using a selected and account-pinned macOS
Chrome profile sent a short parent prompt as GPT-5.6 Sol Instant. The first
`subchat_wait` returned a failure while the saved operation was still `sending`
with a generation HTTP 200 receipt. A new controller recovered the same parent
operation ID from history and verified its final `READY` answer; it did not
submit another parent message. A child was then saved as `queued`, changed with
`expected_revision=0` and a fresh GPT-5.6 Sol `中程度` choice ID, and recovered
under its original ID. The final child answer was `MODEL-CHECK`, and the saved
reported settings were `gpt-5-6-thinking` with `standard` effort. This verifies
the wire choice for one account and one macOS transport run. The initial wait
failure remains a separate diagnostic item; HTTP 200 alone was not counted as
completion.

A later read-only check of the selected account's authenticated HTTP model
catalog found GPT-6 Pro under `version_id=latest`, with an available
`gpt-6-pro` Pro preset. The same catalog listed GPT-5.6 Pro under `5.6` and
GPT-5.5 Pro under `5.5`. The current UI picker showed an enabled `最新` row;
opening it exposed Pro as its fifth effort position. Source beta.11 MCP then
sent one benign new Chat using the fresh GPT-6 Pro choice ID. A test-client
argument error ended its first MCP session while the saved operation was
`prepared`, before the durable dispatch boundary. After status confirmed that
state, the exact same request ID, intent key and prompt resumed it. The saved
operation progressed through `submitted` to `completed`, and history reported
`model_slug=gpt-6-pro` with null thinking effort. Its final answer warned that
a retried child-completion notification could trigger duplicate parent work
without deduplication. This verifies one currently sendable GPT-6 Pro choice;
the `latest` group and its picker label are not permanent model identities.

A fresh read-only check through the user's still-installed beta.10 Subchat
Plugin on 2026-09-27 again found `latest / Pro` with wire slug `gpt-6-pro`,
while the explicit `5.6 / Pro` choice used `gpt-5-6-pro`. The UI listed the
enabled `最新` row and, when that row was explicitly inspected, Pro at position
five of five; no message was sent in this check. The first general UI read
returned `efforts_unconfirmed`, so an enabled model row alone did not prove
its effort was selectable. Installed beta.10 rejected `source=compare`; that
new combined view exists only in the current beta.11 source. The catalog and
model-menu suites passed 58 tests. This read-only observation does not replace
the earlier verified GPT-6 Pro generation, nor guarantee a future `latest`
mapping.

The current catalog contains no separate `version_id=6` entry. Therefore
"choose GPT-6 Pro directly" currently means selecting the available
`latest / Pro` choice whose exact `model_slug` is `gpt-6-pro`, preferably by
its `choice_id`; the model title alone is not a complete wire selection.
Source preparation re-reads the catalog, resolves an enabled UI row, and
checks Pro effort. The intercepted browser generation request must then carry
`model=gpt-6-pro`; the observed GPT-6 Pro composer uses
`thinking_effort=standard` although this catalog preset records null, and
the validator allows only that specific observed difference. HTTPX forwards
the browser-prepared request once with its account and origin binding. If a
future `latest / Pro` moves to another slug, an old GPT-6 Pro choice fails
before dispatch instead of silently following the new model. A future explicit
`6 / Pro` route is supported by the selection logic if observed in both the
authenticated catalog and UI, but has not been observed in this account.
Claude Code 2.1.281 running explicit `claude-opus-5-5` reviewed this route
read-only. Its first pass missed the later recorded GPT-6 Pro live send; a
targeted reread corrected that and distinguished the proven `latest` choice
from the unobserved explicit `version_id=6` route. The local Pro/Latest
validation checks also passed 24 targeted tests after this review.

The HTTP generation history adapter also masked authentication and selected-
account failures as a generic connection error in `find_submission` and
`read_answer`. It now retains those specific exceptions after recording the
failed history checkpoint, allowing the MCP layer and automatic queue to
report an authorization problem accurately. Four synthetic regression cases
cover both observers and 401/account mismatch. This was a demonstrated source
defect, not proof that it caused the earlier live first-wait failure; the exact
exception from that run was not retained.

## Live attachment observation

The selected account's Chat composer uploaded three harmless test text files
through an observed file-create, presigned PUT, and processing sequence. Its
request specified `store_in_library: true`; all three names then appeared in
Library search even though no generation message was sent. The read-only
Library search also returned each exact `file_id` with `state=ready`. A future
Library upload can use that exact ID for recovery after the initial create
response; an interrupted create before an ID is observed must remain unknown
and must not be retried automatically. The existing
`file_*` attachment reference therefore must not be described as a local
file upload or as Chat-only storage.

Two more synthetic text files were tested from the selected account's Library
UI after the create/PUT/process checkpoint design was added. The observed
`POST /backend-api/files` returned 200 with `file_id`, `status` and
`upload_url`; the presigned PUT returned 201; `POST
/backend-api/files/process_upload_stream` returned HTTP 200 with
`text/event-stream`. The create request set `store_in_library: true`,
`library_persistence_mode: required`, `use_case: my_files` and a resolved
`text/plain` MIME type. The processing request carried the file ID, name,
MIME type, retrieval-index flag and Library metadata. No Chat generation was
sent. The Library UI's visible results were inconsistent across fresh browser
sessions, so UI display alone was insufficient. An authenticated, account-pinned
read-only `POST /backend-api/files/library` with `source: uploaded` and
`ranking: suggested` subsequently returned both exact filenames, byte sizes,
file IDs and `state: ready`. Its item identity field is `id`, with a distinct
`libfile_` value, not `library_item_id`. The source parser now accepts that
observed field and an in-memory ledger reconciled both live response items to
`ready` using their exact file IDs and selected account. This establishes the
two new Library items; it does not prove a byte-for-byte remote digest because
the list response contains no provider digest. Removal of the five fixtures
awaits owner approval.

A read-only HTTP Library reconciler now issues the observed, exact-origin
query with the in-memory account-bound session, follows at most 20 bounded
cursor pages, and stops on changed accounts, authentication failure, invalid
responses or repeated cursors. It never uploads or searches by filename alone
when a create response was lost. A live call with one fixture's saved file ID
returned `ready` and the distinct `libfile_` identity through this new helper.
The suggested-order scan covers at most 2,000 entries. If the exact file is
outside that window, the result remains unconfirmed; the operator must use
Library to inspect it, and no upload retry follows. The currently observed
Library list endpoint did not provide an exact file-ID filter. Its `ready`
value confirms only matching Library metadata, not remote byte digest.

The installed beta.10 Subchat Plugin then attached one of these exact ready
items to a new Chat under the selected account. A fresh GPT-5.6 Sol Instant
choice from the authenticated `5.6` catalog supplied the model, effort and
wire selection. The saved operation reached `completed`; its final answer
reproduced the test file's only line and cited the attached file. The saved
reported model was `gpt-5-6`, while the chosen catalog preset slug was
`gpt-5-6-instant`, matching the observed Instant alias rule. This establishes
one real already-uploaded Library-to-Chat attachment and final-answer path.
It does not implement uploading a local path through the Plugin or establish
that every listed model choice is currently selectable and sendable.

The source now contains an owner- and account-bound Library upload checkpoint
ledger with exact file-ID and ready-item reconciliation. A durable
`claim_create` check now admits only the first file-create attempt for one
operation ID, including across two database connections and a restart. Rows
created by the older schema migrate as already claimed because their previous
dispatch status is unknowable. The next PUT and processing requests now have
separate one-time claims and confirmed-response checkpoints. A processing
claim requires a confirmed PUT response; a successful processing response
still does not mean the Library item is ready. Older rows without these
checkpoints migrate with both network stages claimed, refusing automatic
replay. The ledger makes no provider upload call and
does not attach a file to a Chat. Library search metadata does not prove a
byte-for-byte match without a provider digest or download check.
For a browser UI upload, `claim_ui_batch` durably reserves all three stages
before selecting the file, since the browser can dispatch the full sequence
without an intermediate controller callback. An interruption before the create
ID is captured remains unknown and cannot reuse the same operation ID. The
caller must distinguish a proven pre-dispatch failure from an uncertain send;
the ledger alone cannot infer that from a missing receipt.

The separate Plugin MCP Library upload now requires a local `--prepare` for
the exact path, content digest, selected account, and operation ID. This
authorization expires after 15 minutes, is distinct from an ordinary ledger
reservation, and is checked again under the one-shot claim lock. An expired or
changed file fails before a browser upload; uncertain dispatches retain the
original operation ID for read-only status recovery.

The candidate `anywhere-subchat-upload /absolute/path/to/file --operation-id
<32-hex-id>` command now pins at most 20 MiB of local bytes, uses the selected
macOS Chrome account, and observes the Library UI's create/PUT/process sequence
under that one operation ID. It can read-only reconcile a saved file ID after
an interruption; `anywhere-subchat-upload --status <operation-id>` does this
without the original local file and never sends upload bytes. In the current
live UI, `/library` and its unique hidden file input match this implementation.
Live CLI runs revealed that the input appears after DOM content loads, so the
controller now waits for it before claiming the one-shot browser batch. The
request's root `file_id` field, create response, presigned PUT, and processing
stream were observed with synthetic text files. The processing HTTP 200 body
is newline-delimited JSON despite the `text/event-stream` content type. One
observed sequence ended with `file.processing.completed` at progress 100 and
the same file ID; it then reconciled to Library `ready` and a `libfile_` ID.
Repeating the source command with its original operation ID and using
`--status` without the source both returned the same saved ready identities;
neither dispatched another upload.
On 2026-09-27, another current-source macOS CLI run uploaded a new synthetic
text file to the selected account's Library. An initial `/tmp` path was
rejected before reservation because macOS redirects it through a symlink;
the canonical `/private/tmp` path was then used with the same operation ID.
The first upload returned a saved `file_id` and `unknown` state after the
browser create/PUT/process sequence. A read-only `--status` for that exact ID
returned `ready` with a `libfile_` item ID. This proves one end-to-end
current-source Library upload and eventual metadata reconciliation. The
synthetic Library item remains in the selected account. It does not prove
byte-for-byte remote integrity, direct Chat attachment, or selected-device
save from a real Chat file.
Because the live run needed a second read-only search a few seconds later,
the initial upload now makes at most three exact-ID Library observations,
with two-second gaps, before returning a still-unknown checkpoint. These
observations cannot dispatch another create, PUT, or processing request.
A mocked Library indexing-lag regression passed; the focused upload suite
passed 27 tests, along with Ruff and Mypy for the changed source. This retry
behavior has not yet been repeated against the live account.
The controller now requires that exact terminal event before marking the
processing stage confirmed. Four other synthetic runs reached a saved file
ID and recorded stage receipts but their exact Library searches remained
`unknown`; repeating `--status` on the first stayed unknown. These outcomes
must not be represented as ready or retried under a new operation ID without
an explicit new upload decision. The five new benign Library test files join
the five earlier fixtures awaiting owner-authorized cleanup. The candidate
CLI remains absent from the installed beta.10 Plugin.
On a later read-only `--status` pass, two of those four unknown operations
reconciled under their original file IDs to distinct ready Library items.
The other two remain `unknown` despite confirmed PUT and processing response
checkpoints. No upload bytes were resent. Across the five CLI fixtures, the
current ledger now records three ready and two unknown results; the source
does not infer remote readiness from a processing HTTP 200 alone.
A later read-only status check left one of these two operations `unknown`.
The other Library search timed out, and its local checkpoint still records
`unknown` with confirmed PUT and processing receipts. The CLI now reports a
network timeout as an `unverified` outcome with the original operation ID,
`automatic_retry=false`, and a nonzero exit instead of a raw traceback.
The Library UI search uses a separate `POST /backend-api/global/search` with
`source_requests` for `library`. A read-only exact-name search for one ready
CLI fixture returned its saved file ID in `items[].payload.file_id`, whereas
an exact-name search for one
unknown fixture returned neither its saved file ID nor its filename. This
cross-check supports the current unknown classification; it does not establish
why the item is absent or authorize another upload attempt. The UI-generated
request also includes the current external-storage account filters, so it is
not yet a stable replacement for the exact-ID, account-pinned Library list
reconciler.

An additional synthetic request-shape fixture confirmed that the UI's file
create request carries `file_name` and integer `file_size`. The upload
observer now accepts only a create response for the selected file, then
matches the PUT by the returned upload URL and processing by the saved file
ID. An unrelated same-page create, PUT and process sequence is covered by a
regression test. A fresh synthetic upload through the source CLI saved its
file ID as `unknown` on the first immediate scan and reconciled to a distinct
`libfile_` ID and `ready` on a later read-only `--status` call; no bytes were
resent. These two additional benign fixtures also await owner-authorized
Library cleanup.

Automatic queue completion events now append to a separate owner-scoped
history, so rearming the same queued operation no longer erases a prior
failure or disable event. Existing single-row events are migrated once.
`subchat_queue_events` can read that history with `after_id=0` followed by
`next_cursor`, returning events in ID order with `has_more`. This lets a
controller recover more than the recent-list limit after missing a logging
notification. The controller now places the same committed event ID in its
MCP notification, so a host adapter can deduplicate a retried completion
notice before launching parent follow-up work. A test compares that ID with
the cursor page. The HTTPS gateway now exposes the same durable event cursor
as a separately scoped, owner-bound read-only tool. A real loopback HTTP MCP
test verifies that the event is visible only to its grant and that another
scope cannot list or call the tool. This gives ordinary Chat a recovery read,
but does not prove host or model delivery or automatic parent wakeup.
After OAuth re-consent, the new grant can recover an older queue's events by
supplying its exact operation ID. The gateway applies the existing same-principal
and selected-account checks used for status recovery; an unfiltered event list
does not enumerate old grant rows. Cross-client and wrong-account cases are
covered by a regression test.
The direct HTTPS gateway now separately scopes `subchat_queue_auto`. Its
service-owned controller keeps an armed worker alive across HTTP client
disconnect, and a fresh HTTP MCP connection can read the terminal event. A
real loopback OAuth/MCP test covers scope isolation, client disconnect, one
child send, and event recovery. Service restart still needs a subsequent
send-capable gateway session to resume the durable arm; parent model wakeup
remains unverified.
The worker retains the grant that armed the queue and checks it before
reserving a child send. Revocation records `authorization_lost` and leaves an
unsent child queued; a fresh same-principal grant can read that event by exact
operation ID. A real loopback OAuth/MCP test covers this boundary.
Admission now checks both durable armed rows and still-running controller
workers before saving a new arm. Disabling eight queues while their workers
are still exiting no longer lets a ninth call report failure after writing an
orphaned armed row; a regression test verifies rejection leaves no new arm or
worker and the same operation can be armed after capacity clears.
If a queue is rearmed while its earlier session task is still exiting, that
session starts a successor task after the earlier one finishes. Each arm has a
durable epoch; an old worker cannot end the newly armed queue using a stale
observation. A test holds a provider observation across rearm and verifies
the new worker remains armed after the old observation fails. This covers
the local controller lifecycle; it does not imply delivery to an ordinary
Chat parent without a live, authorized handoff.

The line-oriented local controller now forwards the explicitly requested
`include_prompt_preview` flag to the owner-scoped list operation. Previously
that controller silently dropped the flag even though the store and MCP
gateway supported it; a CLI regression checks both the default-hidden and
opt-in preview responses.

The setup CLI now has a no-send `doctor` action. It checks the selected profile
and pinned Chat account, then reports configured read/send tool groups while
explicitly leaving the running MCP catalog unobserved. On this macOS host,
the selected account matched and background send was configured. This is
setup evidence, not proof that a subsequent generation will succeed.

## Live direct-save observation

A real Chat account produced a 19-byte sandbox file. A local save under one
save ID reached `upload_begin`, but the first recovery stayed at `uploading`:
the request used macOS `/var/...` while the local upload status reported the
same parent through `/private/var/...`. The runner and journal compared those
strings literally. The source now compares resolved parent identity and the
same basename for local targets only; remote target paths still require exact
equality. A local Engine/Router/Uploads regression completes all four stages
through a parent-directory alias without replaying `upload_begin`. The original
live save ID has not been resumed with its original authorization context, so
that original request remains unresolved. A separate read-only replay of the
same completed Chat source through the selected account gateway, verified
download adapter, local DeviceRouter, and Uploads completed under a new local
save ID. Its states were `running`, `running`, `unknown`, `completed`; the
resulting 19-byte file matched the original source SHA-256. This proves the
real Chat-to-local-engine path with a synthetic local grant, not the full
OAuth HTTP service or a remote device.
On 2026-09-27, an isolated current-source OAuth authorization store issued an
actual bearer for `subchat_send`, `subchat_save_file`, and the local upload
tools. A loopback HTTP MCP service used the selected macOS Chat account and a
fresh private Subchat ledger. Its authenticated HTTP catalog supplied the
`5.6 / Instant` choice. One new ordinary Chat returned a verified final
sandbox link; `subchat_save_file` under the same bearer advanced through
`running`, `running`, `running`, `unknown`, `completed` without a new save ID.
The 42-byte published file matched the requested synthetic content and the
durable save journal's SHA-256; reopening that journal showed `completed` and
the same byte count. This is real provider source, OAuth bearer, HTTP MCP,
gateway, and local Engine/Uploads acceptance on macOS. The service and target
were isolated to loopback; it does not prove public ChatGPT app connectivity,
another user's grant, or publication to a physically remote device.
On 2026-09-27, a new ordinary Chat was sent through the installed beta.10
Subchat Plugin using the authenticated `5.6 / Instant` HTTP catalog choice.
The final answer contained one sandbox file link, and the installed download
tool fetched its 44 bytes. The current-source verified download function then
read that same saved, account-bound final answer and fed the current
`SaveRunner` and `RoutedSaveTarget` into a fresh local Engine/Uploads target.
The save progressed `running`, `running`, `unknown`, `completed`; its published
44-byte file matched the expected content and saved SHA-256. The local target
and journal were isolated and removed after verification. Both registered SSH
devices were freshly probed and remained unreachable. This adds a second live
Chat-to-local-engine observation, but the target grant was synthetic and the
full OAuth HTTP `subchat_save_file` call or a remote-device publication is
still unverified. The initially read submission JSON omitted the account ID
by design; its separate binding row matched the current selected account.

The save request also now accepts a canonical absolute Windows destination
for a selected remote device even when the service runs on macOS or Linux.
Drive-relative paths, traversal components, and mixed separators are rejected.
This validates the request boundary; a real remote Windows upload and its
reported path still require end-to-end verification.

New target uploads now retain their original requested path alongside the
canonical publication path. The save journal can reconcile an authenticated
remote status using the exact original request even when the target resolved
a directory alias. Legacy records without that field retain exact remote
path comparison. The schema migration and mismatch rejection pass local tests;
remote transport and Windows publication remain live gates.

Another 2026-09-27 probe found the registered Windows VM unreachable over SSH.
`anywhere http-show` reported no valid HTTP service configuration on this
machine, so public HTTPS passkey and remote HTTP acceptance cannot begin from
the current local setup. A Codex desktop update check reported production
version `26.924.22138` as `up_to_date`; this says nothing about the installed
Anywhere Computer Plugin, whose live Subchat capability still reported
`0.2.0b10`.

Fresh `anywhere device-status` checks on 2026-09-27 returned `unreachable`
for both saved SSH targets (Latitude 5320 and Windows VM). The previous
`ready` entry for Latitude was a cached observation over eleven hours old.
Neither target was eligible for a remote save or Windows upload acceptance
run at that point.

The source MCP catalog now offers explicit `devices_probe` for a current
authenticated SSH or HTTP observation without running a target tool. It
returns the fresh check time and state; `devices_list` remains a cached list.
SSH and HTTP probe paths have isolated router tests. This helps distinguish
registered endpoints from currently usable ones, but does not make either
unreachable test machine available.

On 2026-09-27, an isolated loopback OpenSSH `sshd` with a fresh Ed25519 host
key, pinned `known_hosts`, noninteractive client key, and high local port
accepted a real SSH connection from `SSHBackend`. The remote command ran the
current MCP Engine over SSH stdio. Initialization, `tools/list`, `files_write`,
and recovery of the same operation ID all succeeded; the written bytes matched.
The daemon and keys lived only in a temporary fixture and were stopped after
the run. This verifies OpenSSH framing and the operation-ID path across a real
SSH socket. It used ordinary MCP, so it does not prove the child-bearer remote
entry point, revocation across hosts, or either registered device.

A second run used that loopback OpenSSH daemon with the `SSHChildSession`
entry point and a separately issued child bearer. An incorrect bearer failed
before catalog access; the correct bearer exposed only `files_read`,
`operations_get`, and `delegated_identity`. An allowed read succeeded, an
out-of-root read returned `dispatched=false`, and revocation stopped a later
catalog call on the same SSH connection. The first allowed-read attempt used
macOS's `/var` alias and was correctly denied by the descriptor-confined file
executor, which requires canonical absolute paths without symlink ancestors;
using the resolved `/private/var` path passed. This establishes live loopback
SSH transport plus child authentication and scope. A separate physical host,
cross-host expiry/revocation after reconnection, and owner provisioning still
need acceptance.

The loopback exercise also exposed a precheck mismatch: a symlink alias that
resolved inside a permitted local read root passed the child grant check, but
the descriptor-confined file executor later rejected that same path. The
precheck now requires the caller's absolute path to equal its resolved path,
so both layers reject the alias before an operation is reserved. A regression
test failed against the old precheck (`dispatched=false` without a policy
reason) and passed with the change (`path_out_of_scope`); the related
delegation, confined-file, and SSH suites passed 14 tests, plus Ruff and Mypy.
The dirty beta.11 development Plugin was rebuilt after this source fix and
its five development package checks passed. The formal clean-source release
check still requires a committed tree and was deselected for this candidate.
The broad suite after the fix completed with 2,538 passed, 36 skipped, and
that one clean-source test deselected in 348.28 seconds. Mypy passed on all
152 source files and Ruff passed for `src` and `tests`. These checks do not
replace public HTTPS, physical passkey, or independent remote-host acceptance.
