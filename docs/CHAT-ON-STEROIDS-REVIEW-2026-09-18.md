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
subchat-specific durable record still needs the peer, conversation, selected model,
submission identity and state **before** send, because the general ledger stores
an argument digest and final result, not intermediate browser recovery data.
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

## Recovery issue recheck, 2026-09-20

GitHub still reports main `8f76ccc790917b01ee758da6687a1cf9b576ba8a`.
[Issue 328](https://github.com/totec448-spec/chat-on-steroids/issues/328) reports
mid-task `401 tunnel_use_forbidden` after initially successful access, while
[issue 323](https://github.com/totec448-spec/chat-on-steroids/issues/323) reports
loss of an existing workspace association. These are upstream field reports,
not reproduced Anywhere defects; the suspected identity/lifecycle causes are
not established by the reports.

Adopt their acceptance concerns: preserve the selected device/workspace across
recovery, distinguish authorization failure from transport failure, and recover
results using the original authorized operation identity. Anywhere's
`AuthorizedDeviceMCP.current()` already revalidates grants at dispatch, and the
HTTP authorization tests exercise revoked grants/devices. Do not automatically
re-enroll, widen permissions, change devices, or replay writes on every 401.
Refresh of legitimately expired credentials must remain distinct from revocation.
Persistent product subchat work-context binding is still incomplete, as tracked
in the goal review; carrying a workspace string is not authenticated binding.

The all-tools Chat-like HTTP acceptance suite caught a missing invocation of the
new `audio_status` tool. It now invokes the real tool through authenticated HTTP
and checks that no capture started. This closes test coverage, not native capture
acceptance, and is not evidence of browser-free subchat creation.
