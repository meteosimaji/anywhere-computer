# Subchat usage and limits

`subchat` works with an ordinary ChatGPT Chat used for a delegated task. It is
experimental and depends on the selected account, observed model catalog and
transport. It is separate from a Codex task, ChatGPT Work task, the Anywhere
Computer engine and the public OpenAI API.

## Codex Plugin: Subchat server

The Codex Plugin registers `anywhere-subchat` beside `anywhere-computer`. Its
installation and `uv` prerequisite are described in the [README](../README.md#get-started).
After installation, restart the Plugin session and confirm both MCP servers are
available before using the Subchat tools. In read-only mode the server exposes
`subchat_capabilities`,
`subchat_activity`, `subchat_catalog`, `subchat_list`, `subchat_status`, `subchat_recover` and
`subchat_wait`, plus `subchat_download_file` and `subchat_download_image`. A selected Chrome login profile adds
`subchat_refresh_auth`, which reacquires the same account through a headless
snapshot and authenticated GETs. A 401 on an authenticated read also triggers one
refresh and one repeat of that GET. Check `subchat_capabilities` first. On macOS,
a selected profile with an account ID pin and `"enable_background_send": true`
in `subchat/login-selection.json` enables background browser-prepared HTTPX
sending at Plugin startup. Without that explicit selection, or on other platforms, the
default `generation_transport=unavailable` cannot create or send a Chat. Set
`ANYWHERE_SUBCHAT_PLUGIN_TRANSPORT=http-read-only` to keep a pinned macOS
installation read-only. Plugin installation does not configure generation for
the separate CLI.

An older manual `codex mcp` registration with the same server name can shadow
the installed Plugin and keep pointing to a removed wheel. Check `codex mcp
list` when a newly installed Plugin reports `runtime_status=failed` and zero
tools. If `anywhere-computer` or `anywhere-subchat` is a stale manual entry,
remove that entry with `codex mcp remove NAME` and let the installed Plugin
provide the server. Keep the Plugin enabled and reopen its MCP session before
checking `subchat_capabilities` again. Plugin updates do not rewrite unrelated
manual MCP registrations.

When ChatGPT calls Subchat through Anywhere Computer's `codex_plugin_call`
bridge, open `codex_plugin_session_open` with the workspace cwd first. Pass its
`session_id` to `codex_plugin_tools` and each `codex_plugin_call` for Subchat
send, message, recover, wait or queue watch. A one-call bridge context rejects
these operations before dispatch, including when the MCP server was registered
under a different name. Keep the session through final answer recovery and
close it explicitly. Idle cleanup consults `subchat_activity`; it preserves a
session while a send, recovery or queue watch is live, then allows normal idle
expiry after the work finishes. An explicit session close or Engine shutdown
still ends that local background work.
An older server without `subchat_activity` is rejected before a stateful bridge
call. Sanitized preparation failures are saved with their operation ID so a
later status call can report the reason after a controller restart; an exact
retry clears that prior failure before preparing again.

The separately authorized ChatGPT HTTPS Subchat gateway also exposes
`subchat_list`. It reads saved operation summaries for the selected account and
current principal without opening Chrome, so a client can recover an operation
ID after losing local state. Results are paginated. Operations owned by older
grant identities remain accessible only through their exact IDs after the
principal and account checks; the list does not enumerate those legacy rows.
Publishing this tool does not grant it to an existing OAuth connection. The
owner must consent to its scope before an ordinary Chat can invoke it.

To enable actual sends through the dedicated Chrome profile, set
`ANYWHERE_SUBCHAT_PLUGIN_TRANSPORT=browser-send` in the Plugin process and restart
its MCP session. This exposes `subchat_send`, `subchat_message` and related
mutation tools; `subchat_capabilities` reports
`generation_transport=browser_prepared`. The mode uses the same logged-in
dedicated profile by default, with an optional absolute
`ANYWHERE_SUBCHAT_BROWSER_SEND_PROFILE` override. Close any other Chrome process
using that profile first. The controller minimizes its Chrome window, but it can
briefly take focus; a live macOS catalog probe observed this. Obtain exact UI
model and effort labels plus an available
`http_selection` from `subchat_catalog` before sending, and use the returned
operation ID for `subchat_wait` or `subchat_recover`. Keep the controller alive
until a pending send reaches a confirmed result. This mode uses Chrome to send;
it does not prove independent HTTP-only generation.

On macOS, `ANYWHERE_SUBCHAT_PLUGIN_TRANSPORT=browser-prepared-httpx` exposes the
same send tools with a different generation transport. Use the dedicated logged-in
profile, or select an ordinary Chrome profile through
`ANYWHERE_SUBCHAT_CHROME_SOURCE_PROFILE` or the local `login-selection.json`.
For multiple accounts, set `ANYWHERE_SUBCHAT_EXPECTED_ACCOUNT_ID`. A private
temporary profile runs
ChatGPT's turn preparation in a background Chrome process and an owned background
tab. On macOS the controller launches Chrome through Launch Services without
activation, then connects to its fresh private profile on an ephemeral
`127.0.0.1` CDP port. That local debugging port remains exposed to local
processes while the controller runs; keep the machine and process access trusted.
Chrome is still required. The generation POST is intercepted before browser
dispatch and sent once through HTTPX; the browser then renders that response.
`subchat_capabilities` reports
`generation_transport=browser_prepared_httpx` and `browser_required=true`.
Codex and Claude can use separate temporary browser snapshots while sharing a
ledger. The ledger serializes distinct sends to the same conversation before
dispatch; a competing send returns `concurrent_send` with
`blocking_operation_id` and is not sent. Recover that operation before starting
the next one. If its outcome cannot be established from conversation history,
the original conversation stays blocked against automated sends. Start a new
Chat instead of resending the uncertain request. This prevents duplicate or
out-of-order turns when the provider's receipt is unavailable.
The installed Codex Plugin has passed live new-Chat, same-conversation
follow-up and saved-answer recovery checks on macOS. In sampled runs the
dedicated Chrome windows stayed offscreen and another app retained focus; a
temporary Dock icon may appear during execution. This does not guarantee the
same behavior on every macOS/Chrome combination or after provider changes.
Do not resend an operation whose outcome is uncertain; recover it by operation ID.

In Codex, a natural-language request to use a Subchat still needs explicit tool
discovery and selection. Inspect `subchat_capabilities` and `subchat_catalog`,
then choose the exact available UI `model` and `effort` labels and copy the
matching available `http_selection` for an HTTP-read send. For example, if the
current catalog offers GPT-6 Pro with the requested effort, select that exact
entry; if it is absent, report that it is unavailable instead of silently
substituting another model. Call `subchat_send` with a fresh request ID, then
`subchat_wait` or `subchat_recover` using that same operation ID until the saved
answer is final. A submission receipt alone is not a completed answer.

`subchat_capabilities` also returns `implementation_version` and
`implementation_runtime_id` for the Subchat server that answered this call.
Check these separately from the common engine version and runtime ID returned
by `computer_status`; an existing Plugin session may retain an older wheel.

`subchat_download_file` takes a saved completed operation ID and an exact
`sandbox:/mnt/data/...` link from its final answer. It rechecks the bound
account and answer, then returns file metadata and at most 512 KiB of base64
content. It writes no local file and does not upload into another Chat or Library.
An unknown link, changed account, or oversized file fails explicitly. The
source Chat's path alone does not give another Chat access to its sandbox.

`subchat_download_image` takes a saved operation ID. It finds one generated image
in the exact account, conversation and turn bound to that operation, then returns
its PNG, JPEG or WebP bytes as bounded base64 content. The result includes
`submission_state` and `final_answer_verified`; image availability alone does
not prove a final assistant answer. A live macOS MCP call retrieved a 1254 × 1254
PNG from a saved ChatGPT image turn while the operation was still `submitted`.
The direct MCP response is capped at 2 MiB of image bytes. When calling through
Anywhere Computer's plugin bridge, request `chunk_bytes` of at most 24 KiB and
advance `offset` to each returned `next_offset`; the bridge limits individual
tool results to 64 KiB of text. The image is rechecked against the saved Chat
on each chunk request.

The Plugin server uses an already logged-in, dedicated Chrome profile. By
default, the profile and ledger are the `subchat/chrome-login` and
`subchat/ledger` directories beneath Anywhere Computer's local state directory.
Set absolute `ANYWHERE_SUBCHAT_CHROME_LOGIN_PROFILE` and
`ANYWHERE_SUBCHAT_STATE_DIR` paths in the Plugin process to override them. Log
in to the dedicated profile in a separate Chrome session, then close that
session before starting the server. Do not point the dedicated-profile setting
at a normal profile that is open elsewhere. On macOS, set the separate
`ANYWHERE_SUBCHAT_CHROME_SOURCE_PROFILE` variable to an explicit ordinary Chrome
profile directory, such as `~/Library/Application Support/Google/Chrome/Default`,
to use its existing login. To retain the choice across Plugin updates, create
`login-selection.json` beside the selected Subchat ledger directory (by default,
`subchat/login-selection.json` under Anywhere Computer's local state directory)
with `{"chrome_source_profile":"/absolute/path/to/Chrome/Default",
"expected_account_id":"your-Chat-account-ID"}`. The account ID is optional,
but pinning it prevents a different logged-in account from being accepted.
On macOS, `anywhere-subchat-setup inspect '/absolute/path/to/Chrome/Default'`
observes the account ID through a private profile snapshot. It leaves the source
profile unchanged and requests a background Chrome launch with no startup window.
From a source checkout, run
`uv run --locked --extra browser anywhere-subchat-setup inspect '/absolute/path/to/Chrome/Default'`.
For an installed Codex or Claude Plugin, run the setup entry point from the
Plugin's bundled wheel and requirements, using the absolute path to the
installed Plugin directory:

```sh
PLUGIN_ROOT=/absolute/path/to/installed/anywhere-computer
WHEEL=$(find "$PLUGIN_ROOT/bundled" -maxdepth 1 -name 'anywhere_computer-*.whl' -print -quit)
uv tool run --python 3.12 --from "$WHEEL" \
  --with-requirements "$PLUGIN_ROOT/bundled/dependencies.txt" \
  anywhere-subchat-setup inspect '/absolute/path/to/Chrome/Default'
```

Replace `inspect` with `select` and add the options below after reviewing the
account ID. A base wheel installation needs its optional `browser` extra for
inspection; the Plugin's bundled requirements already include Playwright.
After checking the ID,
`anywhere-subchat-setup select '/absolute/path/to/Chrome/Default' --expect-account-id ID`
saves the verified profile and account pin in a mode-0600 selection file. Selection
keeps the Plugin read-only. Add `--enable-background-send` to `select` only when
you explicitly intend to enable background browser-prepared sending. Restart the
Plugin after selection. The setup command prints no cookies, tokens, or email.
`ANYWHERE_SUBCHAT_CHROME_SOURCE_PROFILE` and
`ANYWHERE_SUBCHAT_EXPECTED_ACCOUNT_ID` override their respective file values.
Neither setting contains cookies or tokens. In read-only HTTP mode, the Plugin
takes a private temporary snapshot of ChatGPT cookies and opens only that
snapshot headlessly. In macOS background HTTPX mode, it uses a private profile
snapshot for browser preparation. The ordinary Chrome window remains open and
is not activated in the observed run. Source-profile selection applies to these
two modes; `browser-send` continues to use its separate dedicated profile and
can still briefly take focus. Check the account
reported by `subchat_capabilities` before recovering account-bound data. If
login is missing or denied, or the selected profile is missing or locked, the
server still exposes capabilities and saved operations. A missing or locked
profile reports `authentication_required`; an HTTP 403 reports `access_denied`.
If the observed account differs from a configured pin, capabilities and HTTP
reads report `account_mismatch` without exposing the observed account identity.
The pin remains in force for `subchat_refresh_auth`; select the intended account
before refreshing.
Restore access, then call `subchat_refresh_auth` or restart that Plugin session.
The main Anywhere Computer server is independent.

If Google sign-in rejects a browser controlled by test automation, use the
ordinary Chrome process shown below for this one-time interactive login. Do
not retry sign-in in an automation-controlled Chrome window. Close the ordinary
Chrome process after login so the Plugin can open the profile without a lock.

For the default macOS profile, stop the Subchat MCP server, open the profile
once to log in, and close that Chrome process before reconnecting:

```sh
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --user-data-dir="$HOME/Library/Application Support/Anywhere Computer/subchat/chrome-login" \
  https://chatgpt.com
```

On Windows and Linux, use the OS state directory reported by Anywhere
Computer's `state_directory()` rule or set the absolute overrides explicitly.
The Plugin server's catalog and history reads use the selected browser login;
they are not a credential renewal service.

The Plugin reads only its selected ledger. To inspect operations created by a
separate generation controller, use the same absolute ledger directory for
that controller's `--state-dir` and the Plugin's
`ANYWHERE_SUBCHAT_STATE_DIR` (or its default). The operation ID and logged-in
account must also match. The Plugin does not search or import another ledger.
For example, on macOS, the default Plugin ledger can be shared with a
separately configured controller as follows:

```sh
anywhere-subchat --browser-profile /absolute/path/to/dedicated-chat-profile \
  --state-dir "$HOME/Library/Application Support/Anywhere Computer/subchat/ledger"
```

This browser-assisted controller can open a Chrome window. Its HTTP-only mode
uses the same `--state-dir` option but requires its separate generation handoff
for sends. Check the Plugin's `subchat_list` after sending to verify the shared
ledger before attempting recovery.

## Local generation controller

For explicitly configured generation experiments, install the optional
`browser` extra and Chrome. Start `anywhere-subchat` with `--browser-profile`
pointing to a dedicated, logged-in profile and `--state-dir` pointing to its
local ledger. The controller reads one JSON object per stdin line and writes
one JSON result per stdout line. Keep stdin open between commands to retain
the same controller. Add `--mcp` to use the same local controller over stdio
MCP instead of JSON lines. Do not expose the stdio endpoint as an unauthenticated
network service.

Obtain available model and effort labels from the current account before
sending. The packaged catalog probe is:

```sh
python -m anywhere_computer.subchat_browser.catalog --profile PATH --headed
```

Close the probe before using the same profile in the controller. A `send`
requires a caller-generated 32-character lowercase hexadecimal
`operation_id`, exact `prompt`, and observed `model` and `effort` labels.
Optional `conversation_id` targets a follow-up. The MCP `subchat_send` uses a
32-character lowercase hexadecimal `request_id`; use that send ID as the
`operation_id` for recovery. A successful send can still be pending.

Use `status` or `recover` with the original operation ID after an uncertain
response. Poll while the operation is `sending` or `submitted`. Do not send
again with a new ID when an acknowledgement or final answer is missing.
`subchat_list` shows saved submission summaries; it is saved ledger state,
not a fresh provider observation. Access errors and unknown outcomes need
explicit diagnosis. The controller never treats them as permission to replay.

The `browser-send` controller may create or focus a headed Chrome window.
`--minimized` does not guarantee uninterrupted fullscreen viewing. The macOS
background HTTPX path has the narrower live observation described above. Neither
path grants child-specific file permissions or uploads local files into a Chat.
Attachments refer to files already uploaded to the selected account; a local
path is not an attachment ID. For shared work, identify the intended
device and expected content hash, then verify the child's result before using
it.

## HTTP paths and scope

`--http-read` supports authenticated, saved-answer recovery and model reads
through an observed browser session. It does not independently log in or send.
An HTTP rejection does not trigger automatic browser fallback or generation
replay. The separate opt-in HTTP-only generation mode has not passed live
generation acceptance. It requires a complete observed request handoff, does
not acquire generation protection values automatically and does not renew an
expired login. Start read-only and check the reported capabilities before
enabling generation.

For this opt-in CLI mode, supply `--http-only --state-dir PATH` and exactly one
session source: `--http-session-stdin`, `--chrome-login-profile PATH`, or (on
macOS) `--chrome-login-source-profile PATH`. Sending also requires
`--http-generation-stdin`; a Chrome login source additionally requires
`--expected-account-id ID`. The generation handoff has four fields:
`headers`, `sentinel_p`, `prepare_template` and `generation_template`. The
controller checks the selected account against the authenticated read session
before dispatch. Pass observed session and handoff data through a trusted
anonymous stdin pipe. Keep credentials and protection values out of command
arguments, files, logs, MCP calls and shell history. The controller does not
refresh those values or retry an uncertain send.

Personal ordinary-Chat HTTP access must stay within the permission and account
scope granted to the owner. The experimental transport is not an official API;
provider behavior can change. A completed operation is established by its
correlated saved receipt and final answer, not by a successful submission call
alone. New-Chat creation still requires a confirmed conversation identity;
when it is unknown after a crash, recover the original operation rather than
searching broadly or resending.
