# Anywhere Computer

English | [日本語](README.ja.md)

Work with your computers from MCP clients such as ChatGPT and Codex. Your AI
plans the work; Anywhere Computer executes file, search, document, terminal and
isolated headless browser operations on the computer you select. Core file and
terminal operations do not
require a Codex model turn. Project code is [MIT licensed](LICENSE).

[Download a release](https://github.com/meteosimaji/anywhere-computer/releases) ·
[Report an issue](https://github.com/meteosimaji/anywhere-computer/issues) ·
[Changes](CHANGELOG.md)

Release assets and their receipts establish what was published. This README
explains the checked-out source; a development feature is not automatically part
of an older release. Historical test records are kept outside this repository.

<!-- BEGIN GENERATED: project-reference -->
This checkout (not a publication or installed-runtime claim):

| Source | Value |
| --- | --- |
| [Python package](src/anywhere_computer/__init__.py) | `0.2.0a25` |
| [Codex Plugin version mapping](scripts/package_plugin.py) | `0.2.0-alpha.25` |
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

For the Codex Plugin in this checkout, install `uv` and make it available to
Codex, then register this checkout's local marketplace and install its Plugin:

```sh
codex plugin marketplace add /absolute/path/to/anywhere-computer
codex plugin add anywhere-computer@personal
```

The marketplace entry is in `.agents/plugins/marketplace.json`; the Plugin runs
its bundled wheel through `uv`. Restart the Plugin session, then check that
`anywhere-computer` and `anywhere-subchat` appear as separate MCP servers. Call
`subchat_capabilities` to confirm the Subchat server's actual mode and version.
If a prior manual `codex mcp` registration uses the same server name, its stale
wheel path can shadow the Plugin; see the [Subchat guide](docs/SUBCHAT-PROBE.md)
before diagnosing a zero-tool runtime as a Plugin packaging failure.
For a published version, use a checkout of its matching release tag rather than
assuming that this development checkout matches the release asset.

For Claude Code CLI or local Code mode, add this checkout as a Claude marketplace
and install the Plugin:

```sh
claude plugin marketplace add /absolute/path/to/anywhere-computer
claude plugin install anywhere-computer@anywhere-computer-local --scope user
claude mcp list
```

The Claude marketplace is `.claude-plugin/marketplace.json`. Its MCP servers run
the same bundled wheel as the Codex Plugin. Install `uv` first and restart the
Claude Code session after a Plugin update. The local stdio servers require this
computer to remain on; Claude cloud sessions and Claude Chat/Cowork need a
separately configured public HTTPS MCP endpoint. Installing this Plugin does not
make local files available to those cloud clients. Subchat sending remains subject
to the transport and login limitations below.

For ChatGPT, choose its HTTPS route in `setup`. You still need a public HTTPS MCP
URL, authentication and registration in ChatGPT. Installing the Codex Plugin does
not perform those steps. Hosting, tunnels and managed pairing are not silently
provisioned. Use the setup and client guides above for the actual connection steps.
On macOS, an explicitly selected, logged-in Chrome profile can also provide
direct `subchat_*` tools on that HTTPS MCP connection. Adding those scopes requires
a new OAuth consent; existing grants are not silently expanded. The selected
account is pinned, Chrome prepares the request in a background tab, and HTTPX
sends the generation request. See [HTTP server setup](docs/HTTP-SERVER.md) for
the opt-in command and recovery rules.

## Cooperative work with subchats

A subchat is an ordinary ChatGPT Chat used for a delegated task, separate from a
ChatGPT Work task. The experimental adapter supports explicit model selection,
submission tracking and correlated result recovery. The parent assigns scope,
compares evidence and verifies proposed changes before integrating them.

This checkout's Codex Plugin adds a separate Subchat MCP server. Its read-only
mode has tools for capabilities, catalog, saved operations, result recovery,
and bounded downloads of verified sandbox files and generated images. A selected
Chrome login profile also enables `subchat_refresh_auth`.
`subchat_capabilities.authentication_state` reports the current in-process
authentication state. If login is missing or denied, the server still exposes
its tools and saved operations; HTTP reads return `authentication_required` or
`access_denied` until the operator logs in and calls `subchat_refresh_auth` or
restarts that Plugin session. Authenticated reads that receive 401 refresh the
selected profile once and retry only the GET; sends and deletes are never replayed.
With an account ID pin, selecting another account reports `account_mismatch`;
the pin remains active on explicit refresh.
On macOS, an explicitly selected existing Chrome profile can supply the login
through a private headless snapshot (`ANYWHERE_SUBCHAT_CHROME_SOURCE_PROFILE`),
without opening or activating the ordinary Chrome window. Authentication and
model catalog GETs returned HTTP 200 in a live check; independent HTTP-only
generation remains unverified. A local `subchat/login-selection.json` can retain
the selected profile path and a Chat account ID pin across Plugin
updates. When the ledger location is overridden, place that file beside the
selected ledger directory. On macOS, `anywhere-subchat-setup inspect PROFILE`
reads the account ID from a private background snapshot; `select` saves a
verified profile and account pin. See the Subchat guide for the exact commands.
On macOS, a selected profile with an account ID pin and
`"enable_background_send": true` in `subchat/login-selection.json` enables the
background browser-prepared HTTPX send tools when the Plugin starts. Without
this explicit selection, or on other platforms, the default remains read-only. Set
`ANYWHERE_SUBCHAT_PLUGIN_TRANSPORT=http-read-only` to keep a pinned macOS
installation read-only.
Set `ANYWHERE_SUBCHAT_PLUGIN_TRANSPORT=browser-send` in the Plugin process to expose
`subchat_send` and follow-up tools through the dedicated, logged-in Chrome
profile. This mode minimizes Chrome but briefly activated its window in a live
macOS probe;
`subchat_capabilities` reports `generation_transport=browser_prepared`. Close
other Chrome sessions using that profile before starting the Plugin. The same
profile can be used by the two Plugin modes at different times. Independent
HTTP-only generation remains an opt-in experiment in the separate
`anywhere-subchat` CLI/MCP and has not passed live generation acceptance. The
existing Anywhere Computer engine and its remote ChatGPT connection are separate
from this Subchat server.

On macOS, `ANYWHERE_SUBCHAT_PLUGIN_TRANSPORT=browser-prepared-httpx` selects an
experimental sender that snapshots a selected logged-in Chrome profile when
configured, lets ChatGPT prepare one turn, and makes the generation POST once
with HTTPX. It starts a private Chrome profile in the background and uses a
temporary loopback CDP connection for turn preparation. In one live macOS run
observed at 30 frames per second, a new Chat and follow-up completed in the
same conversation with the exact saved answers, without Chrome coming to the
foreground. A separate live GPT-6 Pro new-send and follow-up run recorded
HTTPX `200 text/event-stream` for both generation POSTs. Neither
result guarantees no focus change in every environment or browser-free sending.
Select an existing profile with `ANYWHERE_SUBCHAT_CHROME_SOURCE_PROFILE` and pin
the account with `ANYWHERE_SUBCHAT_EXPECTED_ACCOUNT_ID` when multiple Chat
accounts are used.

The subchat guide is the authority for supported transports, CLI/MCP usage,
attachments, plugin selection, parallel-work evidence and remaining limitations.
Do not infer browser-free generation, native steering, child permission isolation
or automatic replay from the word "subchat". A Mac path is useful only when the
selected device and connected tools can access it; it is not an attachment or a grant.

Isolated browser sessions now expose `browser_click` and `browser_fill` in
addition to opening, navigating, observing and closing an owned tab. Actions
require a single visible target and return an unknown outcome if the result
cannot be observed; inspect the tab before another action. Existing Chrome
profiles and tabs are not part of this isolated-browser interface.

For explicit local agent-to-agent text delivery, `anywhere-peer --help` describes
owner provisioning and its separate MCP stdio server. Give each local peer its
own mode-0600 credential file and bind it to the same intended owner, account
and project. The recipient must be connected to receive a new message. This
mailbox records delivery and acknowledgement; it does not start a model turn
or insert text into a ChatGPT, Codex or Claude conversation.

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
