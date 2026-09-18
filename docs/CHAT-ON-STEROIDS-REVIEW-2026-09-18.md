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
