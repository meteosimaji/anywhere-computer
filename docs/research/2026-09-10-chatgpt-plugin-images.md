# ChatGPT plugin bridge: bounded images and diagnosable preflight

Date: 2026-09-10. Base revision: `21b2e97`.
Work branch: `codex/chatgpt-plugin-bridge`, in a separate worktree.

## Changes

- Return each inspected tool's exact `call_arguments` together with its schema.
  The call input schema, descriptor digest algorithm and authorization scopes are unchanged.
- On `catalog_stale`, record the received/current descriptor digests and normalized
  workspace/server/tool in `details`. Do not dispatch or automatically retry.
- Attempt ephemeral-thread unsubscribe on preflight rejection as well as completed
  or uncertain dispatch. Cleanup failure must not replace the original result.
- Retain at most four inline PNG/JPEG/WebP/GIF images, sharing a 2 MiB decoded-byte
  budget. Check canonical Base64, MIME type and magic bytes; this is not a full
  compressed-image decoder. Unsupported formats/limits are explicit omissions.
- Project images into native MCP ImageContent, not Base64 in text or duplicated
  structured data. The ledger keeps the original bytes; the text envelope reports
  the MIME type, byte count and SHA-256. Child error propagation is preserved.
- Continue inspecting images after the text byte budget is exhausted.

No new runtime dependencies, global configuration edits, authorization expansion,
GUI-control API, persistent plugin sessions or model-turn delegation were introduced.

## Tests performed from the actual ChatGPT connection

The running pre-patch deployment returned a successful result for
`openaiDeveloperDocs.list_openai_docs({"limit":1})`.
External operation ID: `c7866d148b4e462f86cbd9653dff30cf`.
A second `operations_get` call using that same external ID confirmed completion.

The exact Google Calendar palette descriptor was then inspected and used through
`codex_plugin_call`. The running deployment returned `catalog_stale` with
`dispatched=false` (external ID `abea29ce2ccb4e0f9b137d4ce3a1adab`). Two separate
local inspections yielded identical descriptors. The original mismatch cause is
not established; this patch adds evidence for the next mismatch rather than
weakening the stale-catalog guard or claiming to have fixed an unproven cause.

The installed `chatgpt-apps` skill was selected and read through
`codex_skills_list` / `codex_skill_read`. That demonstrates reference retrieval,
not availability of every runtime described by a skill.

## Operation identity clarification

`RemoteAgent.internal_id` deliberately derives the ledger key from both the peer
identity and the external operation ID. The HTTP reply restores the external ID.
A literal comparison of ChatGPT's operation ID against SQLite's primary key is
therefore invalid. Use `operations_get` with the external ID on the same grant.
Different IDs alone are not evidence that the model invented an operation.
The authorization or grant database was not modified to investigate this.

## Actual App Server and MCP SDK integration

`scripts/verify_plugin_images.py` creates an isolated CODEX_HOME with a synthetic
image-returning MCP server. The official MCP SDK calls Anywhere Computer over
stdio; its engine invokes the installed Codex App Server, which invokes the
fixture. Assertions cover native image content, no Base64 duplication in model
text, ledger recovery, and wrong-digest rejection without another dispatch.

The optional `--installed` case also exercises the user's registered tools.
The captured result in `2026-09-10-plugin-images-live.json` shows:

- Image fixture and stale guard: passed.
- `openaiDeveloperDocs.list_openai_docs`: bridge and downstream tool succeeded.
- `codex_apps` / `google_calendar.get_colors`: descriptor validation and dispatch
  succeeded, but the downstream tool returned `FORBIDDEN` /
  `ACCESS_TOKEN_SCOPE_INSUFFICIENT`. This is not counted as a successful Calendar
  operation. No reauthentication or scope changes were performed.

The live verifier records method names actually sent by `_Session._send`.
It observed initialize, initialized, ephemeral thread/start, catalog listing,
MCP tool call and unsubscribe. No turn/start, turn/steer or thread/resume was sent.
This says nothing about separate third-party services' billing or usage limits.

The installed-case verifier intentionally exits nonzero if a downstream tool
fails while still printing its structured diagnostic report. The first run stopped
at the Calendar permission error; the improved reporter retains earlier results.

## Regression and distribution checks

Seven newly written targeted assertions failed before the source patch and passed
after it. The expanded focused set passed 71 tests, and strict mypy passed for all
59 source files. Ruff passed after line wrapping in the new verifier.

The first full run had 646 passes, six skips, and one expected distribution-parity
failure because the checked-in wheel had not yet been rebuilt to include the new
module. The wheel and checksum manifest were then regenerated using the existing
packaging script. Final verification is recorded below after the rebuilt run.

## Preserved user work and remaining scope

The original `codex/initial-engine` worktree is not used for patch writes.
Its pre-existing modified `scripts/verify_codex_plugins.py` and untracked
`tests/test_codex_plugin_probe.py` are not included in this change.

This work does not establish native desktop Computer Use, persistent browser
state across plugin calls, another plugin's interactive widget transfer, or
ChatGPT-side native image rendering until tested through the deployed connection.
The launchd upgrade timing issue reported in the handoff has not been changed.
