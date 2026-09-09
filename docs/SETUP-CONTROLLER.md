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

## Still required for the setup application

1. A local UI/controller with explicit draft, committing, saved and error states; reconcile uncertain writes by reading the actual saved configuration.
2. Separate owner/tunnel credential steps using the native vault, with no secret fields in configuration DTOs, logs or persisted drafts.
3. Browser authorization start, waiting, completion and cancellation states around native login.
4. Device registration and connection checks with displayed receipts.
5. OS-specific permission and login-start controls using shared startup functions, explicit local interaction, and readback of real state.
6. A launchable distribution that does not require users to install Python/uv or edit configuration files manually.

The shared functions alone do not establish a public route, start a service, register OS startup, request administrator rights, or prove a successful first-run experience. The product goal retains all of those applicable acceptance gates.

Validation on 2026-09-09: related setup/HTTP/authorization tests 29 passed; full suite 496 passed, 5 skipped. Ruff, strict mypy and Windows-target mypy passed. Plugin validation passed. The installed version-specific runtime matched source and successfully planned/committed/read back a files-only configuration in a disposable directory without native credentials. No production setup or OS registration was performed.
