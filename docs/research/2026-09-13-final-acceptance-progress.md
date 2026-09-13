# Completion acceptance progress

This record is incomplete acceptance evidence, not a release qualification.
Windows-for-Mac product development is outside this completion pass.

## Current baseline

At main `b586b49`, the local suite completed with 925 passed and 17 skipped in
93.52 seconds. Ruff passed; mypy passed for 80 source files. A focused rerun
with skip reasons classified all 17: 14 Windows-only native checks, two
Linux-only checks, and one registration test restricted to disposable GitHub
runners. That rerun completed with 69 passed and 17 skipped.
GitHub Quality run 34744562722 completed on
macOS, Windows and Ubuntu. These checks do not establish GUI or reboot behavior.

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
