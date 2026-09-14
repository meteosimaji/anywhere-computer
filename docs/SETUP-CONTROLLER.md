# Local setup controller integration

The initial setup screen runs on the user's computer before the remote MCP connection exists. It must not depend on a conversation widget to bootstrap that connection. The terminal-only setup remains available while the graphical flow is being implemented.

## Implemented shared configuration stage

`remote_setup.plan_remote_setup(...)` accepts public configuration fields and returns a frozen `HTTPServiceConfig`:

- Defaults: owner `owner`, client `anywhere-native`, port 8768, read-only access, native loopback OAuth callbacks.
- Canonical HTTPS `/mcp` resource, callbacks, identifiers and port are validated by the same service contract used at runtime.
- Permissions are a concrete set from the current tool catalog. The files profile includes the workspace presentation tool, but not terminal execution or settings mutation.
- Planning requests no terminal input and reads/writes no owner or tunnel credential. Its catalog inspection uses a disposable engine.
- The prospective device identifier and exact tool set belong to the plan. A screen may show these public fields and hold the plan while the user checks it.

`http_service.save_http_config(directory, plan)` commits that exact plan. It uses the existing lock and staged publication of configuration plus authorization enrollment. It does not recalculate the selected permissions. It refuses an existing configuration, including when the same plan is retried; callers should read the saved configuration to reconcile an uncertain commit, rather than overwrite it. Unknown/removed tool scopes are validated by enrollment.

The CLI `remote-setup` uses the same plan/commit functions. `http-configure` retains its original API through `configure_http`, which constructs a configuration and calls the shared commit function.

## Implemented screen state controller

`SetupController(directory)` does not create a state directory or access credentials.
`progress()` reads the current configuration and returns a typed `SetupProgress` with
`new`, `review`, `saving`, `configured`, `conflict`, or `invalid` phase. Here
`configured` means the HTTP configuration stage is saved, not that authentication,
startup, public reachability, or the entire setup has completed.

`review(plan)` validates and holds an immutable plan, returning its content fingerprint.
That fingerprint identifies the displayed plan; it is not a credential or endpoint
authentication mechanism. `confirm(plan_id)` rejects stale plans and serializes confirms.
A new plan cannot replace one while its save is awaiting completion.

If saving raises an error, the controller reconciles the actual disk contents before
returning. An exact matching saved plan is reported as configured; another saved plan
is a conflict; unreadable/corrupt configuration is invalid. If the finished save did
not publish a configuration, the screen can return to review with a retry message.
A fresh controller after restart reads saved configuration without replaying a save.
No generic network endpoint exposes these trusted-host methods.

## Still required for the setup application

1. Provide an initial launchable native host before any MCP connection exists. The installed local MCP now exposes the configuration stage in the workspace UI, but cannot bootstrap its own runtime.
2. Separate owner/tunnel credential steps using the native vault, with no secret fields in configuration DTOs, logs or persisted drafts.
3. Browser authorization start, waiting, completion and cancellation states around native login.
4. Device registration and connection checks with displayed receipts.
5. OS-specific permission and login-start controls using shared startup functions, explicit local interaction, and readback of real state.
6. A launchable distribution that does not require users to install Python/uv or edit configuration files manually.

The shared functions alone do not establish a public route, start a service, register OS startup, request administrator rights, or prove a successful first-run experience. The product goal retains all of those applicable acceptance gates.

Validation on 2026-09-09: related setup/HTTP/authorization tests 29 passed; full suite 496 passed, 5 skipped. Ruff, strict mypy and Windows-target mypy passed. Plugin validation passed. The installed version-specific runtime matched source and successfully planned/committed/read back a files-only configuration in a disposable directory without native credentials. No production setup or OS registration was performed.

Controller validation on 2026-09-09: added lost-result/double-confirm, stale-plan/conflict, pending-save/retry, and corrupt-state tests. Full suite: 500 passed, 5 skipped. Ruff and strict mypy (including Windows target) passed for 54 runtime modules. The reinstalled version-specific runtime matched source and passed an isolated controller confirmation/reconciliation smoke test. These checks do not replace the pending graphical first-run acceptance test.

## Local MCP configuration screen

The local stdio connector wraps DeviceRouter with SetupConnector. Only this wrapper
advertises `connection_setup_status`, `connection_setup_plan`, and
`connection_setup_confirm`; Engine and public HTTP do not register these tools.
Device routing rejects this prefix for both local and remote targets and removes it
from forwarded catalogs. `workspace_open` accepts `view: "connection"`.

The workspace's Connection tab is visible only for a local target whose advertised
capabilities include setup status. The screen asks for the public HTTPS resource and
access profile; owner, client and loopback port are under advanced settings. It shows
the exact planned scopes before confirmation. Changing an input invalidates the
screen's review so the old plan cannot be accidentally confirmed from that screen.
No password, tunnel token, OS permissions or autostart change travels through these
setup tools. Saving still uses the controller's create-only atomic publication.

Setup calls have an independent response guard: operation IDs and public state
shape must match before accepting a result. A missing/malformed save response disables
further planning and saving until status has been read successfully. The status call
inspects actual saved configuration; the UI never uses file-operation recovery or
automatically resends a configuration save. The screen labels the result as saved
configuration, not working authentication or a reachable service.

The browser fixture now wraps its disposable engine in this same local setup
connector, using a separate temporary connector directory. It exercises the actual
workspace resource and controller without credentials or a production service.

Setup operation IDs are bound to the outer connector request (tool and argument
hash) before any delegation. Reusing a setup ID for a file/terminal/device call is
rejected. A durable atomic dispatch claim prevents multiple local connectors from
starting the same setup request. Historical plan replies are actionable only while
they still identify this controller's current in-memory review; after a restart or
new review, callers receive unknown and must inspect status. Cancellation leaves the
claim in place. The database stores request digests and public setup replies, never
credentials or the argument contents of delegated tools.

Validation on 2026-09-09 for the integrated configuration screen:

- Full pytest: 509 passed, 5 skipped in 74.56 seconds. After strengthening transport
  metadata/prefix assertions, the related connector/UI tests passed again (13 tests).
- Ruff passed; strict mypy passed for 55 runtime modules and the browser fixture;
  Windows-target mypy passed for the runtime. No native Windows/Linux UI claim.
- Real headed Chromium: local Connection tab, files profile, exact reviewed device
  ID/resource saved to the fixture's config.json, terminal scope excluded, and saved
  status reload. The final browser console had no errors or warnings. The mobile
  screen was inspected at 420 pixels; Japanese heading wrap was corrected.
- UI regression covers wrong/malformed replies, unknown-save guard, status-only
  recovery, stale review after input changes, and missing/remote setup capability.
- Connector tests cover 47 local tools versus 41 Engine tools, workspace metadata,
  denied local/remote setup routing, cross-tool ID reuse, superseded/restarted plans,
  cancellation, and atomic claim across two connector instances.
- Plugin Creator validation passed. Installed
  `0.1.0-alpha.1+codex.20260909021332`; all 55 loaded runtime modules and the UI asset
  matched source. That version-specific runtime completed an isolated plan/save/status
  round trip with the files profile.
- All disposable browser fixture processes were stopped and their temporary state
  removed. The private repository remains private. The old Commander LaunchAgent was
  absent from launchctl and its disabled entry remained set.

Screenshots: `output/playwright/workspace-connection-desktop.png`,
`workspace-connection-mobile.png`, and `workspace-connection-saved.png`. This is
browser/real-engine integration evidence, not actual ChatGPT/Codex host acceptance.
The screen still requires an installed local MCP connector and a user-provided
public URL. One-click native runtime bootstrap, credentials, service launch, actual
internet reachability and official-directory acceptance remain outstanding.

## ChatGPT preset and issuer identification

The `chatgpt-setup` CLI uses the same resumable setup controller and native vaults.
For a new state directory it asks for the public MCP address and access profile;
owner (`owner`), client (`anywhere-chatgpt`), loopback port (8768), and the ChatGPT
callback are filled in. Owner-password and optional tunnel-token prompts remain
hidden and local. Existing setup for a different client is rejected before any
credential change; it is never silently repurposed or overwritten.

After the runtime is installed, start the setup from one command:

```sh
anywhere chatgpt-setup --state-dir /absolute/path/to/chatgpt-state
```

From a source checkout with uv already installed, use `uv run anywhere` instead
of `anywhere`. This command does not install Python/uv, provision a domain, start
hosting, change OS startup, or connect the ChatGPT account automatically.

The saved setup prints public connection fields for a predefined OAuth client:
MCP URL, client ID `anywhere-chatgpt`, public-client authentication (`none`), and
`https://chatgpt.com/connector_platform_oauth_redirect`. No client secret is
created or printed. Select the predefined OAuth-client option in ChatGPT and
complete owner login/consent after the service is publicly reachable.

The embedded authorization service now advertises RFC 9207 issuer identification.
Both approved and denied consent responses return `iss` equal to the metadata
issuer, including its exact authority spelling/port. The browser's normalized
Origin remains a separate CSRF check. Registered callback queries containing
`iss` are rejected to prevent duplicate issuer parameters. Arbitrary external
OAuthEndpoints embeddings do not advertise this support unless explicitly opted in.

OpenAI documents this issuer support as the prerequisite for new eligible MCP
connections to use the stable ChatGPT redirect. The connection management screen
remains authoritative for the actual redirect mode. This preset does not implement
CIMD, dynamic registration, or private-key JWT authentication.

Source: https://developers.openai.com/apps-sdk/build/auth

Validation of this preset/issuer update (2026-09-09):

- Full pytest: 540 passed, 5 skipped in 74.32 seconds. Ruff and strict mypy,
  including Windows-target mypy, passed on all 58 runtime modules.
- HTTP integration exercised predefined ChatGPT client registration, stable callback,
  exact issuer response, PKCE token exchange and the grant-filtered MCP catalog.
- Success and denial both include the issuer. Tests reject callback issuer injection,
  preserve explicit issuer authority spelling/port, resume completed ChatGPT setup
  without prompts or credential writes, and reject replacement of native-client setup.
- Public HTTPS integration verified matching issuer metadata/response, native login,
  one-dispatch lost-write recovery, 17 MiB hash-checked download, refresh rotation,
  grant revocation and a 6.67-second owned tunnel-child recovery retaining its MCP session.
  [Public receipt](research/2026-09-09-internet-issuer-verification.json) matches the current
  verifier script and runtime. This was a disposable engine through the public edge,
  not a ChatGPT app UI test or a production hosting setup.
- The fixture listener, owned connector children, temporary files and temporary
  keyring records were removed. The dedicated tunnel's retained token/route were preserved.
- Installed `0.1.0-alpha.1+codex.20260909030721`. All 58 source modules and the web asset
  matched the installed version-specific runtime; its CLI exposes `chatgpt-setup`.
  Runtime ID: `6efefa2ea387858b1431fe842c7d2b4064463c720397d4a8e272f2477cfbc211`.
- The old Commander LaunchAgent remains absent; launchctl reports its label as disabled.
- No user's production owner password, ChatGPT account setting, or autostart definition
  was changed by this update. Actual ChatGPT setup and one-command runtime installation
  remain outstanding acceptance requirements.

For AI-assisted ChatGPT setup, call `connection_setup_plan` with
`client_kind: "chatgpt"`, the public `/mcp` resource, and the intended permission
mode. The connector supplies the predefined ChatGPT client ID and redirect URI;
the assistant does not need to invent or transcribe them. Conflicting explicit
client/redirect values are rejected before review. Omitting `client_kind` keeps
the existing native-client behavior. The reviewed configuration still requires
`connection_setup_confirm`; this only saves public configuration and does not
create credentials, start a service, or connect ChatGPT automatically.

The connection screen now defaults its app selector to ChatGPT. It omits the
manual client field from ChatGPT drafts and lets the connector apply the preset.
Switching the app invalidates a previously reviewed plan. Existing configuration
is identified from its actual client and callback before choosing the displayed
app; saved configuration remains locked against replacement.

2026-09-09 browser verification: the disposable local browser host served the
actual workspace HTML and SetupConnector. Entering `https://fixture.example/mcp`
with ChatGPT selected produced `anywhere-chatgpt` and the predefined callback in
the review. Saving showed the configured state, disabled editing, and explicitly
stated that authentication/startup/connectivity were not performed. The temporary
host and tab were stopped afterwards. This is real browser UI testing against a
local fixture, not evidence of acceptance in ChatGPT itself.

## Explicit saved-device checks

The management device row has an explicit connection-check button. The native
command accepts only a registered 32-character hexadecimal ID, and invokes
`management-device-check --device ID` through the existing runtime. The shared
controller reuses `DeviceStore.probe` or `probe_http` in a worker thread and closes
the registry afterwards. No shell command or endpoint can be supplied by the UI.

SSH success means an authenticated agent status reported ready; HTTP success means
an authorized MCP catalog was obtained. The UI labels the latter as not yet an
actual operation test. Checks update the existing cached observation, but reopening
or refreshing the snapshot still describes it as cached. Raw diagnostic details
and credentials are not returned to the WebView. Failed checks are not retried
automatically. This applies to saved SSH/HTTP devices; relay enrollment is not yet
integrated with this device list.

macOS native check, 2026-09-14: built the manager with locked Cargo dependencies,
wrapped it using the existing `include_manager` bundle layout, and launched it
with a disposable state directory and explicit development Python. A saved SSH
fixture targeting the existing unavailable local VM forwarding endpoint was
checked through the actual button. This exposed CLI fallthrough: after printing
the device observation, the command attempted local engine status and exited 1.
A regression test reproduced it; returning after the observation fixed it.
The native UI then displayed the specific connection failure and timestamp.
Refreshing showed the previous `unreachable` observation while keeping current
connectivity unconfirmed. No guest operation succeeded in this test; it proves the
macOS app-to-CLI diagnostic path and failure presentation, not Windows acceptance.

## Guided CLI acceptance (2026-09-14)

`anywhere setup` offers an English selector for the existing local start,
ChatGPT self-hosted HTTPS setup and native OAuth self-hosted HTTPS setup. It
accepts only an optional state directory; advanced commands retain their existing
interfaces. This is a command selector, not hosted relay provisioning or automatic
AI registration. The local selection preserves `start`'s idle replacement policy.

On macOS, the development CLI was run in an actual interactive terminal with a
separate temporary state path. Cancelling with `q` exited successfully without
creating that path. A second isolated run selected local startup and returned
ready. After that interactive process exited, a separate status command reached
the same instance `1e70d22b0f4e465490ab01f6412a2ed4`, runtime
`466bad39694431b10f444dc07cd8167ee600b23015dea154aaa3aea563fe083e`.
The real native credential store was used; no credential values were logged.

The isolated agent was explicitly stopped, diagnosis reported stopped, and the
single fixture-specific Keychain entry was deleted and its absence verified.
Production state, login startup, Windows and AI account configuration were not
changed. This verifies local setup and terminal-exit continuity, not HTTPS setup,
a fresh OS installation, portable-package onboarding or reboot acceptance.
