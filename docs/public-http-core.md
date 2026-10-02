# Explicit public HTTP tool offering

The HTTP service can select `tool_profile="public-core"` for a new configuration.
Its offered tools must be a nonempty, explicit subset of `PUBLIC_CORE_TOOLS` in
`http_tool_profile.py`. This fixed list covers file and document work, transfers,
search, terminal and process work, isolated browser work, and their status and
operation recovery tools. Tool registration and prefix matching do not extend it.

Subchat gateways, private Codex history/skills/plugin bridges, standalone MCP
sessions, native GUI adapters, local skill discovery, device routing and global
operation history are excluded. A newly registered tool, including
`documents_edit_cell`, remains excluded until a separately reviewed change to the
offering explicitly adds it. General terminal commands still run on the owner's
computer with its existing OS permissions. This offering is an API publication
boundary and does not provide OS, filesystem or process isolation.

`http-configure --tool-profile public-core` uses explicitly supplied `--scope`
values. The local setup wizard and trusted `connection_setup_plan` also accept
the `public-core` access mode, which reviews the complete fixed list. The normal
workspace access selector keeps its existing choices. No service, credential or
authorization is changed by planning a configuration.

The profile is part of the reviewed, persisted HTTP configuration. Configuration
validation rejects private scopes or a Subchat selection; save revalidates copied
or constructed models before creating state or enrollment. Reload and HTTP
startup validate the same fields. The direct `AuthorizedDeviceMCP` entry point
requires explicit scopes for `public-core`, and the running HTTP service passes
the saved profile to it for either an embedded or shared Engine.

`http-add-tools` revalidates its expanded configuration before changing enrollment
or the saved configuration. It can add tools within the selected offering, with
fresh consent required for clients that lack those tools; existing grants retain
their original scopes. The CLI cannot use `--tool-profile` to switch an existing
service through this update command.

The default profile is `full`. Existing beta52 configurations that omit the new
field load as `full`, retain their exact enrolled scopes, and continue to support
their explicitly configured private tools. Selecting this offering does not
modify the local Engine or any other configuration.

`tests/test_http_tool_profile.py` checks public setup/review/save/reload, malformed
model copies and construction, private updates without persistent changes,
future tool registration, direct backend validation, embedded and shared Engine
HTTP listing/call denial, and legacy full compatibility. All state, owner vaults,
authorization and HTTP endpoints in these tests are disposable local fixtures.
