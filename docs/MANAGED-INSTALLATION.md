# Managed installation implementation record

## Scope and release gate

The 2026-09-13 implementation directive is the acceptance contract. Complete
stages 0–5 before publishing `0.1.0b1` / Plugin `0.1.0-beta.1`. Intermediate
reviewed changes remain alpha releases. A passing fixture, source build, or
GitHub release does not prove a hosted relay or fresh-machine installation.

Commit, push, main integration, and eventual release publication are authorized.
Paid contracts, production relay deployment, production signing keys, and changes
to users' production permissions/configuration require separate authorization.
Continue isolated development when those external steps are not authorized.

## Baseline and progress

Baseline: `f860eedb041dca8ba5a3ef71fda5890ef09263d0`, clean `main`, public alpha9.
The first working branch is `codex/managed-installation`.

| Stage | Current evidence | Remaining acceptance |
| --- | --- | --- |
| 0: baseline | Existing setup, device router, update, runtime, CI and package contracts inspected. Before changes: setup/controller/router tests 31 passed in 4.25 s. | Maintain a requirement-to-evidence record as each subsystem changes. |
| 1: management and installation | Read-only shared Python controller and thin Tauri management preview implemented locally. Mac native refresh displays stopped/unconfigured and leaves an absent fixture directory absent. | Windows native UI verification; packaged runtime; startup; pairing; isolated relay; fresh Mac/Windows file workflow. |
| 2: update/recovery | Existing release preparation, verification, activation and supervisor are retained. | Unified product controller, lifecycle failure matrix, actual multi-entry runtime verification. |
| 3: efficiency | Existing direct MCP search retained. Device schemas still fetched without filters. | Selective device catalogs and measured payload/request reductions. |
| 4: GUI/browser | Existing Peekaboo route retained; no default provider change. | Pinned provider evaluation and per-OS application/IME/DPI tests. |
| 5: tools | Existing files/pipe terminals/documents/Codex skills retained. | PTY/ConPTY, bounded document changes/preview, independent shared Skill resources. |

Previous alpha9 documentation contains live ChatGPT and Windows acceptance
records. Those records remain historical evidence, not a new beta acceptance run.
No production installation or permission has been changed for this preview.

Focused controller/device/setup/diagnostic tests: 30 passed (1.08 s).
Rust decoder tests: 2 passed, covering malformed/incompatible/oversized output
and preservation of Japanese/emoji text. Native Mac `cargo check` and build
passed. A subsequent real-process test covers hung and oversized readers: the
host awaits their termination, and the test verifies an exit status is available.
Rust now reports 3 passed / 1 ignored; the ignored entry is the child fixture
explicitly invoked by the cleanup test, not missing acceptance coverage.
These do not establish Windows GUI acceptance. All-target Clippy passed.
CI now includes locked native host tests/builds on Mac and Windows; execution
results must be recorded after publication rather than assumed from this config.

First-change local checkpoint: full Python suite 930 passed / 17 skipped in
92.86 s; Ruff passed; mypy passed for 81 source files; Rust Clippy passed with
warnings denied. Source distribution and its wheel built successfully. The
bundled Plugin was regenerated from the working source, marked unverified/dirty;
it is not a new published release or an installed-runtime update.

## First change: one controller, one read-only screen

`ManagementController` composes existing `diagnose`, `SetupController`,
`DeviceStore`, and the saved automatic-update preference. CLI command
`management-status` and the desktop host consume the same typed snapshot.
No terminal text parsing or second authentication implementation is added.

Opening the screen does not initialize an absent state directory, migrate the
device database, start an agent, query a remote device, or activate an update.
The existing device registry now has an explicit SQLite read-only mode; its
existing decoders/list logic remain shared. Unsupported registry versions are
reported without migration. Device observations are marked cached with their
original check time. Saved HTTP setup is never labelled a verified connection.

The model returns an allowlist of engine fields. Credential values, arbitrary
diagnostic payloads, and stored device error detail do not enter the WebView.
Saved auto-update preference is not evidence that an update supervisor is running.

## Desktop decision (provisional)

Evaluate Tauri 2 as requested, with `tauri 2.11.5` and `tauri-build 2.6.3`,
resolved on 2026-09-13 and pinned in Cargo.lock. A plain HTML/CSS/JS screen
avoids another frontend framework or Node-based production runtime. Rust and
the OS WebView are additional build/maintenance dependencies; this is not a
dependency-free desktop distribution. The initial lock resolves 433 packages
across supported targets. Native packaging, WebView2 prerequisites, signatures,
and relocation must still be qualified.

The native host exports only `management_snapshot`, with no JavaScript-controlled
command/path arguments. It runs the selected interpreter directly, uses bounded
stdout and a timeout, discards stderr from the UI, and accepts snapshot schema 1.
Python uses the same `-I -X utf8` isolation/encoding flags as internal engine
launchers, so working-directory imports, PYTHON settings, and the Windows locale
cannot select an unintended module or alter snapshot encoding.
The WebView receives no shell/filesystem plugin. Remote content is not loaded.
The fixed Python selection is currently a development-only native command-line
argument, not a finished installation mechanism. Production will use a verified
packaged runtime and the existing runtime-selection contract.

Closing the screen only closes the manager; the existing agent has its own
lifetime. This is not proof of login startup, singleton management, or sleep
recovery. Those belong to the following change sets.

Reference: [Tauri sidecar documentation](https://v2.tauri.app/develop/sidecar/),
accessed 2026-09-13. No additional Tauri updater is introduced; existing release
verification and activation remain the single engine-update authority.

## Development preview

From the source checkout with the locked Python environment prepared:

```sh
uv run --locked anywhere management-status --state-dir /absolute/fixture-state
cargo run --manifest-path desktop/Cargo.toml -- \
  --python /absolute/checkout/.venv/bin/python \
  --state-dir /absolute/fixture-state
```

The Windows interpreter is `.venv/Scripts/python.exe`. An absent fixture-state
is valid: the screen should show stopped/unconfigured without creating it.
These developer commands are explicitly not the proposed public installation
experience. Bundle generation is disabled until packaged-runtime qualification.

## Next bounded changes

Lifecycle follow-up (separate `codex/management-lifecycle` branch): explicit
`management-start` reuses `ensure_agent` without forcing a runtime replacement,
then reconciles observed status. A start acknowledgement alone is not readiness;
exceptions do not trigger a repeated start. The native start command accepts no
WebView arguments. Its timeout reports an unknown result, since an engine could
have started before the response was lost. UI enables start only after observing
stopped state and disables repeated clicks while the request is pending.
Seven focused Python tests pass, including authenticated observation of a live
fixture engine retaining its instance ID across start. Rust tests and all-target
Clippy pass. Mac native button operation started a fresh isolated engine on
2026-09-13. Closing the manager retained its ready response and identical
instance `d738d865c23f4cc4b89cff91d6e0fda5`, runtime
`cf4fdfc291bf9e4a0c2c4c370715e9d5c07ec6ec9c3a01087703bff2e526a2db`.
The fixture used a built wheel and locked dependencies in a separate test venv;
it was stopped after verification. Windows native button acceptance is pending.
This is not login startup or a completed installer.

The first native lifecycle attempt using the Documents checkout venv timed out
during Python initialization (`getpath_readlines` / `open`, observed by process
sampling). The same application using the separate installed-wheel fixture
responded. This establishes a launch-environment dependency, not a proven TCC
root cause; no privacy permissions were changed. Production packaging must use
its own verified runtime instead of relying on a development checkout path.

Startup integration finding: `startup_service._install_startup_locked` currently
requires `cloudflared_executable`, HTTP owner setup, and `TunnelCredential`, while
`autostart.startup_definition` always launches `remote-watch`. Local management
must not call that path as a universal installer. Extend the existing owned
startup receipt/definition contract with an explicit connection mode and matching
prerequisites, retaining old Cloudflare receipts and recovery behavior. Reuse
`connection.ensure_agent` for local runtime selection and busy-update refusal;
do not duplicate its restart or credential logic inside the desktop host.

1. Verify the native preview and its non-mutating status contract on both OSes.
2. Connect startup, existing setup planning, and update controllers with typed
   native requests, preserving current execution and approval boundaries.
3. Implement account/device association and the minimal relay as independently
   testable components; keep AI OAuth, native login, and device transport grants
   distinct. Public relay service remains unavailable until deployed/accepted.
4. Complete the remaining stages with the directive's failure and real-operation
   acceptance cases before changing the release channel to beta.
