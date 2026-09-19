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
