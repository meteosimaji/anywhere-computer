# Subchat backlog audit

This audit maps issue [#185](https://github.com/meteosimaji/anywhere-computer/issues/185)
to source contracts at the b31 baseline and the subsequent batch-file change.
It distinguishes implemented behavior from live acceptance. Historical entries
in `OPEN-ISSUE-PLAN.md` describe the version and environment of each observation;
they are not automatically acceptance of the next release.

| Requested improvement | Source contract and verification | Remaining acceptance or implementation |
| --- | --- | --- |
| Saved discovery | `SubchatSubmissions.list` scopes records to their owner, omits prompts by default, optionally returns 160 characters, and preserves absent legacy creation timestamps. `test_subchat_state.py` and `test_subchat_mcp.py` cover it. | Reconfirm the packaged catalog and selected account after release. |
| One model choice | HTTP catalog `choice_id` resolves the exact model, effort and HTTP selection. Model/version mismatches fail before dispatch. | A catalog entry still needs an available UI preparation row; no listed model is automatically a successful live send. |
| Named accounts and doctor | `subchat_setup.py` saves named selections, checks the expected account on switching and supplies a no-send doctor. | Selected Chrome-profile inspection and these account setup paths remain macOS-specific. A second physical account is needed to accept a real switch. |
| Local files and Library | Single-file upload has exact path/byte/account preparation, durable stage claims and ready attachment descriptors. New `subchat_upload_library_batch` validates all unsent files first and shares one browser session. `subchat_upload_batch_status` only reconciles saved IDs. A real source two-file batch and one Chat answer reading both files passed on macOS; see the receipt below. | Repeat acceptance for the newly installed package. The batch returns resources for an explicit subsequent Chat send; it does not silently create a Chat. |
| Image selection, file ranges and direct save | `subchat_http_image.py`, `subchat_http_download.py`, `subchat_device_save.py` bind downloads to verified final answers and direct saves to explicit device/path permissions. | Existing live local/loopback evidence does not verify a physically remote device. |
| Queued changes and reattachment | Local and HTTPS tools implement model/resource changes with revision checks and immutable mutation receipts. HTTPS tools require separate OAuth scopes; HTTP model changes use a fresh choice ID. | Browser preparation must still validate the changed choice and references before dispatch. |
| Stop, steer and interrupted recovery | Capabilities accurately return `provider_stop=false`, `native_steer=false`. Cancel affects only unsent local rows. Unknown sends retain their original operation ID; interrupted output is not marked completed and its queued children do not dispatch. | Provider stop/steer and a reviewed continuation in the same conversation are not implemented. No stable provider contract or fresh UI stop acceptance establishes them. Do not unlock unknown sends by editing ledger state. |
| HTTPS parity | Owner-scoped list/status/observe, image/file retrieval, local cancel, automatic queues, event cursors, model/resource changes and direct save have distinct scopes and gateway tests. | Ordinary Chat grant/client acceptance is separate from loopback HTTP tests. Optional local Library uploads are not exposed by the HTTPS gateway. |
| Windows/Linux background behavior | General dedicated-browser setup exists, while the private background Chrome launcher explicitly supports macOS. | Windows/Linux browser-prepared background send and focus behavior need implementation and real desktop acceptance; a successful CI browser test does not establish desktop focus. |
| Completion and previews | Owner-scoped durable queue event IDs, cursor recovery, logging notifications, optional macOS alerts and 512-character provisional previews exist. | A notification does not prove that a parent model wakes or consumes the result. |
| Useful parallel work | Stable intent keys/operation IDs prevent response-loss resends. The skill describes independent bounded assignments, explicit input access and continued parent work. | Requested/actual child count, overlapping work, elapsed time and provider usage need a common observable report before claiming an efficiency improvement over one Chat. |

## File handoff comparison and design

[ChatGPT Library](https://help.openai.com/en/articles/20001052-file-storage-and-library-in-chatgpt)
supports retaining and reusing files. The practical comparison for Subchat is the
number of user actions and setup sessions needed to attach several known files.
The previous single-file workflow repeated profile snapshot, browser startup and
account session setup for each file. The batch path shares those steps and returns
one exact `resources` object when all items are ready. This reduces setup work by
construction; actual elapsed-time improvement requires a same-file live measurement.

[Playwright's file input API](https://playwright.dev/python/docs/input#upload-files)
can supply files or memory buffers. Anywhere retains one observed upload sequence
per file so each create/PUT/processing receipt stays correlated to its own saved ID.
It provides the pinned bytes to that sequence, preserving protection against a
source path changing during browser preparation. The batch never derives provider
attachment IDs from file paths or names.

Both upload tools are additive writes to an external account. Their MCP annotations
therefore set `readOnlyHint=false`, `destructiveHint=false` and `openWorldHint=true`.
This follows the [MCP annotation definition](https://ts.sdk.modelcontextprotocol.io/v2/api/%40modelcontextprotocol/server/server/mcp.html)
for additive operations; it does not grant file access, suppress host review, or
change the exact local approval and durable dispatch checks.

## Batch acceptance recipe

1. Prepare two small synthetic files with different verification markers and
   canonical absolute paths. Keep both markers out of the later Chat prompt.
2. Save a JSON manifest with `files`, each containing a fresh lowercase 32-character
   `operation_id` and the exact `path`. Keep this file for recovery.
3. Run `anywhere-subchat-upload --batch /absolute/manifest.json --prepare` locally.
   Confirm `provider_dispatched=false`, the original IDs and bounded approval expiry.
4. Open a fresh Library MCP process for the candidate. Call
   `subchat_upload_library_batch` once using the manifest's `files` array. Record
   elapsed wall time and every returned stage; do not replace an unknown ID.
5. Use `subchat_upload_batch_status` on those exact IDs if necessary. A partial
   batch reports `dispatch_claimed` for each item. Starting a still-unsent item
   requires a subsequent explicit upload decision with its original ID and valid
   preparation; a claimed item can only be reconciled.
6. After both items are ready, copy the returned `resources` into one explicitly
   authorized Subchat send with a fresh intent key and one saved send ID. Ask the
   model to report each file's marker, without including the markers in the prompt.
7. Recover the same send ID to a confirmed receipt and completed final answer.
   Check both exact markers, two visible attachment cards when UI is available,
   and one ledger record per file/send. Repeat read-only status from a fresh process
   to confirm restart recovery without new provider uploads.

Use the published, newly installed package for the final package acceptance. Source
execution and deterministic transport fixtures establish different evidence.

## Source acceptance on 2026-09-29

A real stdio Library MCP process uploaded exactly two small synthetic text files
prepared under distinct stable IDs in the selected macOS account. Both reached
`ready` with `provider_receipt=confirmed`. The call and process cleanup took
26.083 seconds. A process observer saw one new background Chrome owner during
that call and none left afterward. A fresh Library MCP process recovered the same
two ready attachment descriptors in 0.177 seconds without starting a browser.

A fresh authenticated HTTP catalog offered `GPT-5.6 Sol` / `Instant` with version
`5.6`. One explicitly intended Subchat send attached the returned resources.
Its first response was `prepared/not_sent`; original-ID waiting observed
`sending/unconfirmed`, then `completed/confirmed`. The final answer contained both
distinct file markers, which were absent from the prompt. Send, waits and process
cleanup took 16.859 seconds. The selected model labels and the final reported
`gpt-5-6` slug with null effort are distinct observed settings.

The saved intent had exactly one row and one matching submission, against one
requested Chat slot. A fresh Subchat MCP process returned the same completed
answer in 0.177 seconds without opening a browser. All acceptance processes
exited and their background browser owners were gone. No unknown operation was
resent and no model was substituted. These timings are one observation, not a
benchmark or a measured speedup against direct composer attachment. Visible
attachment cards were not separately inspected. Private IDs, account bindings,
file markers and full results are retained only in a local acceptance artifact;
this receipt does not establish installed-package or remote-device acceptance.
