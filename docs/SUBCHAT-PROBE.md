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
Windows setup inspection reads the account from an authenticated, same-origin
GET inside the selected installed browser. Read-only authentication and
browser-prepared HTTPX sending still use a separate HTTPX client. These Windows
paths launch the browser headed at an offscreen position. The setup login
window is intentionally visible. An existing ordinary Edge login is not
silently copied. After selection, verify an actual send and its saved final
answer on the intended Windows account; setup GET success alone does not prove
that the separate HTTPX generation request will work. A foreground sample can
detect visible focus changes, but it cannot exclude a shorter transition.

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
send, message, recover, wait, queue watch or durable queue arming. A one-call bridge context rejects
these operations before dispatch, including when the MCP server was registered
under a different name. Keep the session through final answer recovery and
close it explicitly. Idle cleanup consults `subchat_activity`; it preserves a
session while a send, recovery or queue worker is live, then allows normal idle
expiry after the work finishes. An explicit session close or Engine shutdown
still ends that local background work.

`subchat_queue_auto` opts one already saved `queued` follow-up into unattended
delivery for a bounded lease (30 seconds to 24 hours). Unlike the short-lived
`subchat_queue_watch`, it saves the opt-in in the owner-scoped ledger and
re-arms it when a send-capable controller restarts. It may reopen the selected
browser profile; the selected account is still checked before dispatch. The
worker recovers the predecessor, sends only after its final answer is saved,
then observes the child until completion or a stop condition. An operation in
`sending` is recovered from history, never sent again. Disabling the opt-in
prevents its worker from reserving a queued child for dispatch, including
while parent recovery is in progress. Once the child's durable state advances
to `sending`, disable cannot undo the provider request; recover that original
operation ID. Use `subchat_cancel` to cancel the queued child itself.

Terminal completion and failure reasons are saved for `subchat_queue_events`.
While the stdio session that armed the queue remains connected, it emits a
bounded MCP `notifications/message` event with the operation ID, outcome, and the same
durable `event_id` returned by the cursor-based event query. Deduplicate
notifications by `event_id` before starting parent follow-up work. This is a
transport notification; a host is not guaranteed to surface it in the parent
model turn. A different controller may resume the queue but does not receive
the arming session's notification. The durable event query is the recovery
path after the arming session closes or loses its notice.
Re-arming the same queued operation starts a new epoch. A session subscribed
to an older epoch does not receive the new epoch's outcome.
Automatic work cannot progress while the controller process is fully stopped;
the next send-capable session resumes an armed, unexpired queue. Expiry or an
observation error stops the worker and requires a fresh explicit arm. A parent
in `sending` can accept a queued child only after its exact conversation,
user-message and account identities have been checkpointed. The worker still
waits for a verified final parent answer before dispatch.
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
Newly saved operations include a creation timestamp; older rows retain a null
timestamp. Prompt text is omitted by default. A separate
`subchat_prompt_preview` OAuth scope is required to set
`include_prompt_preview=true` and read a bounded 160-character preview for the
selected owner. Existing `subchat_list` grants continue to return summaries only.
Publishing this tool does not grant it to an existing OAuth connection. The
owner must consent to its scope before an ordinary Chat can invoke it.
The HTTPS gateway also exposes read-only `subchat_queue_events` with its own
OAuth tool scope. It reads the selected account's owner-scoped durable event
history using `after_id`, `next_cursor`, and `has_more`, without opening Chrome
or dispatching a queued send. A grant for another Subchat tool does not include
this scope automatically. HTTPS polling can recover an event that a local stdio
notification did not reach, but it does not wake a parent Chat model turn by
itself. Older rows saved under another grant identity are not enumerated by a
new grant's event query. If the original operation ID is known, pass it as
`operation_id` to retrieve only that operation's events. A replacement grant
can use this form only when it belongs to the same OAuth principal and the
saved operation is bound to the selected Chat account.
The direct HTTPS gateway now also offers `subchat_queue_auto` under a separate
OAuth scope. Enable it only for a saved `queued` follow-up and retain that
operation ID. Its service-owned worker may continue after the HTTP connection
closes; a new connection can read `subchat_queue_events` for completion or
failure. Revoking the OAuth grant that armed a queue stops its worker before
the next queued send is reserved and records `authorization_lost`. A send
already in `sending` remains subject to recovery under its original operation
ID. A fully
stopped service cannot run the worker; its next send-capable gateway session
resumes an armed queue. The HTTPS tool does not wake a parent model turn.

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
For each HTTP choice, `available` and `availability_basis=authenticated_http_catalog`
report what the selected account's model catalog exposed. The separate
`generation_sendability=unknown` means no generation was attempted for that
choice. A visible choice can still fail UI preparation, quota checks or
generation; use the operation result and recovered history for send evidence.
In a send-capable local session, call `subchat_catalog` with `source=compare`
and no `model` to observe both the HTTP catalog and the current UI model menu
without submitting a message. Each HTTP choice reports `ui_picker_status` as
`selectable_row_observed`, `not_confirmed`, or `unknown`, plus the matched row
label when available. This checks only the model row; it does not verify the
effort control, quota, generation request, or final answer. The HTTPS gateway
and read-only Plugin mode continue to provide the HTTP catalog alone.

If UI navigation is blocked, `source=ui` reports `catalog_unavailable` with a
bounded `reason`, `failure_stage`, and the observed `http_status` when available.
`browser_challenge` means the response contained Cloudflare's
`cf-mitigated: challenge` header; an HTTP 403 without that header is
`navigation_failed`. A visible login button is `authentication_required`.
Navigation and picker timeouts have separate reasons. `source=compare` preserves
these fields as `ui_reason`, `ui_failure_stage`, and `ui_http_status`.
Send preparation persists the same reason while the operation remains unsent;
inspect its original operation ID instead of starting another send. These
diagnostics retain no response body, cookies, or authentication headers. A
successful HTTP catalog or a previous successful UI visit does not establish
that the next browser navigation will pass a challenge.

The HTTP catalog bootstrap checks the home navigation too: a challenge stops
before waiting for a model response that the challenge page cannot produce.
After a challenge, this controller refuses further UI catalog observations,
HTTP catalog bootstraps, and new send preparation without opening another tab.
UI results then report `new_session_required: true`. Already authenticated
history recovery remains available when it does not need browser bootstrap.
Inspect the login browser before explicitly starting a fresh session; repeatedly
opening new sessions is not a recovery strategy. This guard does not solve or
bypass provider challenges, and the provider can challenge a later navigation
even after a successful catalog read.

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
The new Chat needs a new operation ID and explicit user intent; the original
operation remains saved for later read-only recovery. Before moving work,
inspect any queued child with `subchat_status`. Cancel it while still unsent,
or disable its automatic queue watcher, so it cannot later dispatch into the
old conversation. A confirmed provider interruption is still not a completed
answer and does not make the old queued child safe to send.
The installed Codex Plugin has passed live new-Chat, same-conversation
follow-up and saved-answer recovery checks on macOS. In sampled runs the
dedicated Chrome windows stayed offscreen and another app retained focus; a
temporary Dock icon may appear during execution. This does not guarantee the
same behavior on every macOS/Chrome combination or after provider changes.
Do not resend an operation whose outcome is uncertain; recover it by operation ID.

An unsent queued follow-up can change its model through `subchat_queue_model_change` in
the local Subchat controller or the separately scoped HTTPS gateway. Read
`queue_revision` with `subchat_status`, then pass the
same operation ID and `expected_revision`. For an HTTP-selected queue pass a current
`choice_id` from the selected account's HTTP catalog; for a UI-selected queue pass
`model` and `effort`. The controller checks the HTTP choice on change and again during
send preparation. The HTTPS variant accepts only the HTTP `choice_id` and requires
its own OAuth scope. `subchat_queue_resources_change` likewise has a separate
HTTPS scope and replaces explicit attachment/plugin references under the same revision.
A stale revision or a `sending` checkpoint rejects the change, and
neither this tool nor same-ID status/recover sends a second copy. A running automatic
queue may reserve the input first; inspect status after any rejection.

In Codex, the model may select Subchat when the user asks a separate ordinary
ChatGPT Chat to help, even without saying "Subchat". This requires discovery
of the actual tools and a grant that permits the intended action; a discovery
match does not authorize sending. ChatGPT Work tasks are a different surface.
Inspect `subchat_capabilities` and `subchat_catalog`. For an HTTP-read send,
choose an available `source=http` choice, set `model` to its exact `model_title`
and `effort` to its exact `title`, and copy its `http_selection` unchanged.
Alternatively pass that choice's `choice_id` alone with the prompt; it binds
those three values and is rechecked against the current account catalog before
dispatch. A choice ID is not an authorization credential. The HTTP catalog's
`available` flag does not establish that the current browser picker can select
that model. The controller resolves the version against the current picker and
checks the intercepted generation model and effort before dispatch. If a model
moves out of `latest`, select a fresh catalog choice; an older `choice_id` may
be rejected. A missing or ambiguous UI row fails before send.
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
`provider_receipt` separates the transport call from the provider: `not_sent`
is a saved pre-dispatch state, `unconfirmed` means the outcome still needs
reconciliation, and `confirmed` means matching provider history shows the
user message. A generation HTTP 200 or candidate conversation ID remains
`unconfirmed` until that history check. `subchat_list` returns the canonical
`submission_operation_id` and this classification for each saved row, without
prompt text by default. `conversation_url` is populated only for a confirmed
UUID conversation. No child count is inferred from prompt wording; keep one
stable intent key per intended child and inspect saved rows before creating a
new key after a missing response or host block.
`subchat_observe` can reconcile a saved sending/submitted operation without
dispatching a queued follow-up. In a send-capable controller,
`subchat_recover` and `subchat_wait` may dispatch a queued follow-up once its
parent completes; their risk annotations reflect that effect.

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

`subchat_download_image` takes a saved operation ID. It finds generated images
in the exact account, conversation and turn bound to that operation. If there
is more than one, pass the zero-based `image_index`; the response includes
`image_count` and the selected index. It then returns
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
anywhere-subchat-setup doctor
```

`discover` inspects at most 20 `Default` or `Profile N` directories under the
current macOS user's standard Chrome profile store. It reports the Chat account
ID or an availability state for each; it does not read another directory chosen
by the caller. `choose` inspects the chosen profile again, requires the observed
account ID to match, and stores only the profile ID, account pin and send choice.
`doctor` reads the selected login and checks the account again without creating
a Chat message. It reports the configured tool groups, including whether the
current transport setting permits background send. `tool_catalog=not_observed`
means it has not inspected a running MCP session's actual tool list; use that
session's catalog to confirm registration.
To keep several macOS Chrome choices, save each selected profile under a local
name, then explicitly confirm the account before switching:

```sh
anywhere-subchat-setup save-named --name personal
anywhere-subchat-setup list-named
anywhere-subchat-setup use-named --name personal --expect-account-id ID_FROM_LIST
```

`save-named` requires a profile-ID selection made with `choose`. The separate
named list is private to the local user and contains profile IDs, account IDs,
and the saved send setting; it contains no login credentials. `use-named`
rechecks the currently observed Chat account before replacing the active
selection. Restart the Plugin after switching so the running controller opens
the newly selected account. This workflow does not switch an active generation
or move operations between account scopes. Windows dedicated-browser selections
are not supported by these named commands.
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

The local `anywhere-subchat-library` Plugin server exposes
`subchat_upload_library` and `subchat_upload_status` on macOS. The first tool
requires an explicit absolute local path and a caller-generated, stable
`request_id`. Before an MCP upload, the owner runs
`anywhere-subchat-upload /absolute/file --operation-id ID --prepare` in a local
terminal. Preparation reads and fingerprints the file, pins the selected
account and operation ID, and makes no browser or provider request. The MCP
authorization lasts 15 minutes and is separate from an ordinary CLI upload
reservation. The MCP tool requires that exact prepared file, account and ID;
it checks authorization again immediately before dispatch and uploads to ChatGPT
Library, not to a conversation. The status tool takes the original upload
operation ID and performs read-only reconciliation without the source file.
If the first call is interrupted or reports `unknown`, inspect that exact ID
before any new upload decision. The existing CLI `anywhere-subchat-upload`
uses the same ledger. Neither entry point turns a local path into an existing
Chat attachment automatically, and the Library item ID must be observed before
the caller attaches it to a Chat. A `ready` MCP result includes an
`attachment` object with the saved file ID, Library item ID, filename, MIME
type, and exact byte count for an explicit `subchat_send` resource. It is
`null` until ready; do not guess these fields from a local path or file name.
For several files, save a bounded JSON manifest, for example:

```json
{"files":[
  {"operation_id":"11111111111111111111111111111111","path":"/absolute/first.pdf"},
  {"operation_id":"22222222222222222222222222222222","path":"/absolute/second.pdf"}
]}
```

Use fresh IDs for an explicitly intended new upload, then prepare the exact
manifest locally with `anywhere-subchat-upload --batch /absolute/manifest.json --prepare`.
Pass the same `files` array to `subchat_upload_library_batch`. The tool verifies
all unsent paths, bytes, account bindings and approvals before opening a browser.
It shares one account/profile/browser session while retaining one durable
upload record per file. Batches are limited to ten files and 40 MiB total.
An uncertain item stops new uploads in the batch. The response includes each
saved ID and whether its provider dispatch was claimed; it returns grouped
`resources.attachments` only when every file is ready and has validated metadata.
`provider_receipt` distinguishes `not_sent`, `unconfirmed`, and `confirmed`;
a durable dispatch claim alone does not prove that the provider received bytes.
Use those resources in one separate explicit `subchat_send` call.

After a lost response, call `subchat_upload_batch_status` with the original
`operation_ids`. Status never reads source files or starts an upload. A later
explicit batch request using the same IDs can start still-prepared files;
claimed files are only reconciled, including after approval expiry or removal
of the source. Per-file `subchat_upload_status` remains available. A response
with `error_code` also identifies the affected `failed_upload_operation_id`;
`transport_unverified` never authorizes a new upload ID.

The local path alone and the Library upload alone do not
attach anything to that conversation. The current schema permits up to ten
attachments of at most 20 MiB each; check the selected account's actual
acceptance before relying on a particular file type.

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
