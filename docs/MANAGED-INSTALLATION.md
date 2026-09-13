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
| 1: management and installation | Shared controller, native Mac/Windows status/startup controls and isolated registration/removal accepted (PRs 2–5). PR 6 merged after combined-package CI and relocated Mac/Windows native Start, authenticated file editing and window-close continuity acceptance. | Pairing; isolated relay; fresh Mac/Windows installation and connected file workflow; installer signing and distribution qualification. |
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

## Local login supervision (development follow-up)

`autostart-preview --startup-mode local` and `autostart-install --startup-mode local`
reuse the existing user-session registration, owned receipt, conflict checks,
lost-acknowledgement reconciliation, and uninstall/upgrade paths. The default for
new CLI registrations stays remote for compatibility. Repeating installation
without a mode retains the saved mode; an explicit different mode is rejected.
Old receipts without `mode` remain remote and render their original definition.
New receipts require this runtime; do not downgrade the registration reader.

Local mode does not require HTTP owner setup, Cloudflare, DNS, or a tunnel token.
It runs `local-watch`, which uses normal `ensure_agent` selection and startup
locking every 15 seconds. It never forces a runtime replacement or kills an
unresponsive engine. Configuration/credential errors leave a blocked watcher
until it is restarted, avoiding repeated login prompts. Stopping/uninstalling
this watcher leaves shared engine sessions running. Stopping just the engine
while the watcher is enabled causes the engine to start again.

An OS registration or a historical `engine_ready` observation is not a live
connection receipt. Status must still query the engine. Targeted tests cover all
three definition formats, local prerequisites, idempotence/mode preservation,
upgrade/removal, blocked errors and a real authenticated fixture engine retaining
its instance after the watcher stops. Actual login/reboot recovery and the desktop
control for registration remain unverified. No production registration was made.

Linux local registration uses `KillMode=process`: normal systemd control-group
cleanup would otherwise terminate the shared engine when removing only the
watcher. Remote mode retains control-group cleanup for its owned HTTP/tunnel
children. Native Windows task-removal behavior still needs explicit acceptance.

Local follow-up verification: focused startup/watch tests 61 passed / 3 skipped;
full Python suite 939 passed / 17 skipped (95.36 s), Ruff and mypy (82 sources)
passed. Definition-only Linux cleanup adjustment was verified separately after
that full run. These results do not prove login/reboot or native uninstall.

Native local-supervision acceptance (2026-09-14, PR #4): an isolated macOS
LaunchAgent started an authenticated engine; removing the registration retained
instance `899993646c724d5e8e0b1b2e968aae84`. An isolated Windows Task Scheduler
registration started instance `16751ee76ccf4edebc74b62f53704c18`; authenticated
status operations `8842918e4efc4ef2b2e9ee0476c4b76b` and
`fda010dd6592439f9e2df7b765e0d5a3` confirmed that instance before/after removal.
Both test registrations and engines were cleaned up. No reboot or active-session
continuity claim follows from these idle-engine removal tests.

The first Windows fixture was made from elevated SSH, with Administrators
ownership and OWNER RIGHTS ACL entries. Its normal-user task could not open
`local-watch.lock`. Creating a separate fixture from the interactive normal user
succeeded without relaxing ACLs. Run per-user installation in the intended user's
normal session, not an elevated SSH shell. Existing production settings stayed
unchanged.

## Management startup controls (development follow-up)

The management controller exposes a separate typed startup observation plus
explicit enable/disable results. CLI and three fixed native commands share it.
The WebView cannot supply an executable, directory, registration mode or shell
command. A missing registration is read without creating state. OS registration
and engine readiness remain separate observations with timestamps.

The controls apply to local startup only. Existing remote registrations are shown
but cannot be disabled through these controls. The removal controller also
checks the expected mode under the existing startup lock, so a mode change after
a UI observation cannot remove a remote registration. Mutation failures reconcile
once against OS state; they do not retry the write or expose raw errors. Controls
stay disabled while a mutation is pending or the observation is unknown.

Focused Python verification: 72 passed / 3 skipped; packaged-source equality test
passed. Rust tests: 3 passed / 1 child fixture ignored; Clippy and debug build
passed. Mac native button acceptance subsequently succeeded on 2026-09-14:
enable registered an isolated LaunchAgent, refresh showed a responding engine,
and disable returned the registration to `not_installed`. Authenticated status
before and after removal retained instance `c30d234cc2ec4bfa923c9bf3ae614736`
and runtime `2ee740c8fae7148d2adde42ac2413c7b5ca7c1db74b1c22b39e11faa07fa6fa0`.
Status operations were `37db1f02be6c4df986e9363c9488f0eb` and
`589527c5600f4eada9b91bb985583e32`. The latter followed clicking the window's
close button; the subsequent accessibility observation timed out, so that
observation alone does not prove window closure. The engine remained ready.
Explicit fixture stop `482ebe59e7cc446581466af94d392e73` was followed by a
`stopped` management observation. No production registration was changed.

The initial Mac UI acquisition timed out with a shell wrapper configured as the
test bundle's executable. Selecting the actual native executable in Info.plist
and launching it with the fixture arguments made the window accessible, without
a product-code change. This qualifies the corrected development fixture, not
the still-unfinished packaged installer. Windows native verification follows
below; CI compilation and CLI-level native tests do not replace that UI test.

Windows native button verification (2026-09-14) used the executable built from
`9700b3d93d58249f595ec2ada33ff3c60322e26b`, Actions run `34768326837`.
Its SHA-256 was `d8d3a1a1df0521ec0a76d49c649fed7f553f440bfca795c8b20b23a0a4c68ab8`,
checked again on the guest before launch. The isolated runtime wheel was also
hash-checked. The normal-user fixture, separate from production, was
`%LOCALAPPDATA%/Temp/anywhere-manager-native-9700b3d`.

The actual window displayed stopped/unregistered, accepted enable, displayed
registered and a responding engine after refresh, and accepted disable. The
window then displayed unregistered. Closing it ended the manager process with
exit code 0. Independent authenticated observations before and after removal
retained instance `026fa5f7c8a54d5e9bc4c37dddb35a38` and runtime
`2ee740c8fae7148d2adde42ac2413c7b5ca7c1db74b1c22b39e11faa07fa6fa0`.
Routed observation operations were `f703ab85d62b4e15a3e50468a8f2ce36` and
`4f1ea94672444e7eab04a1d53eac367c`. Explicit fixture stop
`424151f2bca04e89988b9edaaf5af726` was followed by absent agent metadata;
all test terminal sessions were closed. Production engine and VM remained up.

This test also exposed an unresolved usability defect: console windows appeared
in front of the manager during startup registration. The native manager's Python
child launch, native startup subprocesses and scheduled console interpreter are
the relevant launch paths to investigate; the screenshot alone does not identify
which process owns each console. Do not call the Windows installation experience
complete until this is fixed and retested. The fixture contains `pythonw.exe`;
compatibility with startup receipts and runtime selection must be checked before
changing the scheduled interpreter. The first verification command also found
`Get-FileHash` unavailable in this PowerShell environment; Python SHA-256 checking
succeeded without weakening verification or changing production settings.

Console follow-up: the Windows manager is now built as a GUI-subsystem process,
and its fixed Python readers and native startup-manager subprocesses request
`CREATE_NO_WINDOW`. A real Windows probe of the isolated previous implementation
reported a console handle; the patched native subprocess returned no console
handle while preserving a Japanese/emoji stdin/stdout round trip. This checks
that subprocess boundary, not the complete manager workflow.
New local Windows registrations select `pythonw.exe` beside the current
interpreter, never from PATH, and fail clearly if it is absent. Preview and
registration share that selection. Existing receipts retain their recorded
interpreter; remote registrations are unchanged. Focused Python tests: 93 passed / 5 skipped;
Ruff, mypy (3 sources), bundled-source equality, Rust tests and Clippy passed.
The Windows-only console regression test subsequently passed in Windows CI.

The corrected Windows manager from `3a9e562` was then exercised on the VM.
Enable, refresh, disable and manager exit completed; the persistent foreground
consoles seen before the fix were absent in those observations. This is a bounded
interactive check, not frame-by-frame proof against every possible transient.
Authenticated before/after status retained instance
`13a0e26cc1564fefb6f8db4bc077a7e7`, runtime
`bf67321e3e1f407c9e063772b830d224300f314288b0be853a460b4fc61a415f`.
The fixture registration and engine were subsequently removed/stopped, with
absent registration/agent metadata confirmed. Production remained unchanged.
[Full receipt](https://github.com/meteosimaji/anywhere-computer/pull/5#issuecomment-5654621700).
All current-head Mac/Windows desktop and Linux/Mac/Windows runtime CI checks
passed. PR #5 merged as `51529862845c1c032209c65883a22319ee8ade23`.
This completes the bounded management startup controls, not stages 0–5 or a
published installer: packaged runtime selection, pairing, relay, actual login
recovery and the other acceptance items above remain required.

CI exposed a test readiness race: the disabled-update case slept 300 ms before
probing the HTTP service. Adding a 500 ms startup delay reproduced the failure.
The test now waits for the real HTTP service context to enter before probing,
while retaining the live metadata check and update-monitor assertions. Both
immediate and delayed startup are covered. The complete remote-service test
file passed (18 tests); this changes test synchronization, not server behavior.

## Independent GUI acceptance checkpoint

On 2026-09-14, ChatGPT GPT-5.6 Sol with medium reasoning used the existing direct
Peekaboo MCP route on the Mac. TextEdit received Japanese and emoji text, then
a separate call appended `42` after the earlier `40`. Independent accessibility
inspection confirmed all three lines in the same document. Finder initially
remained at the wrong directory; re-observation and targeted input reached the
empty test directory `/tmp/anywhere-gui-test-mac-20260914`. Session close was
independently recovered from the operation ledger with `cleanup_confirmed=true`.
These are two real application workflows, not a long-duration or IME/DPI gate.

The registered Windows device responded to status and terminal commands, but no
usable GUI provider was established. Notepad, Explorer and browser GUI actions
were not executed. The static adapter advertisement is not runtime availability;
per-OS provider discovery and truthful readiness remain required work.


## Packaged manager checkpoint (2026-09-14)

The portable builder accepts `--manager /absolute/path/to/native-executable`.
It includes the native Windows executable or macOS application bundle in the
existing archive and file checksum manifest. The engine and dependency bundle
are reused; no second Python distribution or PATH-based interpreter is added.
Build the desktop with `cargo build --locked --manifest-path desktop/Cargo.toml`,
then pass that platform's executable to `scripts/build_portable.py` alongside
its trusted standalone `--runtime` and a new `--output` archive path.

Opening the packaged manager with no arguments selects the sibling
`runtime/python.exe` on Windows or `runtime/bin/python3` outside the macOS app
bundle. Move the entire portable directory together. The existing CLI selects
the user's state directory. Explicit native `--state-dir` remains available for
isolated acceptance, as does the earlier development `--python ... --state-dir
...` form. WebView callers cannot select these paths. A missing runtime is
reported in the status window and prevents management actions.

Mac acceptance used a fresh archive relocated under a Japanese directory name,
with PATH restricted to `/usr/bin:/bin`, no interpreter argument, and an isolated
state directory. Full archive verification plus the relocated runtime smoke
test passed (3,118 manifest files). Clicking Start launched the bundled engine;
authenticated UTF-8/emoji file creation, read and SHA-256-conditional replacement
from 40 to 42 succeeded. Closing the window retained instance
`b142ca1515c5445888fd7465ff42f507`; fixture stop was then confirmed. The initial
replacement harness omitted `mode=replace` and was correctly rejected without
an overwrite; the corrected call and subsequent read verified the result.
A separately built final native binary without its runtime displayed the
placement error in the real macOS window. Rust checks covered layout selection,
bootstrap-error propagation and reader cleanup; Python checks covered packaging
and archive verification. Windows packaged GUI acceptance was pending at this first checkpoint; the completed follow-up is recorded below.

These are unsigned development archives, not a completed installer, signing or
fresh-machine qualification. The native manager's file hash is in the manifest;
this is not an authenticity verifier at every launch. Moving the app alone is
unsupported. Pairing and relay operation are still separate unfinished work.

The additional ChatGPT 5.6 medium browser test used the Mac direct Peekaboo MCP
route: a new Chrome window navigated from example.com to example.org in the same
tab. Server result records contained the final URL and Example Domain heading;
independent native accessibility observation confirmed both. Session
`72a5c8c6aba24ee1ae081bbc7600de16` closed with cleanup confirmed in ledger operation
`b3da79bd926375da0c517d67d97ed0eb`. This extends the TextEdit/Finder checkpoint,
not Windows GUI, long-duration, DPI or IME qualification.


## Completed combined-package acceptance

PR 6 merged as `b794c54e58e64e03e2e8689a07fd0212c7ef0a10` after all ten
checks on `18c647f` passed. The immutable diff security review found no reportable
candidate; it does not qualify repository-wide security or signed distribution.
The native product sources are identical to the `042e4d0` CI artifacts used below;
the final change corrected a platform-specific test assertion and documentation.

Both artifacts were freshly extracted under isolated paths. macOS verified 3,118
manifest files; Windows verified 4,649. Each manager selected its bundled runtime
without `--python` and with a restricted PATH. Clicking Start in the real native
window launched an engine in a separate test state directory. Authenticated file
creation, readback, SHA-256-conditional replacement from 40 to 42 and final read
passed. Closing the manager retained the same engine instance. Explicit fixture
stop then succeeded; production services were not replaced.

| OS | Instance | Final file-read operation | Fixture stop operation |
| --- | --- | --- | --- |
| macOS | `5cc078ea7f634b8a8124ee4d52ab5d47` | `1ef49ca1efee4fcf9ef55982d3e4fdc7` | `97d9f05c139947a9937db05b80e6e64e` |
| Windows | `aa646ab55874482eb30641ef71b9e733` | `91fb5e8cdac34e428515aacea62d6e83` | `558f14d0fa3842a1b9be8c678ad2c363` |

Both fixture runtimes reported
`bf67321e3e1f407c9e063772b830d224300f314288b0be853a460b4fc61a415f`.
On Windows, starting the target interpreter before manifest verification changed
24 bundled bytecode cache files and correctly failed the integrity check. The
successful retry used a fresh extraction and an already trusted host interpreter
to verify the manifest before starting the target runtime. No checksum requirement
was relaxed. Native window startup, runtime-only smoke verification, and GUI
automation of arbitrary Windows applications remain separate test layers.

## Association boundary for the next implementation

Code inspection at `042e4d0` distinguishes existing HTTP setup from enrollment:
`plan_remote_setup` generates a single-owner configuration with a random device
ID, exact tool scopes and registered callbacks; `SetupController` previews and
publishes that configuration. `BrowserAuthorization` authenticates the already
initialized local owner and grants an AI client access to that resource. Neither
component associates a new PC with an account on a multi-user relay. Reusing
that consent form as device enrollment would conflate two different grants.

Keep the existing personal/self-hosted HTTP setup compatible. The relay account
and device association belong to a separate optional service boundary. The
manager should use an external browser for native authorization with PKCE;
the code/QR alternative is for completing association on another device. Neither
flow accepts account passwords in the manager. The pending enrollment belongs
to one local attempt and configured issuer, not a display name. Cancellation,
expiry and server denial terminate it; an uncertain token exchange must not
restart enrollment or silently create a second device. Credential publication
must complete in the OS vault before the manager reports association complete.

Three credentials remain distinct: account enrollment, AI-to-relay access, and
PC-to-relay transport. Enrollment alone grants no AI tool execution. Delivery
must bind the authorized account, device ID and operation ID; duplicate names
or an offline device never select another device or local fallback. Existing
engine result recovery remains authoritative after dispatch. The relay must
retain a bounded receipt identifying whether dispatch occurred, rather than
turning an uncertain response into another execution.

The next isolated slice must exercise the enrollment client against an actual
local HTTP fixture: successful association and vault readback, cancellation,
expiry, rejection, repeated code use, wrong issuer/state, and lost exchange
responses. Device-flow polling must retain increased intervals after slow_down
and stop on terminal errors. This fixture does not establish public service
operation. Select a maintained authorization implementation before introducing
service-side token issuance; do not transplant the existing single-owner store
into a multi-account service or implement signing primitives anew.

References checked 2026-09-14: [RFC 8252](https://www.rfc-editor.org/rfc/rfc8252.html)
for external-browser native authorization, and
[RFC 8628](https://www.rfc-editor.org/rfc/rfc8628.html) for the separate-device
flow. The paragraphs above are design decisions and pending acceptance, not
claims that these enrollment or relay paths already exist.

### Device-flow polling implementation (2026-09-14)

`device_polling.DevicePolling` now implements the credential-free lifecycle for
one device authorization attempt. It delays the first request, permits one
request in flight, retains each five-second `slow_down` increase, doubles the
interval on connection timeouts, and stops on cancellation, expiry, denial or
an unknown exchange result. The clock is monotonic; tests advance it explicitly
without sleeping. `authorized` means only that the transport reported a valid
grant, not that a device is registered or credentials were stored.

This is an internal component, not a new public tool or working pairing screen.
The transport must validate response bodies before classifying them; a lost
response after a possible exchange is `unknown_result`, not a connection timeout.
Cancellation prevents a late response from reactivating the attempt. The owning
transport must also discard any late credentials and perform appropriate remote
cleanup; stopping local polling does not revoke a server-side grant.

The targeted lifecycle suite covers initial pacing, repeated slowdown, expiry
before the next poll, in-flight cancellation, terminal outcomes and timeout
backoff. The following client slice connects that lifecycle to HTTPS; neither
slice is yet wired into the manager or a deployed relay.

### Isolated enrollment authorization client (2026-09-14)

`DeviceAuthorizationClient` now retrieves a device code, exposes the short user
code and verified-origin activation URL to its owning UI, and polls an explicitly
configured token endpoint. `progress()` is read-only with respect to the network;
the caller schedules `poll()` using `retry_after`. One instance represents one
explicit attempt. A second call to `start()` never creates another code. Keep
this object in the native controller rather than constructing it on every UI
refresh. Network operations can run in a worker thread; cancellation and result
publication share a lock so a late response cannot revive a cancelled attempt.

This first provider profile requires HTTPS and same-origin issuer, device and
token endpoints. Verification URLs must have that origin too. Explicit issuer
fields must match, and granted scopes must equal the requested enrollment scope
(an omitted response scope has the RFC 6749 meaning: unchanged). Cross-origin
identity-provider configurations and automatic metadata discovery are not yet
supported. This is a constraint of this client profile, not an OAuth requirement.

`enrollment_http` uses the standard-library HTTPS client, verified certificates
and hostnames, no redirects/proxies/implicit retries, and a 16 KiB JSON limit.
It rejects duplicate JSON fields and non-JSON or compressed bodies. DNS/connect
uses the socket timeout; after TLS connects, a separate watchdog bounds the
whole exchange, including slow-drip responses. Operating-system DNS resolution
is not guaranteed to obey the socket timeout. Only a timeout before any HTTP
request bytes could be sent permits backoff/retry. A partial write, lost reply,
malformed token success or unexpected HTTP status becomes `uncertain` and stops.

`EnrollmentCredentials` reuses the native credential-backend selector and
process lock, with an `enrollment-grant-` namespace bound to state directory,
issuer, client and profile. It preserves existing AI credentials and refuses to
overwrite another enrollment attempt. Exact vault readback is required before
`grant_saved` is reported. A failed publication can be retried using the same
in-memory grant; it never repeats code redemption. Process restart does not
automatically resume this in-memory attempt. Cancellation stops local work; it
does not revoke a grant already issued by the authorization server.

The local TLS fixture exercises the real client HTTP exchange using temporary
certificates and synthetic server replies. Its 35 tests cover success, pacing,
issuer/scope checks, cancellation during both requests, connection loss,
redirect refusal, invalid bodies, total exchange timeout, credential-write and
readback failures, and isolation from existing credentials. Together with the
polling, existing native PKCE login, token and setup-controller suites, 89 tests
passed on macOS. Ruff passed for `src tests scripts`; strict mypy passed for
86 source files. A separate macOS native Keychain check saved and read back one
disposable synthetic enrollment grant, then verified its removal. A separate
Windows VM check at `81af9bd` ran the same isolated save/readback/removal through
Anywhere Computer's routed terminal with the real `keyring.backends.Windows`
backend, returning exit code 0 and confirmed cleanup. These checks prove the
OS-vault layer separately, not a real account authorization.

The Windows TLS fixture initially exceeded its two-second request-arrival wait:
an IPv4-only listener addressed as `localhost` first tried IPv6. A Windows VM
probe measured 2.063 seconds for `localhost` versus less than a millisecond for
`127.0.0.1`. The fixture now uses the literal IPv4 endpoint and a matching
temporary certificate SAN; a hostname mismatch still fails verification.
The related 46 tests passed locally after this correction. A separate existing
JavaScript CI timeout did not reproduce in five Windows VM runs (0.08–0.17
seconds each); this is not evidence that its CI cause has been established.
Both push and pull-request CI subsequently passed all ten checks at `81af9bd`,
including the Windows full suite (979 passed, 27 platform/fixture skips in the
push run), portable build, relocated runtime verification, and provenance
verification. PR 7 merged as `8ca7692`. These are CI and isolated OS receipts;
the enrollment client is still not installed into the production manager flow.

Still pending: account authentication with a maintained authorization server,
server-enforced one-use codes and rate limits, actual device registration and
PC-to-relay credentials, registration/result recovery across restart, manager
wiring. Existing `native_login` already
provides PKCE and a bounded loopback callback for personal HTTP client login;
enrollment integration must reuse that behavior without reusing its AI tokens.
No public relay, endpoint, native permission or production credential changed.

### Recovering a saved enrollment grant (2026-09-14)

The trusted registration client can reopen the same enrollment vault namespace
and request its access token with the original attempt ID and exact scope.
Loading validates the saved format, issuer, client, attempt, scope and lifetime;
an expired grant, a clock earlier than its issuance, malformed storage or a vault
failure returns a redacted credential error. Reading never rotates, removes or
replaces credentials and never redeems the original device code again. The
returned token is internal and must not enter management IPC or MCP responses.
This credential-store operation alone does not register a device or indicate
that a PC is connected. The subsequent isolated registration implementation
validates the bearer audience/account at the relay, persists the original
registration request locally and connects it to the native manager worker;
see [the current relay implementation](RELAY-PROTOTYPE.md). That implementation
still does not establish PC transport or complete default-installation pairing.

The targeted credential and device-authorization suites passed 52 tests on Mac.
The isolated Keycloak runner also reopened its actual received grant with a new
credential-store object and rejected reuse of the consumed device code. That
runner uses synthetic credentials in memory; it is not a native-vault restart
test or a rendered manager acceptance test. Server expiry was not repeated for
this change; its earlier separate receipt remains the expiry evidence.

The next provider compatibility check is documented in
[ENROLLMENT-PROVIDER-TEST.md](ENROLLMENT-PROVIDER-TEST.md). It uses an isolated
Keycloak release and the actual device-flow HTTP endpoints, with a synthetic
account and memory-only grant storage. This is separate from the native-vault
receipts above and from production account/device association.
