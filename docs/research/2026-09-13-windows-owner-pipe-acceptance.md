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
