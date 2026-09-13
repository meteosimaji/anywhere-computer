# Windows owner-pipe integration acceptance (2026-09-13)

The working-tree integration adds an owner-authenticated Windows named-pipe route to
an existing local agent. It addresses SSH logon sessions that cannot read the
interactive logon's credential store. Explicit-credential TCP clients retain their
existing route. Invalid pipe identity is an error, not a reason to retry over TCP.

## Observed tests

- Windows ARM64 VM, installed alpha9 embedded Python, isolated source copy with the
  revised connection and owner-pipe modules: 9 native transport tests passed in
  1.168 seconds. The synthetic 8 MiB request/reply took 0.141 seconds.
- Actual `serve` plus `exchange`, with a synthetic in-memory server credential and
  client credential-store access forced to fail: file round trip, regex child,
  terminal reconnection, busy-stop refusal and agent cleanup passed. This uses a
  disposable state directory and does not replace the installed agent.
- Windows pytest: connection and deadline-contract suites, 11 passed in 1.33
  seconds. The new native integration test exercises file round trip and operation
  recovery using the real pipe route. Pure-Python test dependencies were isolated
  from the installed runtime; versions match the Mac locked test environment.
- Mac connection/runtime-selection/deadline suites: 21 passed before adding the
  Windows-only native integration case. After adding it, connection/deadline:
  10 passed, 1 Windows-only skip. Ruff and Mac/Windows-target mypy passed for the
  changed implementation before that test-only addition.

The endpoint-publication regression fails on the preceding implementation: after
an injected write error, the TCP server remains serving. The fixed implementation
includes endpoint publication in the server cleanup scope.

## Limits

These tests do not prove that the installed Windows agent has been upgraded, that
an interactive-login agent can serve the final SSH entry point, or that ChatGPT can
switch from Mac to Windows. Those deployment and end-to-end gates remain open.
The pipe tests are not GUI, HVCI, EAC or VRChat qualification.

## Cross-logon follow-up

The first deployed owner-pipe build failed before its hello: the interactive agent
received Windows error 5 from `OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)` when
inspecting the SSH client's process. A diagnostic subclass recorded the exception
class and stack frame without request payloads or credentials.

The revised server reads the client's bounded frame, identifies its Windows token
through `ImpersonateNamedPipeClient` / `OpenThreadToken`, restores its own identity,
and checks the owner SID before dispatch. The client requests identification-only
security QoS. The server process identity and hello checks remain on the client.
A real interactive-login server / SSH client round trip returned `{"ok":true}`.
Native tests including process-query denial and wrong-client-SID rejection:
11 passed in 1.356 seconds. Connection/deadline tests: 11 passed in 1.41 seconds.
This follow-up was verified in an isolated server; installed-agent and ChatGPT
acceptance still require the revised build to be deployed.

## Installed-agent and ChatGPT verification

The 43895bb candidate was deployed by overlaying the verified wheel onto a copy of
its existing portable runtime, retaining the previous installation. Runtime-only
portable verification passed. The interactive agent and SSH entry point now share
one working Windows instance. Mac Plugin routing completed Windows status, a
41-byte Japanese/emoji file write and exact read-back, terminal start/output/stop,
and operation-history retrieval. The terminal exited with code 0, but nested
`cmd /c echo` output included an unexpected trailing quote; exact command-output
compatibility is therefore still open.

A new ChatGPT conversation was tested with GPT-5.6 Sol / medium selected in the UI.
It discovered the registered Windows device and its tools, then reported an
unconfirmed `computer_status` response and `Invalid routed operation lookup` on
recovery. No Windows file/terminal acceptance was performed by that chat. The
Windows operation history independently showed no new status/file/terminal calls
from the chat's test interval. Its exact failed JSON arguments could not be
recovered from the inspected UI, so the following reproduction is a matching
failure mode, not proof of that chat's exact arguments.

A read-only real Windows call with an outer request ID and a different nested
`arguments.request_id` reproduced `Device response was not confirmed` with state
`unknown`. The source explains the conflict: the remote MCP catalog advertises
its transport `request_id` inside the routed schema, while SSHBackend supplies
the router's operation ID in MCP metadata. MCPSession rejects conflicting IDs
before engine dispatch. The HTTP recovery validator also rejects extra fields in
its nested OperationId arguments. This transport/schema mismatch needs a fix and
fresh ChatGPT acceptance before the Windows Chat route can be marked passed.

CI run 34742186547 for 43895bb completed successfully on GitHub. Passing CI does
not supersede the failed ChatGPT acceptance or the terminal-output discrepancy.

## Routed operation-ID repair

The router now removes the peer transport's request_id schema from nested tool
schemas. The outer devices_call remains the caller's recoverable ID. Nested
request_id values from stale catalogs are rejected before dispatch with an
explicit correction; HTTP recovery uses the same explanation rather than the
opaque lookup-validation error. Grant namespaces and operation replay checks are
unchanged.

Two new regressions failed before the patch. Device-router, real SSH stdio and
HTTP device-routing tests pass (13 tests), including response-loss recovery,
session reconnection and grant separation. Ruff and mypy passed for the changed
code. An isolated revised router using the actual saved Windows SSH entry point
returned a catalog with no nested request_id, completed Windows status, and
recovered that exact completed status by operation ID. This verifies the revised
source against the installed Windows agent; deployment of the gateway and a fresh
ChatGPT acceptance are separate remaining gates.

## ChatGPT Windows file and terminal acceptance passed

The Mac portable was rebuilt offline from the b33bab0 bundle, manifest/runtime
verified (3,114 files; file and regex checks passed), and installed alongside the
preserved f111c76 runtime. Idle engine replacement and the existing startup
upgrade command selected the new interpreter; native startup was verified running.

A fresh ChatGPT conversation used GPT-5.6 Sol / medium. The Windows status, isolated
directory creation, Japanese/emoji file creation and read-back, hash-conditioned
append and read-back, original operation recovery, terminal execution, separate
output retrieval and cleanup all completed. Independent Windows ledger retrieval
confirmed the actual terminal session 59d28f3d1889425bbd95541afdc535a1 returned
`ANYWHERE_WINDOWS_42\r\n`, exit 0 and EOF, and that recovery returned the original
56-byte creation result. Direct file read-back matched the chat's final SHA-256:
`1628875a60b2f00563e58cae3280b9b09c634aa6cf0c3893d2874cba3e232064`.
Final Windows status reported zero active sessions and operations. The chat's Mac
instance claim was not used as deployment evidence; native startup and live engine
receipts establish the selected Mac runtime separately.

This acceptance does not cover GUI or VM reboot continuity. Nested `cmd /c echo`
trailing-quote behavior was independently reproduced using Python subprocess on
Windows without Anywhere Computer; plain `echo` does not exhibit it. Windows
Codex's cached plugin command still points to the older installation and remains
a separate update task, even though the SSH wrapper uses the working agent.

## Windows installed Plugin updated

Windows was staged with the b33bab0 bundle in a separate portable installation.
The 4,620-file manifest, file round trip and regex child checks passed. The idle
interactive agent was replaced without restarting the VM. Its new instance is
8a8bd23d0fab4836bbc193df8b595f4d and runtime fingerprint is
42f10d9ce185a8ad96cd9f90d2e5b4aeaa472ac94a4b4ce09154b47c85f03314.
The previous Plugin cache and wrapper were backed up; the verified Plugin archive,
Windows-specific MCP command and SSH wrapper were updated. Executing the actual
installed .mcp.json command through MCP initialize and computer_status returned
that same new instance. This verifies the saved Plugin configuration, not a claim
that an already-open Codex conversation refreshed its cached tools.

CI run 34742801352 completed successfully on macOS, Windows and Ubuntu.
