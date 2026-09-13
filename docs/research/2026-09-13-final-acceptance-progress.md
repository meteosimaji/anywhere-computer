# Completion acceptance progress

This record separates completed acceptance checks from untested support claims.
It is not a stable-release qualification.
Windows-for-Mac product development is outside this completion pass.

## Current baseline

At main `b586b49`, the local suite completed with 925 passed and 17 skipped in
93.52 seconds. Ruff passed; mypy passed for 80 source files. A focused rerun
with skip reasons classified all 17: 14 Windows-only native checks, two
Linux-only checks, and one registration test restricted to disposable GitHub
runners. That rerun completed with 69 passed and 17 skipped.
GitHub Quality run 34744562722 completed on
macOS, Windows and Ubuntu. These checks do not establish GUI or reboot behavior.
Quality run 34745974391 at `5d0f6f8` subsequently completed successfully on all
three platforms, including portable build and provenance verification.

## New ChatGPT trial

Conversation: https://chatgpt.com/c/6aa65186-fb04-83ee-a5d5-b4a8ad8a88ea

GPT-5.6 Sol was selected explicitly and the composer showed medium effort before
submission. The trial requested file creation, conditional append, search,
terminal output, operation recovery and cleanup on both registered computers.
The model reported a platform safety rejection of a routed Mac directory
creation. The rejection payload and its operation ID were not available in its
final report, so the precise classification remains unverified. It attempted a
direct local path before interruption. That continuation is recorded but is not
used to qualify the rejected operation. The follow-up explicitly stopped changes
and prohibited alternate-path retries. Windows mutations and the remaining
workflow were not completed. This trial is not a pass.

## Shared Mac engine update omission repaired

Live status calls through both the local Plugin and the authenticated HTTP
connector confirmed the Mac instance reported by the trial:
`5c4ffeca895d4796aa6000e97334c440`, runtime
`5c7f629bf0c8da42f216a7b98214cb6f69d4496e09d535b596647aee33ce446b`.
This was an actual old engine, not an unverified model assertion. Process and
runtime-selection inspection showed two state directories: the HTTP service
directory had already selected b33bab0, while the shared base directory still
selected f111c76. Updating the HTTP service alone had not updated the engine
actually used for operations.

With zero active sessions and operations, the b33bab0 interpreter's existing
`start` command updated the shared base directory. Both actual tool entry points
then returned instance `a7152a1d57e744a096434f0ba5b28405` and runtime
`42f10d9ce185a8ad96cd9f90d2e5b4aeaa472ac94a4b4ce09154b47c85f03314`.
Local verification operation: `4623523451924753b96ec76615e2b171`.
HTTP verification operation: `8b62e04d7fe948b8a2d80bf9ab6a8736`.
Both reported ready and zero active work. The VM was not restarted.

Future update acceptance must inspect the operation entry points themselves;
service process identity alone is insufficient when the shared engine directory
differs from the HTTP configuration directory.

## Real Codex protocol checks

`verify_codex_plugins.py` passed catalog discovery, fixture tool execution and
child-process cleanup. `verify_plugin_sessions.py`, using the installed Codex
binary, passed authenticated HTTP session continuity, token refresh, native PNG
relay, dispatch-once replay, stale-catalog rejection, grant isolation/revocation
and cleanup. The recorded methods contained no model-turn start or steer calls.
These tests use isolated fixture MCP servers and credentials; they prove the
bridge protocol and lifecycle, not every installed third-party Plugin.

## Updated HTTP Skill and live GUI checks

The installed authenticated HTTP connector resolved the Cloudflare Skill body
and its `references/workers/README.md` through the returned `skill_directory`.
Operations `aff379ce52a3442ca3ec702407410afa` and
`35df6fb9a6c8441993c3457d26b87a80` both completed. Independent local SHA-256
checks matched the returned body hash
`89bcccbb0b6dc77bec5e348f54a5ed8f1943d6ce56c33231a71fd2cd6bb05714`
and reference hash
`4e00df1c6d64cd393c909430769c183b7027b4fe1db0c2dbb31090987baff14c`.
This read reference material only; no Cloudflare deployment was performed.

`verify_gui_http.py --typed`, with the installed `/opt/homebrew/bin/peekaboo`,
completed Calculator input and observation of 42, 50, 51, 52, 53 and 54 in one
session. Every observation was recovered from the operation ledger, the session
remained open through the sequence, and explicit closure confirmed cleanup.
This is a real macOS GUI test through an isolated authenticated loopback HTTP
server and the typed GUI API, with no model inference. It does not establish
Windows GUI support or replace the incomplete new ChatGPT acceptance trial.

## Separate new ChatGPT read/search/session acceptance

Conversation: https://chatgpt.com/c/6aa654a6-440c-83e8-8ae0-9ed0fb252806

The UI explicitly showed GPT-5.6 Sol and medium effort before submission. This
independent trial requested device identification, reading and searching the
existing Windows acceptance directory, Cloudflare Skill/reference reads, and a
temporary node_repl session. It did not retry the earlier rejected directory
creation. The final Chat response reported these requested items successful,
including one Windows search hit with one visited file and no directory errors.
It also disclosed an initial reused request ID conflict; this is a caller error,
not evidence that the ID-binding safeguard failed.

The Mac server ledger independently confirmed session
`f96a466c32204858bf75ab288f415a2b` and these internal operation IDs:

| Check | Internal ledger operation | Observed result |
| --- | --- | --- |
| First node_repl call | `3dffe4a3e6c56ab257c4259c10e8eace` | 40, completed, is_error=false |
| Second call, same session | `917edfbe85627fc67b6908815faaafb1` | 42, completed, is_error=false |
| Recovery | `db5e6a5351d62bcedc86e52e799868eb` | Original second call and 42 |
| Close | `e62439a4333542f099b3b173014c4e85` | cleanup_confirmed=true |

These IDs are internal, grant-namespaced IDs, distinct from the external IDs in
the Chat response. Skill and reference reads were also completed in the same
trial interval. Both computers reported the expected runtime fingerprint and
their distinct current instance IDs. An independent device continuity probe
also read the unchanged Windows acceptance file and recovered its earlier
terminal operation successfully (three calls, approximately 2.86 seconds).
This is continuity without a VM restart; cold-login recovery remains untested.

Read-only inspection of the Windows ledger independently matched that trial's
search ID `733462a3210f4e87a1543d3b93b4201a`: search start
`eeb016e1fddca0c49220c2a9125840e8` and result
`06b11189444a52675865ea0d9c7e6dca`. The completed result contained one matching
file at line 1, visited_files=1, directory_errors=0 and truncated=false. File
read `0848b77065efa42be6477acdcb6406ea` matched the expected three-line content
and SHA-256. No Windows settings or files were changed by this inspection.

The installed Mac Plugin cache's wheel and pinned dependencies file matched
the repository's bundled checksums, with manifest version `0.1.0-alpha.9`.
The package/source consistency regression test passed as well.

The Chat identified catalog verbosity as a usability limitation: devices_tools
returns all authorized schemas, even when only one tool is needed. The exact
schema and Plugin fingerprint sequence also requires several calls. These are
recorded usability findings, not silently treated as functional test failures
or as proof of a complete whole-product qualification.

## Requirement reconciliation

| Requirement | Evidence and boundary |
| --- | --- |
| Four alpha7 audit defects | Current spawn failure handling, atomic terminal admission, post-write unknown outcome handling, and persistent group/Job ownership are implemented. `test_audit_regressions_20260913.py` covers the first three; `test_terminal_children.py` exercises real surviving children, update blocking, stop and reused-group protection. They are included in the passing full suite and OS CI. |
| Mac/Windows files, search, terminal | Prior Windows write/append/terminal receipts are recorded in `2026-09-13-windows-owner-pipe-acceptance.md`; this pass verifies current Windows reads/search and durable output recovery. Mac workflows are covered by the existing real acceptance records and full workflow tests. |
| MCP/Plugin and Skills | Real Codex protocol checks plus the new Chat's installed node_repl state continuity and actual Skill/reference reads, reconciled above. This does not promise every third-party Plugin's own service works. |
| Same Chat, two computers | The new Chat obtained both distinct instance IDs and the same installed runtime, then operated on the explicitly selected Windows file and Mac Plugin session. |
| Connection continuity | Authenticated HTTP reconnect and token refresh preserve sessions in the protocol test; saved Windows file and operation remain recoverable through subsequent fresh connections. VM power-cycle and cold-login acceptance were not performed. Running processes are not restored after an OS/agent crash. |
| Computer Use | Real macOS Calculator observation/input/re-observation and ledger recovery passed through the typed GUI adapter. Windows native GUI and the Codex-owned execution context are not qualified; neither is falsely advertised as available. |
| Installed updates | Actual Mac local/HTTP operation entry points share the updated engine identity; installed Mac Plugin artifacts match source bundle hashes. Windows installed launcher/Plugin and its actual current runtime were checked in the Windows acceptance record and this Chat. |
| Tests and publication | Full local suite, lint/types and three-OS Quality CI passed. Source/Plugin consistency passed. Changes are on public GitHub main; the distributed public alpha6 release remains distinct from the tested alpha9 development installation. |
| Operational policy | Updates remain manual by default, automatic stable updates opt-in. No new per-operation approval UI was added. Platform decisions remain outside the Plugin's control. VM and user data were preserved. |

Windows-for-Mac display, input, clipboard, HVCI and VRChat work remains outside
this delivery. Future cold-login/VM reboot verification requires a coordinated
window and is not represented by the successful live-connection checks.
