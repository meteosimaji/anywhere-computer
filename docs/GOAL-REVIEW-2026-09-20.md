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

The durable submission lifecycle is in `src/anywhere_computer/subchat.py` and
`subchat_state.py`; the browser adapter is now `src/anywhere_computer/subchat_browser/`. PR 61 merged
these components at `90ef7a352011925980ced6c713c98085f01e71f7` after all five CI
jobs passed, including Windows.
The `anywhere-subchat --mcp` entry now exposes the lifecycle via local stdio MCP.
Two parallel ordinary Chats have used the installed Anywhere direct-MCP route
to that development entry and returned independently identified final answers.
PRs 77–79 merged after all five CI jobs passed; 36 targeted tests passed against
their combined main state `5b85afef6f9bdbb5c8a48e101d8c305dfe67fbd0`.
The installed engine's version is still `0.2.0a1`; this does not establish that
its bundled subchat source has been updated. Model/effort discovery,
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
stopped before sending. The literal input adapter now preserves line breaks and exact saved-message
serialization in the offline browser fixture. Live ordinary Chat creation and
a same-conversation file-edit follow-up also succeeded. These results resolve
the earlier input probe defect; whitespace normalization is not used to hide it.

Development probes previously closed their dedicated Chrome context in `finally`
after each observation. This caused visible repeated window closure, not evidence
of a browser crash. Interactive diagnosis now holds one dedicated session open;
intentional restart acceptance should be a separate, explicitly identified step.

## Scope control

The broader PTY/ConPTY, native GUI, browser-provider, onboarding and durable-task
roadmap remains tracked work, not evidence of this goal's completion. Do not
silently discard those requests or turn every new upstream feature into a release
gate. Complete the selected subchat/context/recovery work before opening another
orchestration framework. Review upstream at pinned revisions; the initial review
pinned CoS at `8f76ccc790917b01ee758da6687a1cf9b576ba8a`. The later review
below pins the newer main separately.

No completion percentage or delivery date is established by test counts. The
largest remaining gap is production integration and end-to-end acceptance, not
another standalone probe.

## Shared work is a subchat acceptance requirement (2026-09-20)

The current `SubchatSubmission` stores prompt/model/effort and message/result
identity and an optional persisted work context (device/workspace/task provenance).
That context does not authorize or enforce routing. Actual ordinary Chat reviews
have read the specified repository and run isolated local regression artifacts;
this proves bounded shared work, not child-specific authorization or safe arbitrary
concurrent edits.
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
from conversational claims. The real engine and MCP/HTTP file handoff tests now cover conflicts and result
recovery; the controller supplied the path explicitly in live Chat trials.
Persistent product work-context binding remains unimplemented. Per the clarified
acceptance policy, deterministic real MCP/HTTP tests are the functional gate;
live Chat trials supplement them. A model-reported rejection is not an established
root cause or, on its own, proof of a plugin defect.


## Follow-up comparison and HTTP acceptance (2026-09-20)

The GitHub main endpoint and the local upstream ref both identified
[`04c6a298078817cf25c197f2b8bb639f8239243c`](https://github.com/totec448-spec/chat-on-steroids/commit/04c6a298078817cf25c197f2b8bb639f8239243c),
merged by [CoS PR 332](https://github.com/totec448-spec/chat-on-steroids/pull/332).
This is a source comparison, not an independent execution of CoS's reported tests.

| Upstream change | Anywhere decision and evidence |
| --- | --- |
| Delayed-final queue binds the exact source conversation/turn, rather than rejecting a late event using its old timestamp | Preserve the existing operation-bound queue. `SubchatSubmissions` persists `after_operation_id` and `expected_last_user_message_id`; `test_queue_restart_pending_completion_and_lost_receipt` verifies completion after reopening the ledger and no resend after a lost receipt. No parallel timestamp-based queue was added. This is not evidence that all of upstream issue 327 is resolved. |
| Only an actual tunnel control-plane rejection invalidates credentials | Preserve the principle, do not port the provider-specific regex. Anywhere's `cloudflare_tunnel.run_tunnel_child` discards provider logs and reports exit/connector errors separately; it does not classify arbitrary log text containing 401/403. Public reachability remains explicitly unverified after credential handoff. |
| Background rendering custody and live shell recording | Do not copy Chrome-companion/document-custody machinery into the HTTP reader. PR 96 instead reduces repeated page work to HTTP GET after authenticated bootstrap. Initial bootstrap and generation still require separate browser integration. |
| Provider-specific model/Pro selection | Do not adopt model-name defaults. Retain observed model/effort discovery and exact selection, including the distinction between versioned 5.6 Pro and latest Pro. |

CoS's own `docs/worklog-2026-09-20-delayed-final-queue.md` distinguishes synthetic
production-bridge event-order tests from the reporter's unverified live acceptance.
Anywhere keeps the same distinction in its evidence; upstream test counts are not
Anywhere acceptance results.

[PR 95](https://github.com/meteosimaji/anywhere-computer/pull/95) merged as
`fe4c5a5` after all five CI jobs passed. It correlates saved HTTP answers to the
exact original input and refuses interrupted success-looking output. The
separate [PR 96](https://github.com/meteosimaji/anywhere-computer/pull/96) adds
HTTP-only repeated retrieval and optional verified window minimization. At this
record's creation PR 96 CI and main integration were still pending.

PR 96's live production reader retrieved the known final text and exact answer
ID, then repeated retrieval twice with new-page creation forbidden. The actual
CLI also returned the exact answer and exit 0 in an isolated ledger. Controlled
real HTTP transport tests cover disconnect, 302, 429, 500 and 401/403; these do not
claim a real account was logged out or rate-limited. Final combined subchat and
package verification passed 129 tests; Ruff and mypy passed. The prior source
fails the repeated-navigation regression. Installed runtime was not replaced.

Remaining HTTP work is generation dispatch, model-change/stop contracts,
streaming-result recovery and authenticated lifecycle acceptance across process
and login changes. Saved-answer HTTP recovery is not proof that these are done.

## Integration review, 2026-09-21

Main `1758a65ba261c739d65c65a13abd648a13ab350e` contains PRs 142–145:
async-final correlation, browser-free HTTP reads, precise queue/cancel capability
reporting and independent HTTP recovery. Each PR passed its five platform CI
jobs before merge. PR145's final source passed 309 subchat tests locally; its
verified CI head was `62885996b21dd399a670d2a51477c6ba4985cc47`.
The runtime contract and credential limitations remain owned by
[the HTTP guide](SUBCHAT-HTTP-RESEARCH.md), not this dated review.

Two additional isolated acceptance harnesses used the product controller in a
separate process with real stdio MCP and real HTTP redirected to localhost only
by the harness. A blocked conversation A did not prevent B's distinct answer.
The same result passed through the actual `DirectMCPSessions` wrapper using a
10 ms wait followed by B recovery and then A recovery: exactly two HTTP requests,
correct answer ownership and confirmed subprocess cleanup. Browser launch was
forbidden. This validates short polling through the serial outer wrapper, not
simultaneous outer calls or a live-provider parallel generation limit.

A separate controlled browser-adapter test bootstrapped synthetic read credentials,
marked that context closed, recovered the exact answer through its HTTP client,
and then rejected repeated reads after a 401 without another request or page.
This tests the real adapter/reader with a synthetic context/provider; it is not
an additional real Chrome-crash or account-expiry experiment.

Upstream main was rechecked and remains `7777e517603289696bb5febddb9bbf51cdde9aea`.
[Issue 339](https://github.com/totec448-spec/chat-on-steroids/issues/339) reports
listening on a local port without companion authorization. Preserve the separation
between a live process, authenticated connection and successful operation; do not
copy its companion pairing design into subchat or equate installed tools with
working ordinary-Chat generation.
[Issue 347](https://github.com/totec448-spec/chat-on-steroids/issues/347) reports a
Windows Defender detection of CoS's packaged `tunnel-client.exe`. Neither its cause
nor a false positive is established by that report. Anywhere uses a separate
optional cloudflared adapter and a pinned, hash-checked CI download. No Defender
exclusion, security setting change, replacement tunnel or claim of equivalent
failure is adopted from this report.

This integration does not finish the goal. The next subchat acceptance remains
quiet new submission plus an established authentication lifecycle; recovery alone
cannot satisfy it. Native steer, automatic queue delivery and child-specific
permission isolation are still separate unmet requirements. Audio capture
integration and real device/update recovery remain in the ordered work above.
Published releases and resident installations were not updated by these merges.

## Audio integration and remaining acceptance, 2026-09-21

PR 147 merged at `3188046984e42f4073a622b68dfb6c594e90e058` after all five
Quality jobs passed on `38f1baa3d8de61581d3043c52c8d7890755470b8`.
The final local suite passed 1,639 tests with 19 skips. Optional system-audio
capture now uses the existing engine ledger; its current contract and live versus
synthetic evidence belong to [the audio guide](AUDIO-PROBE.md). This is source
integration, not a release, resident update or new live recording acceptance.

The earlier documentation PR's Windows run failed twice on different reconnect
paths: initial agent readiness and concurrent HTTP catalog recovery. A later
passing runtime run does not identify or repair their cause. PR 148 retains
bounded disposable-child stderr and the actual failed catalog envelope, and runs
reconnect tests before the parallel suite without removing them from that suite.
Its native Windows pre-parallel reconnect step passed; full CI was pending when
this record was written. Production deadlines and permission checks are unchanged.

Remaining requirements are not discharged by these passing tests:

| Requirement | Evidence still needed |
| --- | --- |
| Quiet new ordinary-Chat submission | Accepted send and correlated final output with no foreground browser intervention; current standalone HTTP adapter is read-only. |
| Authentication lifecycle | Supported initial login and expiry/restart recovery, rather than only an explicit in-memory handoff. |
| Queue and immediate communication | Automatic queue delivery with bounded lifetime and non-interference; verified caller/turn binding for tool-boundary messages or native steer. Unsupported modes remain explicit. |
| Shared work and child permissions | Authenticated child identity or a scoped broker; descriptive task/workspace strings do not restrict a shared connector grant. |
| Context capture | Packaged-engine live system capture and explicitly selected microphone/device-loss acceptance; no implicit phone/default input. |
| Deployment and recovery | Installed-runtime version evidence and real update, sleep/reboot and Windows recovery acceptance. |

The existing competitor decision table remains in the
[reliability comparison](CHAT-ON-STEROIDS-REVIEW-2026-09-18.md). No second task
engine, speculative retry loop or copied companion-authentication scheme was
added to turn these missing requirements into apparent successes.
