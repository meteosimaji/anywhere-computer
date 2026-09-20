# Chat On Steroids comparison and applicable reliability work

Reviewed on 2026-09-18 against Anywhere Computer beta 1 (`2966376`) and
[Chat On Steroids source `2f9acf307189ed1f05bee0cdc97871fdcff1d8f5`](https://github.com/totec448-spec/chat-on-steroids/tree/2f9acf307189ed1f05bee0cdc97871fdcff1d8f5),
whose package version is 2.1.14. The issue inventory returned 138 issues, including
60 open issues. Issue titles were triaged; relevant bodies and the source paths
below were inspected. An open issue is a report or proposal, not proof of a
reproducible defect in either project's current release. Closed reports can still
identify a useful regression case. No CoS application or its private accounts
were exercised in this review. No upstream source was copied or redistributed.

## Product boundary

CoS combines an Electron desktop client, browser companion, conversation/worker
orchestration, native desktop tools, workspace access and MCP plugins. Anywhere
Computer is an execution service selected by the calling AI: local and routed PC
file/terminal tools, stateful MCP adapters, Skills and recoverable operation results.
The browser companion's model menus, conversation compaction and worker scheduler
are not prerequisites for this execution service. Adding them would introduce a
second conversation owner and dependence on ChatGPT's changing interface.

## Decisions and implementation

| Upstream evidence | Anywhere Computer assessment | Action |
| --- | --- | --- |
| [#138](https://github.com/totec448-spec/chat-on-steroids/issues/138): user-installed uv missing from a resident Windows process's PATH; `src/main/plugins/installer.ts::pluginEnvironment` | The same class of failure affects terminal and MCP/Codex child execution. A local isolated terminal reproduced exit 127 with a restricted PATH. | Add one shared PATH helper, preserving existing precedence and minimal MCP environment. Recompute standard install locations at launch. Expose both original and child executable resolution in diagnosis. |
| [#223](https://github.com/totec448-spec/chat-on-steroids/issues/223), [#208](https://github.com/totec448-spec/chat-on-steroids/issues/208): reusable Skills and bounded retrieval; `src/main/skills.ts` | Common local Skills and relative resources already exist. The list lacked a way to select a skill by its actual instructions. | Add optional literal `skills_list.query` over names/full bounded SKILL.md before pagination. Return existing compact metadata, then read the chosen resource. No extra YAML parser, memory store or execution framework. |
| [#213](https://github.com/totec448-spec/chat-on-steroids/issues/213): catalog truncated at 64 tools; `src/main/plugins/exposure.ts` | Our direct MCP does not have that count limit. It preserves upstream cursors, with filtering explicitly scoped to each page. Raising an unrelated limit would not fix a demonstrated defect here. | Add a real stdio MCP regression: 118 tools over 80/38 pages, empty filtered first page, opaque Unicode cursor, full-description search, exact schema and execution of the last tool. |
| [#203](https://github.com/totec448-spec/chat-on-steroids/issues/203): keyboard input acknowledged without TextEdit's expected effect | Our beta GUI is experimental; exact window/snapshot targeting, consumed observations and `postcondition_verified=false` already distinguish dispatch from effect. | Retain and rerun those contract tests. Do not relabel GUI as supported, infer successful saving from key dispatch, or copy CoS's native input implementation. |
| [#168](https://github.com/totec448-spec/chat-on-steroids/issues/168), [#113](https://github.com/totec448-spec/chat-on-steroids/issues/113): delivery ACK loss / interrupted client response | We already retain operation IDs and outcomes, reject changed arguments and classify uncertain effects without replay. A new ChatGPT message's ACK is a separate browser concern. | Rerun real effect-then-disconnect, same-operation recovery and terminal-input tests. Preserve uncertainty instead of automatically retrying a mutation. |
| [#23](https://github.com/totec448-spec/chat-on-steroids/issues/23), [#210](https://github.com/totec448-spec/chat-on-steroids/issues/210): distinguish health evidence and supervisor lifetime | Our optional Cloudflare adapter reports public reachability as unverified after credential handoff. Its single synchronous owner supervises process exit; it does not parse CoS tunnel polling logs. | Retain conservative status and test bounded restart/cleanup. CoS's provider-specific metrics parser is not transplanted. |
| [#279](https://github.com/totec448-spec/chat-on-steroids/issues/279), [#199](https://github.com/totec448-spec/chat-on-steroids/issues/199), [#197](https://github.com/totec448-spec/chat-on-steroids/issues/197): missing tools / client refusal despite a connection | Engine readiness, client tool attachment, scoped authorization and actual dispatch are different layers. A plugin cannot force the host model to call a tool. | Document diagnosis and result recovery below. Do not forge tool annotations, add a refusal bypass, or retry unknown mutations. |
| [#258](https://github.com/totec448-spec/chat-on-steroids/issues/258), [#233](https://github.com/totec448-spec/chat-on-steroids/issues/233), [#154](https://github.com/totec448-spec/chat-on-steroids/issues/154): signing and OS trust across updates | Relevant distribution limitation, but signing identities/notarization and Windows publisher trust are not supplied by code or GitHub provenance. | Keep unsigned/permission limits explicit. Do not reset TCC, disable Smart App Control, or claim checksums establish trusted publisher identity. |
| [#178](https://github.com/totec448-spec/chat-on-steroids/issues/178): nested plugin virtualenv exceeds Windows path limits | We do not provision CoS-style nested UUID/plugin virtualenvs. Existing portable relocation checks exercise Unicode paths, not arbitrary path lengths. | No registry change or duplicate installer. Retain short install-path guidance; do not promise unlimited Windows paths. |
| [#282](https://github.com/totec448-spec/chat-on-steroids/issues/282), [#119](https://github.com/totec448-spec/chat-on-steroids/issues/119): extra command/conversation approval policy | Product owner explicitly chose host-client approval rather than a second per-action UI. Existing connection authentication and remote grants still apply. | Do not implement a duplicate approval layer. |

Other proposals (conversation ownership/compaction, model slider discovery,
multi-account worker pools, agent graphs, automatic Core attachment, Firefox
companion, project titlebars and app-specific translations) are outside this
change. Existing file APIs already provide workspace operations; a second file
manager is not necessary to solve the identified failures. Broad Windows native
GUI parity is still roadmap work, not an incidental fix for the PATH issue.

## Diagnose by layer

1. Run `anywhere doctor` on the affected PC. Compare the two executable-resolution
   maps; `ready` establishes authenticated engine response, not public reachability.
2. Confirm the chosen `device_id`, runtime version and `mcp_session_status`.
   A stored device registration is not evidence of current connectivity.
3. If the client lacks tools, refresh its connector catalog/reconnect and verify
   the existing grants; opening a new conversation alone may retain an old catalog.
4. For direct MCP catalogs, use `summary`/`query`, follow `nextCursor` even when
   filtered `tools` is empty, then obtain the exact schema before calling.
5. After interruption, recover the original `operation_id` through `operations_get`.
   Reuse that ID only with exactly the original arguments. If the outcome is
   unknown, inspect the target's state before deciding on a new action.
6. For GUI input, observe the exact target again and check the intended change.
   A dispatch receipt cannot establish that a shortcut saved a file.

## Verification boundary

The added tests cover actual child processes, an MCP stdio server and the registered
Skills/remote-grant path. Windows-specific PATH casing is checked separately from
native Windows execution. CI supplies the latter. They do not establish a new
ChatGPT conversation, live Windows VM availability or a repaired native GUI.
The beta 1 assets remain immutable. This is a post-beta-1 change, not a retrospective
claim that beta 1 contained Skills search or augmented child PATH.

## Follow-up source and issue review, 2026-09-19

The additional source review used upstream main
[`f38852167314a5b75a7e17cd97aaf5c2c13a8889`](https://github.com/totec448-spec/chat-on-steroids/tree/f38852167314a5b75a7e17cd97aaf5c2c13a8889).
This is a pinned observation, not a claim that upstream will remain at that revision.
No upstream implementation was copied. Issue claims below are distinguished from
source behavior and from locally reproduced Anywhere defects.

| Evidence | Applicability and decision |
| --- | --- |
| [#305](https://github.com/totec448-spec/chat-on-steroids/issues/305) asks expiry diagnostics to distinguish a command never claimed by the browser from one claimed without a result. | Preserve the existing Anywhere distinction between pre-dispatch rejection and unknown effects. Direct MCP response-loss tests already verify an effect occurs once and is not replayed. A future subchat adapter must separately record receipt, submission and result evidence; a timeout must not cause another message. There is no current Anywhere browser wake queue to patch. |
| [#306](https://github.com/totec448-spec/chat-on-steroids/issues/306) reports Windows foreground activation requiring additional temporary thread-input attachments. | Current `gui_mcp.py` delegates exact-window operations to an explicitly selected Peekaboo MCP provider; it does not own the reported Windows native focus routine. Do not transplant a platform-specific workaround without an Anywhere reproduction. Retain background-window activation, target ownership and input-release checks as native-provider acceptance requirements. |
| [#307](https://github.com/totec448-spec/chat-on-steroids/issues/307) reports macOS off-Space discovery and different pointer/keyboard focus proofs. | Treat discoverability separately from permission to act on current pixels or keyboard focus. This belongs in native GUI acceptance, including wrong-window rejection after focus changes. The existing adapter's snapshot binding is not proof that these OS-specific cases pass. |
| `src/main/computer/windows-api.ts` at the pinned revision bounds observation filters, fences superseded observations, rechecks app identity and clears observation state before mutation. | Anywhere already serializes its adapter interactions and consumes observations before input, including across provider sessions. Preserve those invariants. Native PID/window verification and separate capture/accessibility failures remain provider work; adding unused fields to the existing adapter would not implement them. |
| `src/main/bridge.ts` at the pinned revision reconciles final responses with the current request rather than accepting transcript presentation order. | The experimental subchat collector already requires matching conversation and submitted user-message evidence, rejects duplicate matching prompts and does not replay submission. Its ordinary-Chat live read evidence is recorded in `SUBCHAT-PROBE.md`; external transport and general tool-bearing replies remain incomplete. Do not advertise production subchat support. |

The practical code-writing workflow is now represented by a real HTTP/MCP test:
source and CSV creation, process execution, stdout and result-file verification,
fresh-client result recovery, conditional source editing and repeat execution.
An execution audit verifies that duplicate start requests do not launch extra
processes. See [the acceptance record](PRACTICAL-ACCEPTANCE-2026-09-19.md) for the
boundary between static-authentication machine tests, live plugin execution and
Codex-owned browser playback. This evidence does not complete the broader 0.2
native-control roadmap.

## Latest receipt review

Upstream was rechecked at `c5ab88714d3bdfa7acc5861aa1c3903e0263eaa6`.
[PR #312](https://github.com/totec448-spec/chat-on-steroids/pull/312) separates
local execution drain from native result receipts before automatic compaction;
its `extension/content.js` and `extension/fiber.js` changes retain request identity
through missing rows and Code Mode batches. Anywhere has no automatic compaction
controller to transplant this into. Its experimental subchat reader did, however,
accept an old answer ID repeated under a new submission. A synthetic snapshot
reproduced that acceptance. The reader now rejects duplicate message identities
across the returned snapshot. The regression fails before the fix and passes
after it; the existing duplicate-prompt fixture now gives genuinely distinct
answers distinct IDs so it continues testing prompt ambiguity separately.
This protects receipt matching, not the still-unavailable external transport.

The other new upstream change, PR #313, changes a pinned GVDB source mirror and
release metadata. Anywhere does not distribute that native dependency; no mirror
or dependency change is adopted.

## HTTP 503 outcome correction

Reviewing response-loss semantics exposed an Anywhere-specific defect: a bare HTTP
503 was classified as proof of pre-dispatch rejection. A real HTTP file-write
fixture followed by a substituted 503 response reproduced `failed` even though
the write had occurred. The client now classifies this unconfirmed server response
as `unknown`, retaining the operation ID for lookup without automatic replay.
The regression verifies one write dispatch and successful ledger lookup after
normal responses resume. This does not claim to reproduce CoS's browser queue.

## Long-session delivery review

[CoS #301](https://github.com/totec448-spec/chat-on-steroids/issues/301) reports
large observation batches exceeding the companion's request deadline and entering
a repeated delivery loop. Its proposed timeout increases are reporter suggestions,
not a reproduced Anywhere fix. Anywhere has no equivalent browser journal yet.
Its HTTP backend retains the operation ID and returns unknown after an unconfirmed
response; cancellation of the local observer does not release the transport lock
while the request is still running. Four targeted HTTP tests passed on recheck: a
real delayed file mutation followed by observer cancellation and result lookup,
and three effect-then-invalid-response cases asserting one tool dispatch.
These do not establish large browser-history throughput or a working companion.

No blanket timeout increase or automatic mutation replay is adopted. The subchat
probe's overall deadline now returns a structured timeout rather than a traceback,
while the submitted Chat remains untouched. Long-running generation must be
observed again using the same submission identity.


## Browser recovery follow-up, 2026-09-20 JST

Upstream main remains `c5ab88714d3bdfa7acc5861aa1c3903e0263eaa6` at this check.
Two new unmerged proposals were reviewed; their reported test results are not
Anywhere test evidence and their code was not copied.

- [PR #314](https://github.com/totec448-spec/chat-on-steroids/pull/314), head
  `26053070de0b51509d55ecce51297c851cddbf59`, asks for the exact conversation's
  page when a wake is pending, and separates live agent ownership from dormant
  history. Applicable requirement: loss of a tab must not imply loss of the
  conversation or authorize another submission. There is no production Anywhere
  wake queue or browser lifecycle manager to patch; preserve this distinction
  when implementing the subchat adapter. Do not add a parallel agent registry.
- [PR #315](https://github.com/totec448-spec/chat-on-steroids/pull/315), head
  `5ed455c50be95a2c181291f43450cdae2b4c61a5`, adds bounded once-per-identity
  diagnostics for replacement-page receipts. Anywhere has no equivalent
  continuation marker endpoint. Do not introduce that endpoint just to adopt the
  diagnostic. Existing operation receipts remain the evidence for result recovery.

Issue #311 remains unresolved at this observation. The newest comments describe
ongoing investigation, not a proven cause or compatible model-discovery API.
Account-specific visible menu observation must not be advertised as a stable
provider API or a working production subchat catalog.

The later September 20 recheck still found main at `8f76ccc790917b01ee758da6687a1cf9b576ba8a`.
Issue [311's retest](https://github.com/totec448-spec/chat-on-steroids/issues/311#issuecomment-5745989607)
reports caller-identity errors after the discovery changes; another reporter
observed success after resetting configuration. These reports do not establish a
single root cause. Anywhere's selected response is dynamic authenticated HTTP
catalog observation (PR 93), with model generation and effort preserved as
separate fields, Work excluded, a bounded observation window and no silent model
fallback. This supplements the UI adapter rather than claiming a stable official
Chat API. Live catalog and generation-request evidence is in
`SUBCHAT-SHARED-WORK-ACCEPTANCE-2026-09-20.md`.

Issue [279's newer report](https://github.com/totec448-spec/chat-on-steroids/issues/279#issuecomment-5748625410)
describes healthy page delivery but no recorded Core calls. The subsequent
Plus/Pro comparison is an account anecdote, not a demonstrated plan restriction.
Keep the existing distinction between client tool discovery and server dispatch;
do not recommend an account upgrade, restart a healthy engine, or replay an
uncertain operation on that evidence alone. No additional orchestration layer or
authentication workaround is adopted from these reports.

## Consolidated recovery proposal reviewed, 2026-09-20 JST

[CoS PR #316](https://github.com/totec448-spec/chat-on-steroids/pull/316), inspected
at `647f23ec1fc09da246c9e8953fe2a00f18e546b4`, consolidates the earlier recovery
proposals. Its `askForTheTabToWakeIn` rechecks lifecycle, cancellation, current
ownership and delivery evidence after an asynchronous session read. A pending
wake requests the exact conversation's tab; dormant history does not establish
current worker ownership. The journal path gets its own 60-second budget,
including 413 split retries, rather than changing all request deadlines.

Applicable subchat requirements are to revalidate ownership after awaited work,
recover the same conversation without resubmitting, and preserve unacknowledged
results. Anywhere does not currently have that browser wake/journal queue, so no
queue, global timeout increase or companion-specific endpoint is added. Existing
operation-ledger recovery remains the execution service's mechanism. This is a
source review of an unmerged proposal at the stated revision; upstream test claims
are not Anywhere acceptance evidence. No upstream code was copied.

## Alternate-shell implementation review, 2026-09-20 JST

The next pinned main is
[`8f76ccc790917b01ee758da6687a1cf9b576ba8a`](https://github.com/totec448-spec/chat-on-steroids/tree/8f76ccc790917b01ee758da6687a1cf9b576ba8a).
This supersedes the earlier observations of PRs 316, 319, 321 and 322 as pending.
Reviewed `extension/chatgpt-dom.js`, the follow-up input changes and the two
alternate-shell worklogs. Upstream synthetic browser/CI counts are not our live
acceptance evidence; issue 311's affected-account retest is a separate boundary.

| Implementation evidence | Adoption decision |
| --- | --- |
| A native literal-paste mark preserves Markdown punctuation and hard breaks in the alternate editor. | Adopt the observed editor contract in a separately implemented, guarded draft helper. Anywhere live acceptance independently verified the saved message text; ordinary fill and line-by-line input reproduced formatting changes. |
| Shell layout turn keys and stamped message identity are distinguished. | Do not generalize a layout key into a documented provider message ID. Our specific live user IDs agree with owning-app reads; production recovery still needs explicit conversation/submission identity and changed-markup failure handling. |
| Follow-up running hints use the latest exchange; old exchanges may remain in progress. | Do not stop a new generation or declare completion from an old row. Keep completion tied to the selected submission, not global transcript ordering or text stability. |
| Cold model-picker hydration and bounded request/socket identity are handled separately. | Preserve bounded readiness and dynamic visible model discovery. Do not implement a broad cache/history walker or infer a model from the effort-only composer label. |
| Recovery ownership is checked again after asynchronous work. | The copy-text receipt rechecks target identity and transcript membership after its awaited action. Duplicate original prompts must remain ambiguous unless the caller supplies prior submission identities. |

The message Copy action can preserve details lost by visible Markdown, but is
not an original-source oracle: live observations also show escaped dots and
bare URLs expanded into Markdown links. Anywhere captures that action in the
dedicated page, restores the original clipboard method, compares it to the exact
submitted prompt, and never treats a mismatch as permission to resend. This is not a
stable ChatGPT API or evidence that Codex's external socket has been repaired.

## September 20 follow-up decisions

Upstream main was rechecked at `8f76ccc790917b01ee758da6687a1cf9b576ba8a`.
The following are issue reports, not reproduced Anywhere defects:

| Evidence | Decision and boundary |
| --- | --- |
| [#329](https://github.com/totec448-spec/chat-on-steroids/issues/329): read tools remain available while write/exec access disappears | Adopt capability-specific acceptance rather than equating `ready` with all tools working. The two ordinary Chat reviews independently used file reads and terminal execution and returned the installed runtime identity. This proves those paths at that time, not future connector availability. Do not silently widen permissions, substitute another connector or treat read access as write/exec proof. Automatic reattachment remains unimplemented and requires evidence from the actual connector. |
| [#330](https://github.com/totec448-spec/chat-on-steroids/issues/330): compaction leaves active work requiring manual continuation | Preserve operation/submission IDs and distinguish live work from observation timeout. Reuse saved subchat records and explicit recovery; do not replay an uncertain send. Anywhere does not own the ChatGPT model's context compaction, so transparent model continuation is not claimed. No separate compaction engine is added in this change. |

The copy-evidence subchat's artifact was read and independently rerun alongside
the repository's Copy/submission tests: 15 tests passed. Its collision example
is a controlled serializer model, not a measurement of ChatGPT's internal
storage. A matcher that permits selected serialization rewrites is therefore not
adopted as proof of exact source or as permission to accept historical unknown
sends. The already-discovered mismatch remains an open recovery limitation;
silently broadening equality would hide it rather than resolve it.

## Subchat integration constraint

`Engine.execute` already claims an operation before invoking its handler, preserves
inflight work when the five-second observer expires, and returns saved outcomes
for repeated IDs. Its ledger marks unfinished work unknown after engine restart.
Reuse this behavior for submission; do not add a second retry scheduler. A
subchat-specific durable record now stores the transport owner, requested/observed
conversation, selected model/effort, submission identity and state before send.
The general ledger stores an argument digest and final result, not intermediate
browser recovery data. Caller-supplied work context is descriptive, not a grant.
An observer timeout is not a failed generation, and the generic handler exception
path must not classify an uncertain send as a confirmed failure.

## Reusing CoS workers as an optional backend

At the same pinned source, `registerAgentsTool` exposes `spawn`, `message`,
`status` and `finish`. `callerNow` resolves the caller through a conversation ID,
request correlation, or (when configured) an unattributed request. `ownsPrime`
uses the attached conversation; before attachment the allowed request identity
must still equal the original owner request. Supplying `run_id` does not grant
ownership. Do not treat MCP connectivity or reusing an arbitrary request string
as proof of an authorized persistent external controller.

[Issue 82](https://github.com/totec448-spec/chat-on-steroids/issues/82), still open
at this check, proposes that separate external-controller boundary. Existing CoS
workers are a credible optional backend, but the full external sequence remains
unverified: spawn, message, result, disconnect, reconnect, message the same worker.
No CoS installation was found in the Mac's standard application directories at
this check; no live worker call or installed-backend success is claimed.

The published release inspected is v2.1.14. Its documented setup requires the
desktop app, workspace configuration, Core connection and companion extension.
This is materially more than attaching an arbitrary stateless MCP server. Keep
CoS optional so using Anywhere's file/terminal/device tools does not require it.

## Browser-free controls versus ordinary Chat transport

Rechecked upstream main on September 20: it remains
`8f76ccc790917b01ee758da6687a1cf9b576ba8a`. In that exact source,
`extension/content.js`'s `acceptDesktopInput` waits for and claims a composer;
its direct-turn branch calls `CLF_DOM.stopGeneration` before sending. This is
browser-companion delivery, not a demonstrated browser-free Chat backend API.
Do not transplant Stop+Send as immediate steer. This source inspection does not
prove that no other provider transport is possible.

Anywhere's saved-list and CLI queue commands reuse its existing ledger without
opening Chrome. Queue inherits the confirmed target model, effort and context;
recovery drives delivery through the existing adapter. These controls do not
establish browser-free Chat creation or answer retrieval. The owning Codex app
has separately demonstrated existing-Chat send/read, but the available create
tool exposes Work rather than ordinary Chat creation. A complete replacement
transport remains unverified; do not substitute Work or paid API inference.

[Issue 327](https://github.com/totec448-spec/chat-on-steroids/issues/327) reports
stale session waiting; its suspected causes are not demonstrated Anywhere bugs.
Anywhere records the exact predecessor operation for queued input and rejects
changed targets under the same identity. Queue registration is local acceptance,
not an active background dispatcher. `recover` may remain pending while the
predecessor has no verified final answer; neither elapsed time nor an apparently
idle page authorizes resend or completion. Better explanations of unresolved
receipt/answer states remain useful follow-up work, separate from inventing a
terminal result for a long-thinking model.

## September 21 native control and account scope review

GitHub main was rechecked at `04c6a298078817cf25c197f2b8bb639f8239243c`
(commit timestamp September 20, 12:03 UTC). The following issue bodies were read;
upstream-reported test counts are not Anywhere acceptance evidence.

| Evidence | Anywhere decision |
| --- | --- |
| [#307](https://github.com/totec448-spec/chat-on-steroids/issues/307): off-Space discovery and distinct physical-pointer/keyboard focus proof | Keep discovery separate from input authority. The experimental native helper revalidates process/window/element identity and the observed value before direct AX replacement. It neither activates the application nor falls back to global keyboard input. Its window handles are not WindowServer IDs. Off-Space geometry, physical input and focused-element ownership remain separate, unverified work; successful AX replacement does not certify them. See the native implementation and live limits in [GUI reliability](GUI-RELIABILITY-2026-09-14.md). |
| [#338](https://github.com/totec448-spec/chat-on-steroids/issues/338): built-in account switching and automatic continuation on another account at a usage limit | Defer account switching. It requires explicit account-to-conversation, credential and workspace binding; adding an account picker alone would not establish these. Do not silently change accounts, publish conversation share links, or continue on another account when a limit is encountered. Retain the selected dedicated browser profile and report access/availability separately from generation state. This is not an implemented multi-account capability. |

These decisions add no alternate execution scheduler, credential-import path or
upstream code dependency. Browser-independent PC control and ordinary Chat
transport remain separate acceptance tracks.

## September 21 initial browser authorization review

Upstream main remains `04c6a298078817cf25c197f2b8bb639f8239243c`.
[Issue 339](https://github.com/totec448-spec/chat-on-steroids/issues/339)
reports a listening local bridge with no authorized companion connection on
Windows 11. Its root cause is not established by the report, and it does not
prove an Anywhere bridge defect. Keep process/listener availability separate
from authenticated provider access; do not add a competing bridge or loosen
permissions on this evidence alone.

The relevant Anywhere lifecycle inspection found a concrete adjacent defect:
an initial HTTP authentication observation rejected with 401/403 was not latched,
although subsequent HTTP access rejection was. Repeated polling could therefore
create and close another observation page each time. The reader now retains both
initial and subsequent access rejection until explicit reader/context replacement.
Controlled regression cases for both statuses fail against the old reader
(three observations for three polls) and pass with one observation after the fix.
This is not a reproduction or resolution of CoS #339, nor browser-independent
login. The lifecycle contract is documented in [Subchat probe](SUBCHAT-PROBE.md).

## September 21 proposed credential and hydration fixes

[PR #340](https://github.com/totec448-spec/chat-on-steroids/pull/340) was read at
`37c5f41b9ac742f4303df3c8e867f559cd08daec`, still unmerged against upstream
`04c6a298078817cf25c197f2b8bb639f8239243c`. This is proposed upstream behavior;
its reported tests and live results are not Anywhere acceptance evidence.

| Proposed upstream fix | Anywhere decision and evidence |
| --- | --- |
| Serialize shared browser credential provisioning, reuse automatic pairing credentials, reject a provisioning receipt superseded by Disconnect | Do not copy this pairing mechanism into `ChatHTTPReader`. It observes provider credentials in memory and does not mint or rotate a shared companion credential. Its cache is tied to browser-context identity. This inspection does not certify unrelated enrollment paths. |
| Wait for editable source and recorded question before freezing compaction identity | No equivalent Compact & Resume workflow is implemented here. Keep incomplete rendered history separate from authoritative input identity: the outgoing request checkpoint added in PR #117 supports known-conversation HTTP recovery. Unknown new-conversation identity after an ambiguous crash remains unresolved. |
| Reject invalid explicit tunnel executable instead of silently choosing another binary | Already implemented by `cloudflared_executable`: only an omitted executable searches PATH. The existing `test_explicit_connector_never_searches_path` covers missing explicit executables and passed on re-run. No duplicate implementation needed. |
| Restore bundled ripgrep precedence after login-shell profiles | No transfer based on similarity alone. Establish an Anywhere packaged search/runtime defect before changing shell PATH semantics. |

The current subchat MCP session serializes send, recovery and catalog calls with
one browser lock; context creation also has a separate lock. That prevents
concurrent reader bootstrap within that session, not arbitrary direct backend
calls or multiple sessions sharing one backend. Any future HTTP parallelization
must first define authentication-cache ownership, bootstrap lifetime and context
replacement. Removing the outer lock alone would not establish safe concurrency.

## September 21 shell attribution and worker round-trip update

Upstream main advanced to `fbf2944c79c50b105f202a97799087729e7e0a1c`.
[PR #342](https://github.com/totec448-spec/chat-on-steroids/pull/342),
head `0d8c34d26388db767a744864a8992d44f5c95f2b`, was inspected as source
differences, not run as a live CoS deployment.

- `extension/content.js` now refreshes witnessed manual-Send evidence and
  rechecks route/document lifetime after asynchronous owner confirmation. Its
  provisional exchange requires a separate request-owner handshake. This does
  not turn a conversation ID or shared connector grant into authenticated child
  identity. Anywhere's stdio subchat adapter has no equivalent companion/Fiber
  handshake; keep work_context descriptive and shared-grant inbox append disabled.
- `agents.ts:reconcileAgentRequestOwners` fills an existing worker origin's
  missing parent after late prime identification, including sleeping workers,
  while `recorder.ts:applyOrigin` notifies observers. Anywhere does not have this
  provisional worker-family representation. Do not add a second family registry
  merely to copy the repair; operation/input/conversation identities remain its
  existing source of receipt and answer correlation.
- Writable-composer readiness after model selection is distinct from a rejected
  prompt. Retain this as a browser-adapter acceptance concern; the upstream diff
  alone does not demonstrate an Anywhere failure or browser-free sending.

[Issue #341](https://github.com/totec448-spec/chat-on-steroids/issues/341) proposes
filing a compaction ticket when a work episode ends, after an earlier busy-turn
refusal. Anywhere has no automatic Compact & Resume workflow to patch. Defer
that scheduler rather than introduce automatic reloads, stop/send, or generated
handoffs into result recovery. Preserve pending/unknown and retrieve an existing
result without replay. The reporter's measurements and tests are upstream
claims, not independently reproduced Anywhere evidence.

## Integrated pairing/Skills/Stop review, 2026-09-21

Reviewed upstream main `7777e517603289696bb5febddb9bbf51cdde9aea`, including
[PR #344](https://github.com/totec448-spec/chat-on-steroids/pull/344)
(head `1eed5abbffb375163ada6200b711df312450037e`), against the earlier
`fbf2944c79c50b105f202a97799087729e7e0a1c` baseline. This is source review;
upstream test and installed-runtime claims are not our reproduced evidence.

- `session/input.ts:authorizeBrowserInput` and `failBrowserInput` distinguish a
  proven pre-Send withdrawal from a generic failure after send authorization.
  Anywhere already commits `sending` before backend dispatch and never resets
  it on a send exception. Receipt/answer recovery is distinct from replay.
  Retain this contract instead of adding a second delivery-state registry.
- `skill-links.ts:approvedManagedSkillLink` requires a canonical target within
  currently approved ordinary roots, rechecks link identity and approval, and
  does not use the managed Skills directory as an escape permission. Anywhere
  has no equivalent approved-root registry: its explicit collection roots and
  per-skill resource containment have a different contract. Internal resource
  links already work; external collection escapes stay rejected. Users can
  explicitly select the actual containing collection. Do not import implicit
  root approval or advertise collection containment as an OS sandbox.
- `extension/content.js` separates a Stop request from actual provider
  cancellation and preserves later exact request-owned work. Anywhere's HTTP
  history uses correlated provider cancellation metadata, not a local click or
  idle page. Its local cancel only cancels unsent work; a provider-stop command
  remains unimplemented. No automatic stop/send or continuation loop is adopted.
- Pairing serialization and transactional browser-bridge port replacement apply
  to CoS's companion bridge. They do not demonstrate a defect in Anywhere's
  standalone read client and do not remove Chat's generation preparation.

Local verification: `tests/test_common_skills.py` and
`tests/test_subchat_lifecycle.py` passed all 28 tests. These cover contained and
escaping links, resource replacement, uncertain-send persistence, restart,
Thinking, wrong answer identity and pre-dispatch error handling. They do not
prove live CoS pairing, native steer or browser-independent Chat generation.

## Proposed frontend/Skills feature pack review, 2026-09-21

Upstream main remained `7777e517603289696bb5febddb9bbf51cdde9aea`.
The unmerged [PR #345](https://github.com/totec448-spec/chat-on-steroids/pull/345)
was inspected at `3b26fc112d52b349c8ef0f9b737603849deabdc5`, specifically
`src/renderer/chat.ts`, `src/main/skills.ts` and `src/main/skill-github.ts`.
This was source review, not execution of its UI or upstream acceptance suite.

- `finishAssistantPresentation` only completes local text reveal after the
  controlled backend turn is absent; it does not request provider Stop. Adopt
  the same separation of presentation and execution evidence. Anywhere's HTTP
  history projection already uses correlated provider completion/interruption
  metadata, not visible text animation. No presentation timer or automatic Stop
  is added to recovery.
- `updateSkillPackage` checks GitHub origin/revision, the installed SKILL.md
  digest and directory identity before staging and swapping a package. Useful
  for a future managed importer, but Anywhere currently reads explicitly chosen
  local collections and does not own their remote update lifecycle. Do not add
  an installer or overwrite user Skills as an incidental subchat change. The
  inspected SKILL.md digest check alone is not evidence that every attached
  resource's local edits are protected.
- Desktop pets, composer animation, sidebar geometry and renderer terminal-fit
  coalescing do not apply to the quiet HTTP execution client. These are not
  imported. The PR explicitly excludes its separate internal Chromium host;
  it does not demonstrate browser-independent ordinary Chat authentication or
  generation.
