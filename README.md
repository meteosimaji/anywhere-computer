# Anywhere Computer

English | [日本語](README.ja.md)

A self-hosted execution agent for working with your computers from MCP clients
such as ChatGPT and Codex. Your connected AI plans the work; Anywhere Computer
runs file, search, document and terminal operations on the selected PC. Core file
and terminal operations do not require a Codex model turn.

The public release is `0.1.0a9` (`0.1.0-alpha.9` for the Codex Plugin).
[Download alpha9](https://github.com/meteosimaji/anywhere-computer/releases/tag/v0.1.0a9).
This is an alpha, not a stable or beta release. Development on main may contain
changes that are not included in that release.

Development also includes [common local Skills](docs/COMMON-SKILLS.md), independent
of Codex, with selected resource reads. Check the [implementation status](docs/IMPLEMENTATION-STATUS.md)
for features still awaiting packaged release and platform acceptance.

## Get started

Use the OS-specific portable ZIP from the release for a bundled Python runtime.
GitHub's **Download ZIP** contains source code, not the portable application.
See the [portable distribution guide](docs/PORTABLE.md) for launch instructions,
architecture requirements and verification.

For developers running from source, install [uv](https://docs.astral.sh/uv/) and use:

```sh
git clone https://github.com/meteosimaji/anywhere-computer.git
cd anywhere-computer
uv sync --locked --python 3.12
uv run --locked anywhere start
uv run --locked anywhere status
```

An available OS credential store is required: macOS Keychain, Windows Credential
Manager, or a supported Linux store such as Secret Service/KWallet. Headless Linux
also needs an available, unlocked credential store.

Configure a local MCP client with this repository as its working directory and:

```sh
uv run --locked anywhere mcp
```

This starts the agent if necessary and reuses a compatible running engine. A normal
connection does not silently replace it with another build. See the
[MCP client guide](docs/MCP-CLIENTS.md) for client-specific configuration and the
limits of the recorded Claude Code and Gemini CLI validation.

## Guided command entry (development main)

With the development runtime installed, run `anywhere setup` (or
`uv run --locked anywhere setup` from source). The English menu selects the existing
local start, ChatGPT HTTPS setup, or native OAuth HTTPS setup. Use the same
`--state-dir` to resume an existing configuration. Cancel before selecting a route
to leave setup untouched. Advanced commands remain available for scripts.

This selector does not provision a managed relay, a public URL, or AI-client
registration. The local route has the same idle-engine replacement behavior as
`anywhere start`. It is not part of the previously published alpha9 ZIP.

## Connect ChatGPT

Installing the Codex Plugin does not connect ChatGPT. After preparing the runtime:

```sh
uv run --locked anywhere chatgpt-setup --state-dir ./local-state/chatgpt
```

The existing guided setup needs a public HTTPS MCP URL. It supplies the ChatGPT
client ID and callback preset, uses hidden local credential prompts, and preserves
completed steps when resumed with the same command. Follow the startup and diagnosis
commands it prints, then add the MCP connection in ChatGPT and authenticate.

This flow does not automatically provision a domain or tunnel, register OS startup,
or add the connection in ChatGPT. The managed pairing/relay workflow is still in
development. The optional management app is a development preview; it is not
required for the CLI workflows documented here.

- [Setup controller and ChatGPT preset](docs/SETUP-CONTROLLER.md#chatgpt-preset-and-issuer-identification)
- [HTTP authentication](docs/HTTP-SERVER.md)
- [Optional Cloudflare tunnel](docs/CLOUDFLARE-TUNNEL.md)
- [Startup and persistence](docs/PERSISTENCE.md)

Connection authentication is separate from per-operation approval. Use your AI
client's permission settings. OS and external-provider authentication and permission
requirements still apply.

## Capabilities and limits

| Area | Public alpha functionality |
| --- | --- |
| Files | Listing, search, partial reads, writes, targeted edits, moves, backups and restore |
| Transfers | Chunked uploads/downloads, hashes and interrupted-transfer recovery |
| Documents | Word/Excel/PowerPoint text or cell reading; simple DOCX/XLSX generation |
| Terminals | Start commands, send input, collect incremental output and stop sessions; currently pipe-based |
| Continuity | Sessions survive client disconnects; operation IDs support result recovery and deduplication |
| Multiple computers | Explicit routing to registered SSH or authenticated HTTP devices, including through the HTTP gateway |
| Optional Codex adapter | Selected chat/Skill reading, supported Plugin calls and stateful Plugin sessions |
| Shared engine | Local and HTTP entry points can use the same engine |

Native GUI is not built in. The macOS GUI adapter uses separately installed
Peekaboo MCP and its OS permissions. Registered Codex tools do not imply that
Codex-only Computer Use execution contexts work through the bridge.
[GUI setup](docs/GUI-MCP.md) · [Codex compatibility](docs/CODEX-CONTEXT.md)

Independent browser integration, existing logged-in tabs, PTY/ConPTY, document
rendering and format-preserving editing are unfinished. Reading a Skill does not
install its dependencies or supply unavailable tools.

For multiple PCs, install the agent on each target and register its SSH/HTTP
connection. Select the device ID explicitly; an offline target is not substituted
with the local PC. See [device routing](docs/DEVICE-ROUTING.md).

## Operate and update

From a source checkout, prefix these commands with `uv run --locked`. Portable
launchers are described in the distribution guide.

| Task | Existing command |
| --- | --- |
| Inspect the local engine | `anywhere status` |
| Diagnose startup | `anywhere doctor` |
| List registered targets | `anywhere devices` |
| Diagnose a configured remote service | `anywhere remote-doctor --probe-public` |
| Request an idle engine stop | `anywhere stop` |

Use the same state directory when resuming setup, diagnosing a connection or
upgrading it. See [operations](docs/OPERATIONS.md) for arguments and service commands.

Updates are manual by default. Automatic stable updates are opt-in; alpha releases
are not applied automatically. Development builds discover an installed `gh` on PATH for stable update
verification; `--verifier` can still select an explicit absolute path. Published
alpha9 requires that explicit path. A stable release candidate is not yet available. Follow the
[update guide](docs/UPDATING.md) rather than treating `anywhere update` as an
unconditional one-command upgrade.

Starting from an updated installation can switch an idle engine while retaining
its state directory. Active terminal/Plugin sessions must finish before replacement;
engine restart does not restore their processes. Service registration may also need
`autostart-upgrade` followed by `autostart-start`. Do not move or delete the selected
runtime directory. Client-side tool refresh may still be necessary.

## Verification and troubleshooting

Recorded acceptance includes macOS authenticated HTTP, engine migration and result
recovery; Windows VM file/terminal work routed from ChatGPT; and OS-specific CI and
portable-runtime checks. These are scoped observations, not a guarantee that every
machine passes GUI, first-install, logout, sleep or reboot recovery.

- [Windows operation receipt](docs/research/2026-09-13-windows-owner-pipe-acceptance.md)
- [GUI acceptance record](docs/UPDATE-VERIFICATION-ALPHA8-2026-09-13.md)
- [Update/reconnection record](docs/UPDATE-VERIFICATION-2026-09-12.md)
- [Current acceptance gates and historical implementation notes](docs/EXECUTION-ROADMAP.md)

If a command is missing, check the terminal/service PATH and selected runtime.
If only remote access fails, diagnose the same HTTP state directory and check client
authentication. After a lost operation response, recover its original operation ID
before repeating a mutation. A cached ready observation is not a live connection.

[Report an issue](https://github.com/meteosimaji/anywhere-computer/issues) with OS/CPU,
version, reproduction steps and diagnostic state. Exclude passwords, tokens and
private file contents. Detailed documents include English and Japanese material;
full documentation and UI localization are not complete.

## Development

Core direct dependencies are pydantic, psutil and keyring. Direct MCP, browser and
relay dependencies are optional extras. Keep the existing engine and shared setup
controller; do not duplicate execution logic in a UI.

Start with checks relevant to your change, then broaden verification as required:

```sh
uv run --locked pytest tests/test_connection.py -q
uv run --locked ruff check src tests scripts
uv run --locked mypy
uv run --locked pytest -q
uv build
```

After runtime source changes, regenerate the bundled Plugin before package checks:

```sh
uv run --locked python scripts/package_plugin.py
```

[Architecture](docs/ARCHITECTURE.md) · [Product requirements](docs/PRODUCT.md) ·
[Changelog](CHANGELOG.md) · [Transfers](docs/BINARY-TRANSFER.md) ·
[Uploads](docs/UPLOADS.md) · [Search](docs/SEARCH.md)

## License

Project code is [MIT licensed](LICENSE). Bundled Python and third-party components
retain their respective licenses and notices.
