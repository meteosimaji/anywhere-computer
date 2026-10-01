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
| [Python package](src/anywhere_computer/__init__.py) | `0.2.0b52` |
| [Codex Plugin version mapping](scripts/package_plugin.py) | `0.2.0-beta.52` |
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
its bundled wheel through `uv`. After a Plugin update, start a new Codex task so
its MCP processes load the updated wheel. Then check that
`anywhere-computer` and `anywhere-subchat` appear as separate MCP servers. Call
`subchat_capabilities` to confirm the Subchat server's actual mode and version.
When accessing Subchat through ChatGPT's `codex_plugin_call` bridge, open
`codex_plugin_session_open` first, then pass its `session_id` to
`codex_plugin_tools` and each Subchat send, message, observe, recover, wait or queue-watch
call. Keep the session open until the answer is recovered, then close it with
`codex_plugin_session_close`. The temporary bridge context cannot retain a
pending send after its call ends, so those stateful calls without a session are
rejected before dispatch.
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
For an in-place Claude update, refresh the marketplace and run `claude plugin
update anywhere-computer@anywhere-computer-local --scope user`, then start a new
Claude Code session. Claude Code can report `up_to_date` when a development
wheel changes but keeps the same Plugin version. Publish a new immutable
version for an actual update and verify the installed wheel hash.

For ChatGPT, choose its HTTPS route in `setup`. You still need a public HTTPS MCP
URL, authentication and registration in ChatGPT. Installing the Codex Plugin does
not perform those steps. Hosting, tunnels and managed pairing are not silently
provisioned. Use the setup and client guides above for the actual connection steps.
ChatGPT cannot call a Mac's `localhost` MCP address directly. For private
developer-mode testing without an owned domain, [OpenAI's Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels) is
another transport to evaluate; it requires an OpenAI Platform tunnel and a local
`tunnel-client`, and this project has not yet qualified that path. Public Plugin
submission still requires a stable public HTTPS endpoint.
On macOS, an explicitly selected, logged-in Chrome profile can also provide
direct `subchat_*` tools on that HTTPS MCP connection. Adding those scopes requires
a new OAuth consent; existing grants are not silently expanded. The selected
account is pinned, Chrome prepares the request in a background tab, and HTTPX
sends the generation request. See [HTTP server setup](docs/HTTP-SERVER.md) for
the opt-in command and recovery rules.

## Delegated research with subchats

A subchat is an ordinary ChatGPT Chat used for a delegated task, separate from a
ChatGPT Work task. The experimental adapter supports explicit model selection,
submission tracking and correlated result recovery. The parent assigns scope,
compares evidence and verifies proposed changes before integrating them.
Ordinary Chat Subchats do not directly exchange intermediate results. Local
Codex and Claude Code peers can use the separate mailbox described below;
mailbox delivery does not prove a model read or acted on a message. Sending several
children can help when the tasks are independent; overlapping research adds
creation and recovery time. Use a single Chat for one closely related inquiry.
Give each child the exact sources it can access and a bounded result format;
`work_context` records provenance but does not share the parent's tools or
history. Start independent children before waiting, continue parent work, then
recover each saved operation ID and verify its evidence.

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
For the ordinary macOS Chrome store, `anywhere-subchat-setup discover` lists
profile IDs with observed Chat account IDs, and `choose 'Profile N'
--expect-account-id ID --enable-background-send` saves an ID-based selection.
`anywhere-subchat-setup revoke` removes that selection for the next Plugin start.
For a stopped HTTPS LaunchAgent that cannot read Chrome's protected files,
`anywhere-subchat-setup stage PROFILE --expect-account-id ID --http-state-dir DIR`
copies a filtered login into the app's state and atomically switches only the
existing HTTP Subchat profile selection. See [HTTP server setup](docs/HTTP-SERVER.md).
On macOS, a selected profile with an account ID pin and
`"enable_background_send": true` in `subchat/login-selection.json` enables the
background browser-prepared HTTPX send tools when the Plugin starts. Without
an explicit selection, the default remains read-only. On Windows, an owner can
prepare a dedicated Chrome or Microsoft Edge login and save a verified account,
browser choice, and send consent with `anywhere-subchat-setup choose-dedicated`.
If a fresh Windows task reports `http_session_required` from `subchat_catalog`,
check this dedicated profile selection before diagnosing the Plugin version or
retrying a send. An updated Plugin does not create a login selection by itself.
See the [Subchat guide](docs/SUBCHAT-PROBE.md#windows-dedicated-chrome-or-edge-profile)
for setup and current live-validation limits. Set
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
Snapshots include a bounded accessible role tree and `snapshot_id`. Click and
fill accept exact role/name or label targets with that snapshot ID, so a page's
CSS class changes do not require a new selector. Stale snapshots and ambiguous
targets fail before input. The action resolves the current element after a page
rerender and waits briefly for it to become visible. Exact CSS selectors remain
available.
On macOS, the built-in native Accessibility helper can also return a compact
list of actionable AX role/label/identifier targets. New `gui_native_press_target`
and `gui_native_set_value_target` tools require a unique target in a fresh
observation, with app and window identity checked again before input.
Isolated-browser observations also include structured HTML form labels and
CSS-pixel element boxes. Call
`browser_observe` with `include_image=true` to receive the rendered viewport
as a bounded native MCP image alongside the semantic observation. The image
and structure are captured sequentially and can differ on a changing page.
The default isolated browser is installed Edge on Windows and Chrome on macOS
and Linux. A local startup failure returns `browser_startup_unavailable` before
any tab navigation.
The same session and tab IDs continue across document and SPA navigation.
Snapshots include `last_navigation` for the most recent explicit navigation:
its requested URL, confirmed or unconfirmed outcome, and latest observed URL.
After an unconfirmed navigation, `browser_observe` reads the live tab without
replaying the request. A matching observed URL does not prove the attempted
navigation completed.
The isolated tab also exposes bounded live DOM source, redacted network
metadata, and compact source-claimed publication details and visible links for
research. `browser_key`, `browser_drag`, `browser_file_upload`, and
`browser_download` use exact targets and recoverable operation IDs. Downloads
save to a new path and return a SHA-256 digest. A local FFmpeg installation adds
`media_audio_clip` and `media_video_frames`; audio and image data are sent as
native MCP media items when the client accepts them. See the
[GUI guide](docs/GUI-MCP.md) for limits and the
[comparison](docs/GUI-AUTOMATION-PLAN.md) for remaining gaps. The receiving
Chat model's audio perception is a separate acceptance check. For a Chat client
that cannot hear the audio item, optional `media_transcribe` returns text from
an explicitly installed, trusted local Whisper checkpoint without downloading
a model during the tool call.

For explicit local agent-to-agent text delivery, `anywhere-peer --help` describes
owner provisioning and its separate MCP stdio server. Give each local peer its
own mode-0600 credential file and bind it to the same intended owner, account
and project. The recipient must be connected to receive a new message. This
mailbox records delivery and acknowledgement; it does not start a model turn
or insert text into a ChatGPT, Codex or Claude conversation.
The [peer messaging design](docs/PEER-MESSAGING.md) describes runtime support,
security boundaries and the comparison with agmsg.
Supply a fresh 32-character lowercase hexadecimal `request_id` for every
`peer_send`, and retain it until the result is known. An omitted ID is rejected
before storage. Repeat an identical call with the same ID only to resolve an
uncertain response. `peer_ack` confirms receipt by the local client, not that a
model read the text or completed the requested work.
An active Codex or Claude Code turn can call `peer_wait` once to wait up to ten
seconds for unread text in that tool result. It does not acknowledge the text
or wake an idle model turn.

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
