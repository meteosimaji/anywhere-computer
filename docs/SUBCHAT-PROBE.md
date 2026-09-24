# Subchat usage and limits

`subchat` works with an ordinary ChatGPT Chat used for a delegated task. It is
experimental and depends on the selected account, observed model catalog and
transport. It is separate from a Codex task, ChatGPT Work task, the Anywhere
Computer engine and the public OpenAI API.

## Codex Plugin: Subchat server

The Codex Plugin registers `anywhere-subchat` beside `anywhere-computer`. Its
installation and `uv` prerequisite are described in the [README](../README.md#get-started).
After installation, restart the Plugin session and confirm both MCP servers are
available before using the Subchat tools. The Subchat server exposes seven
read-only tools by default: `subchat_capabilities`,
`subchat_catalog`, `subchat_list`, `subchat_status`, `subchat_recover` and
`subchat_wait`, plus `subchat_download_file`. A selected Chrome login profile adds
`subchat_refresh_auth`, which reacquires the same account through a headless
snapshot and authenticated GETs. A 401 on an authenticated read also triggers one
refresh and one repeat of that GET. Check `subchat_capabilities` first. The default
`generation_transport=unavailable` means this server cannot create or send a
Chat. Plugin installation does not configure generation for the separate CLI.

An older manual `codex mcp` registration with the same server name can shadow
the installed Plugin and keep pointing to a removed wheel. Check `codex mcp
list` when a newly installed Plugin reports `runtime_status=failed` and zero
tools. If `anywhere-computer` or `anywhere-subchat` is a stale manual entry,
remove that entry with `codex mcp remove NAME` and let the installed Plugin
provide the server. Keep the Plugin enabled and reopen its MCP session before
checking `subchat_capabilities` again. Plugin updates do not rewrite unrelated
manual MCP registrations.

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
In a 2026-09-24 live macOS run observed at 30 frames per second, a new Chat and
follow-up to that same conversation reached `completed` with the exact saved
answers `背景送信一回目成功` and `背景送信二回目成功`. Chrome did not come to the foreground.
HTTPX 200 for those
generation requests is inferred from the implementation's response handling
and completed results; a prior run measured HTTPX 200 directly. This observation
does not guarantee background behavior for every macOS/Chrome combination or
long-term provider compatibility. Other models, queued follow-ups, long
responses and installed Plugin delivery remain separate acceptance checks. Do
not resend an operation whose outcome is uncertain; recover it by operation ID.

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
replay. The [HTTP-only generation guide](SUBCHAT-HTTP-ONLY-GENERATION.md)
describes a separate opt-in mode using a complete, observed request handoff.
That mode has explicit account matching, does not acquire generation protection
values automatically and does not renew an expired login. Start read-only and
check the reported capabilities before enabling generation.

Personal ordinary-Chat HTTP access must stay within the permission and account
scope granted to the owner. The experimental transport is not an official API;
provider behavior can change. A completed operation is established by its
correlated saved receipt and final answer, not by a successful submission call
alone. New-Chat creation still requires a confirmed conversation identity;
when it is unknown after a crash, recover the original operation rather than
searching broadly or resending.
