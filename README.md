# Anywhere Computer

English | [日本語](README.ja.md)

Work with your computers from MCP clients such as ChatGPT and Codex. Your AI
plans the work; Anywhere Computer executes file, search, document and terminal
operations on the computer you select. Core file and terminal operations do not
require a Codex model turn. Project code is [MIT licensed](LICENSE).

[Download a release](https://github.com/meteosimaji/anywhere-computer/releases) ·
[Report an issue](https://github.com/meteosimaji/anywhere-computer/issues) ·
[Changes](CHANGELOG.md)

Release assets and their receipts establish what was published. This README
explains the checked-out source; a development feature is not automatically part
of an older release. Dated test records describe the tested build and environment.

<!-- BEGIN GENERATED: project-reference -->
This checkout (not a publication or installed-runtime claim):

| Source | Value |
| --- | --- |
| [Python package](src/anywhere_computer/__init__.py) | `0.2.0a10` |
| [Codex Plugin version mapping](scripts/package_plugin.py) | `0.2.0-alpha.10` |
| [Python requirement](pyproject.toml) | `>=3.12` |

Canonical guides:

- [Downloads and portable installation](docs/PORTABLE.md)
- [MCP client configuration](docs/MCP-CLIENTS.md)
- [Guided setup and ChatGPT connection](docs/SETUP-CONTROLLER.md)
- [Commands and diagnosis](docs/OPERATIONS.md)
- [Updates and recovery](docs/UPDATING.md)
- [Multiple computers](docs/DEVICE-ROUTING.md)
- [GUI provider setup and limits](docs/GUI-MCP.md)
- [Subchat usage and limits](docs/SUBCHAT-PROBE.md)
- [Future milestones](docs/PRODUCT-ROADMAP.md)
- [Documentation ownership and checks](docs/DOCUMENTATION.md)
<!-- END GENERATED: project-reference -->

## Get started

For a bundled Python runtime, download the OS-specific portable ZIP from a
release and follow the portable guide above. GitHub's **Download ZIP** is source
code, not the portable application. Third-party runtimes retain their own licenses.

For source development, install [uv](https://docs.astral.sh/uv/) and run:

```sh
git clone https://github.com/meteosimaji/anywhere-computer.git
cd anywhere-computer
uv sync --locked
uv run --locked anywhere setup
uv run --locked anywhere status
```

`setup` offers local startup or guided HTTPS configuration. An available OS
credential store is required: macOS Keychain, Windows Credential Manager, or a
supported, unlocked Linux store. Local startup does not configure OS autostart.

For a local MCP client, use this checkout as the working directory and configure
`uv run --locked anywhere mcp`. It starts an absent agent or reuses a compatible
running engine; connecting alone does not replace it with another build.

For ChatGPT, choose its HTTPS route in `setup`. You still need a public HTTPS MCP
URL, authentication and registration in ChatGPT. Installing the Codex Plugin does
not perform those steps. Hosting, tunnels and managed pairing are not silently
provisioned. Use the setup and client guides above for the actual connection steps.

## Cooperative work with subchats

A subchat is an ordinary ChatGPT Chat used for a delegated task, separate from a
ChatGPT Work task. The experimental adapter supports explicit model selection,
submission tracking and correlated result recovery. The parent assigns scope,
compares evidence and verifies proposed changes before integrating them.

This checkout's Codex Plugin adds a separate Subchat MCP server with seven
read-only tools for capabilities, catalog, saved operations, result recovery
and a bounded download of one verified sandbox file.
It cannot create or send a subchat. Generation remains in the separately
configured `anywhere-subchat` CLI/MCP, using either its browser-assisted mode
or an opt-in HTTP-only mode with an explicit observed request handoff. Installing
the Plugin does not configure either mode. The existing Anywhere Computer engine
and its remote ChatGPT connection are separate from this Subchat server.

The subchat guide is the authority for supported transports, CLI/MCP usage,
attachments, plugin selection, parallel-work evidence and remaining limitations.
Do not infer browser-free generation, native steering, child permission isolation
or automatic replay from the word "subchat". A Mac path is useful only when the
selected device and connected tools can access it; it is not an attachment or a grant.

Use personal Chat HTTP access only within the permission and account scope granted
to you. Misuse, including extracting Chat output at scale for model distillation
or selling access to others, is prohibited. This experimental path is not a
documented OpenAI API. Your account may be restricted or banned for use outside
the permitted scope; the Plugin author cannot assume responsibility for such
account actions. Review the exact permission and applicable terms before use.

## Develop and verify

Use the repository environment (`uv run --locked`), not an unrelated system Python.
The [Quality workflow](.github/workflows/quality.yml) defines the required checks;
[documentation ownership](docs/DOCUMENTATION.md) explains which source to update.
After changing runtime code, commit the intended source changes, then rebuild the
bundled Plugin with `uv run --locked python scripts/package_plugin.py` before
package consistency checks. The packager refuses a dirty source tree by default;
`--allow-dirty` marks an unverified development build and must not be released.

Regenerate shared README facts with `uv run --locked python scripts/update_readme.py`.
CI runs its `--check` mode and rejects stale generated content or missing local link
targets. Feature behavior still needs code review and appropriately scoped tests;
a successful documentation check is not live-service acceptance.

When reporting a problem, include OS/CPU, the actual runtime version, reproduction
steps and diagnostic state. Exclude passwords, tokens and private file contents.
After a lost operation response, recover the original operation ID before repeating
an action. A saved receipt is not proof that a remote PC is currently online.
