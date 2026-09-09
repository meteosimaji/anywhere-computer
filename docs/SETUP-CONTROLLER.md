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

1. Connect the implemented local controller to the initial setup UI and a trusted native host transport.
2. Separate owner/tunnel credential steps using the native vault, with no secret fields in configuration DTOs, logs or persisted drafts.
3. Browser authorization start, waiting, completion and cancellation states around native login.
4. Device registration and connection checks with displayed receipts.
5. OS-specific permission and login-start controls using shared startup functions, explicit local interaction, and readback of real state.
6. A launchable distribution that does not require users to install Python/uv or edit configuration files manually.

The shared functions alone do not establish a public route, start a service, register OS startup, request administrator rights, or prove a successful first-run experience. The product goal retains all of those applicable acceptance gates.

Validation on 2026-09-09: related setup/HTTP/authorization tests 29 passed; full suite 496 passed, 5 skipped. Ruff, strict mypy and Windows-target mypy passed. Plugin validation passed. The installed version-specific runtime matched source and successfully planned/committed/read back a files-only configuration in a disposable directory without native credentials. No production setup or OS registration was performed.

Controller validation on 2026-09-09: added lost-result/double-confirm, stale-plan/conflict, pending-save/retry, and corrupt-state tests. Full suite: 500 passed, 5 skipped. Ruff and strict mypy (including Windows target) passed for 54 runtime modules. The reinstalled version-specific runtime matched source and passed an isolated controller confirmation/reconciliation smoke test. These checks do not replace the pending graphical first-run acceptance test.
