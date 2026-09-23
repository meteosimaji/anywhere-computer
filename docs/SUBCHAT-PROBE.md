# Subchat usage and limits

`subchat` works with an ordinary ChatGPT Chat used for a delegated task. It is
experimental and depends on the selected account, observed model catalog and
transport. It is separate from a Codex task, ChatGPT Work task, the Anywhere
Computer engine and the public OpenAI API.

## Codex Plugin: read-only Subchat server

The Codex Plugin registers `anywhere-subchat` beside `anywhere-computer`. Its
Subchat server exposes six read-only tools: `subchat_capabilities`,
`subchat_catalog`, `subchat_list`, `subchat_status`, `subchat_recover` and
`subchat_wait`. Check `subchat_capabilities` first. The default
`generation_transport=unavailable` means this server cannot create or send a
Chat. Plugin installation does not configure generation for the separate CLI.

The Plugin server uses an already logged-in, dedicated Chrome profile. By
default, the profile and ledger are the `subchat/chrome-login` and
`subchat/ledger` directories beneath Anywhere Computer's local state directory.
Set absolute `ANYWHERE_SUBCHAT_CHROME_LOGIN_PROFILE` and
`ANYWHERE_SUBCHAT_STATE_DIR` paths in the Plugin process to override them. Log
in to the dedicated profile in a separate Chrome session, then close that
session before starting the server. Do not use a normal profile that is open
elsewhere. A login failure leaves this Subchat server unavailable; the main
Anywhere Computer server is independent.

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

The browser-assisted controller may create or focus a headed Chrome window.
`--minimized` does not guarantee uninterrupted fullscreen viewing. It does
not grant child-specific file permissions or make local paths available inside
a Chat. Attachments refer to files already uploaded to the selected account;
a local path is not an attachment ID. For shared work, identify the intended
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
