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
sending at Plugin startup. Windows has a separate dedicated browser selection
below. Without an explicit selection, the default
`generation_transport=unavailable` cannot create or send a Chat. Set
`ANYWHERE_SUBCHAT_PLUGIN_TRANSPORT=http-read-only` to keep a pinned macOS
installation read-only. Plugin installation does not configure generation for
the separate CLI.

### Windows dedicated Chrome or Edge profile

Windows can use a dedicated persistent Chrome or Microsoft Edge profile for
browser-prepared sending. This does not import a normal browser profile or an
existing Edge tab. Keep the Plugin stopped while preparing the profile, and run
one of these commands in an interactive Windows terminal:

If you are working from a repository checkout, prefix the command with
`uv run --locked`. For a Plugin-only installation, prefix it with
`uv tool run --from <wheel-path>`, using the absolute path of the Plugin's
bundled wheel for `<wheel-path>`.

```powershell
anywhere-subchat-setup prepare-dedicated --browser-channel msedge
# Or: anywhere-subchat-setup prepare-dedicated --browser-channel chrome
```

The command uses Windows Shell application registration to open the installed,
normal Edge or Chrome at `https://chatgpt.com/` with the Plugin's dedicated
profile. It does not use Playwright for interactive login or assume a browser
installation path. Sign in in that window, close every window using this
dedicated profile, then press Enter in the terminal and record the returned
`account_id`. The command verifies the saved login with the installed headed
browser placed outside the desktop and authenticated GET requests. It does not
enable sending. To recheck an existing dedicated login without opening an
on-screen window, use
`anywhere-subchat-setup inspect-dedicated --browser-channel msedge` (or
`chrome`). Close other processes using that dedicated profile first.

Save the verified browser, account ID, and explicit send consent:

```powershell
anywhere-subchat-setup choose-dedicated --browser-channel msedge `
  --expect-account-id ACCOUNT_ID_FROM_SETUP --enable-background-send
```

The command rechecks the dedicated login and writes a private local selection
with the browser channel, dedicated profile path, account ID, and send consent;
it stores no credentials. Restart the Plugin MCP session; a separately
launched Codex or Claude app reads this selection without inheriting terminal
environment variables. Omitting `--enable-background-send` saves the browser
and account for read-only use. `anywhere-subchat-setup revoke` removes the
selection for the next Plugin start. On Windows this also prevents the Plugin
from opening the unselected login, but does not delete the local browser
profile or its cookies. Conflicting browser, account, source
profile, or send-profile environment overrides are rejected. On Windows the
Plugin also rejects custom profile and state paths, so browser cookies remain
under the current user's LocalAppData directory. An explicit transport
override cannot bypass the saved send consent. Check
`subchat_capabilities` for
`generation_transport=browser_prepared_httpx`, then inspect
`subchat_catalog` and use exact available model, effort and HTTP selection
values. The controller verifies the pinned account before generation and
recovers the final answer from history using the original operation ID.
Windows setup inspection, read-only authentication, and browser-prepared HTTPX
sending launch the selected installed browser headed at an offscreen position.
The headless Edge path returned 403 for Chat home and authentication on a
Windows ARM64 VM with a signed-in dedicated profile; the headed offscreen
browser returned 200 for home, authentication, and catalog in a separate
manual probe. Whether a launch briefly takes focus from another app remains
unverified. The setup login window is intentionally visible. Nonactivating
background tabs are implemented only on macOS. An existing ordinary Edge login
is not silently copied, and Windows live send/follow-up acceptance must be
checked on the selected machine before claiming it works there.

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

In Codex, the model may select Subchat when the user asks a separate ordinary
ChatGPT Chat to help, even without saying "Subchat". This requires discovery
of the actual tools and a grant that permits the intended action; a discovery
match does not authorize sending. ChatGPT Work tasks are a different surface.
Inspect `subchat_capabilities` and `subchat_catalog`. For an HTTP-read send,
choose an available `source=http` choice, set `model` to its exact `model_title`
and `effort` to its exact `title`, and copy its `http_selection` unchanged.
Version `label` and `selected_display_version` are not the `model` field. For a
transport without HTTP selection, use the exact `source=ui` picker labels. Do
not replace an HTTP choice's `model_title` with a differing UI label. For
example, if the current catalog offers GPT-6 Pro with the requested effort, select that exact
entry; if it is absent, report that it is unavailable instead of silently
substituting another model. For a direct ChatGPT HTTPS send, choose a stable
`intent_key` for each intended child Chat and a fresh transport request ID.
The returned `submission_operation_id` identifies the saved send; it may differ
from the transport ID when the same intent is called again. Use that saved ID
with `subchat_wait` or `subchat_recover` until the answer is final. If a host
blocks or loses the reply, inspect `subchat_list` and saved status first; the
missing reply does not prove that no Chat was created. A submission receipt
alone is not a completed answer.

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
to use its existing login. Only a profile under the current user's standard
Chrome store or an app-managed staged snapshot is accepted. To retain the choice across Plugin updates, create
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
For ordinary macOS Chrome, use the profile-ID workflow to avoid entering a path:

```sh
anywhere-subchat-setup discover
anywhere-subchat-setup choose 'Profile 2' --expect-account-id ID_FROM_DISCOVER \
  --enable-background-send
```

`discover` inspects at most 20 `Default` or `Profile N` directories under the
current macOS user's standard Chrome profile store. It reports the Chat account
ID or an availability state for each; it does not read another directory chosen
by the caller. `choose` inspects the chosen profile again, requires the observed
account ID to match, and stores only the profile ID, account pin and send choice.
Run these commands as the macOS user who owns Chrome and the Plugin state. To
remove this saved choice, run `anywhere-subchat-setup revoke` and restart the
Plugin. Environment overrides and an explicit transport setting are separate
operator settings; remove those as well if they were configured. Existing
path-based `inspect`, `select` and `stage` remain available for older setups and
the stopped HTTPS service staging flow.
`ANYWHERE_SUBCHAT_CHROME_SOURCE_PROFILE` overrides a legacy path selection,
but cannot override a saved profile-ID selection. `ANYWHERE_SUBCHAT_EXPECTED_ACCOUNT_ID`
overrides the file's account pin.
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
`operation_id` for recovery on the legacy local path. The direct HTTPS path
also requires a stable `intent_key` and returns a canonical
`submission_operation_id` for recovery. A successful send can still be pending.

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
and accepts a source path only under the current user's standard Chrome store
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
