# Product roadmap

Owner direction, 2026-09-18. These milestones express intended outcomes, not
current capability claims. Published beta 1 remains available unchanged.
Development begins at 0.2.0a1. Stable automatic updates remain opt-in; development
versions are not promoted into the stable channel.

Keep the authenticated execution engine, multiple-PC router, operation ledger,
response-loss recovery, HTTP/stdio MCP and optional Codex adapters. Do not build
a duplicate ChatGPT browser orchestrator to reproduce a competitor's UI.
The connected AI chooses work; execution does not silently start a model turn.

| Milestone | Theme | Acceptance scope |
| --- | --- | --- |
| 0.2 | Native Computer Control | Coexisting pipe and PTY/ConPTY terminals; native macOS/Windows GUI providers with optional Peekaboo; isolated and explicitly selected existing browser sessions. |
| 0.3 | Zero-config Remote | Packaged installation, resident process, device pairing and AI connection through shared controller functions. CLI remains usable; management GUI is optional. External account/OS steps are represented honestly. |
| 0.4 | Durable Agents | Persistent tasks/workers and dependency graphs with status, cancellation, messages and results independent of browser DOM identity. |
| 0.5 | Multiple computers and agents | Authenticated routing, task ownership and recovery across PCs and agent providers. |
| 1.0 | Daily use | Signed platform releases, qualified update/recovery, auditability and reproducible performance/acceptance evidence. |

## First implementation sequence

1. Terminal backend contract plus POSIX PTY and Windows ConPTY. Retain existing
   pipe tools and compatibility. Support input, incremental output, resize, signals,
   closure and operation recovery. Verify real REPL interaction, UTF-8 boundaries,
   Ctrl-C, EOF, process-tree cleanup and disconnect/reconnect. Do not call Windows
   qualified based on a POSIX test or mocked Windows API.
2. Native CUA provider contract and platform adapters. Recover and evaluate the
   proposed tests in [issue #37](https://github.com/meteosimaji/anywhere-computer/issues/37)
   rather than assuming they all describe the final architecture. Bind observation,
   window, element and process identity to the device and authenticated owner.
   Reject expired, foreign or consumed references. Keep observation/input separate
   from screen capture and from independently verified postconditions.
3. Browser provider with isolated sessions first and explicit existing-session
   selection. Verify navigation, continuity, credentials isolation, stale targets
   and cleanup. Never expose a broad unauthenticated debugging endpoint.

Small reliability fixes from the comparison can merge first. They do not satisfy
the native-control milestone by themselves. Do not implement Workers ahead of
these execution and connection foundations.

## Identity and recovery

The authenticated principal and selected device are authoritative. An MCP session
ID can change at reconnect; it is not the sole durable owner identifier. Future
task/worker IDs must be bound to stored ownership and authorization. Browser DOM
guesses do not establish task ownership.

Keep distinct: recorded history, retrievable operation result, a still-running
process, and resumable application work. A database row cannot restore lost process
memory after OS restart. Unknown side effects are reconciled before a new attempt.
Starting Codex or another model worker uses that provider's actual account limits;
the execution engine does not eliminate provider usage or authorization requirements.

## End-to-end acceptance

The eventual acceptance uses a fresh installation and new AI conversation, plus
an equivalent automated HTTP/MCP harness:

1. Install and connect the AI client without editing configuration files.
2. Edit and verify a file on Mac.
3. Complete an interactive command through PTY.
4. Operate and independently verify a GUI target.
5. Select Windows through the same connection; run and verify a command.
6. Interrupt transport, reconnect and recover the original operation without replay.
7. Resume an eligible durable task (a 0.4 requirement, not a 0.2 gate).

Each PR verifies its applicable segment. Record OS/backend/build, outcomes, calls,
output bytes and elapsed time; separate fresh-client, machine integration, native
UI and CI evidence. Feature counts, schemas or a successful connection alone do
not establish usability. Record remaining steps instead of weakening acceptance.
