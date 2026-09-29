---
name: subchat
description: Use when the user wants a separate ordinary ChatGPT Chat to help with a task, or wants to inspect, send to, or recover a saved Chat conversation through Anywhere Subchat.
---

Use the `anywhere-subchat` MCP server for a requested separate ordinary ChatGPT
conversation, including requests that do not say "Subchat". A ChatGPT Work task
is a different destination. Tool choice follows the user's intent; a discovery
match alone does not authorize a send. Check `subchat_capabilities` before
choosing a workflow. Its read-only mode can inspect
saved submissions and authenticated history but cannot send. A send-capable mode
still requires browser preparation; it is not independent HTTP-only generation.
Do not infer provider authorization from a successful local call.

For parallel work, split only independent, bounded questions with a clear
answer format. Give each child the source material or access path it actually
needs; `work_context` records provenance and does not grant the child the
parent's files, tools, or conversation history. A child may lack the parent's
browser or Computer Use tools, so require it to name the tools it used and to
report access failure without guessing. Record one intent key and request ID
for each intended child, send each child once, then continue useful parent
work while the children run. Collect their saved operation IDs later and
verify their cited evidence before integrating an answer. Do not serially wait
for one child before starting the next independent child. Prefer the single
current Chat when the tasks share context heavily or the expected answer is
shorter than the setup and recovery work. No child-count limit is inferred
from the wording of a prompt; the operation ledger records actual sends.

If calling this server through ChatGPT's `codex_plugin_call` bridge, first open
`codex_plugin_session_open` and pass its `session_id` to tool inspection and
every stateful Subchat call. Keep the session open while sending, waiting, or
watching a queue, then close it after the result is recovered. A temporary
bridge context rejects stateful Subchat calls before dispatch. A long running
send or queue watch keeps the explicit session alive during idle periods;
`subchat_activity` reports live work without opening Chrome.

For an unattended follow-up, first queue it with `subchat_message`, then call
`subchat_queue_auto` for its saved operation ID. This opt-in is durable for a
bounded lease and can reopen the selected browser when a send-capable
controller restarts. Inspect `subchat_queue_events` for a saved completion or
failure; MCP logging notifications are also emitted while connected, but the
host may not relay them to this model turn. Keep the plugin session alive for
delivery; after a full process stop, a new send-capable session resumes an
armed, unexpired queue. Do not re-send a child whose state is `sending`.
Disabling `subchat_queue_auto` prevents its worker from starting an unsent
child, even if parent recovery is in progress. Once the child is `sending`,
disable cannot undo the provider request; recover its original operation ID.
Use `subchat_cancel` to cancel a child that remains `queued`.
On the direct HTTPS gateway, `subchat_queue_auto` and
`subchat_queue_events` each require their own OAuth scope. An armed queue can
continue after the HTTP client disconnects; inspect its durable event through
a fresh connection. A full service stop pauses work until a send-capable
gateway session resumes it. No notification guarantees a parent model turn.
On macOS, `notify_desktop=true` requests a local alert after a durable terminal
event; a missed alert is recovered through `subchat_queue_events`.
Revoking the OAuth grant that armed the HTTPS queue stops an unsent child and
records `authorization_lost`; recover any child already in `sending` by its
original operation ID.

For a new HTTP-read send, inspect `subchat_catalog` with `source=http` and
choose an available choice. Set `model` to that choice's exact `model_title`,
`effort` to its exact `title`, and copy its `http_selection` unchanged, including
an explicit null `thinking_effort`. The version `label` and
`selected_display_version` are not the `model` field. If the selected transport
does not use HTTP selection, inspect `source=ui` for the exact picker model and
effort labels. Do not use a differing UI label as the HTTP send's `model`.
On a version that exposes `choice_id`, it can supply those three HTTP fields
together. It is rechecked before dispatch and is not authorization. A model
listed as available by HTTP may still be unavailable in the current UI picker;
inspect an unsent preparation failure by the original operation ID.
For a send-capable local session, `source=compare` also checks whether the
current UI has a selectable model row for each HTTP choice without sending.
Its UI result and `available` are preparation evidence; only a saved generation
receipt and final history confirm a send.
Do not substitute another model or effort. Save a fresh 32-character lowercase
hex request ID and one stable 32-character lowercase hex `intent_key` for each
intended child Chat before direct `subchat_send`. A returned
`submission_operation_id` is the ID for `subchat_observe`, `subchat_recover`,
or `subchat_wait`;
it can differ from a later transport request ID for the same intent. If a
result is missing, blocked, or unknown, inspect `subchat_list` and the saved
status before another send. Never invent a new intent key for the same child.
There is no child count inferred from the wording of the request. Keep one
stable intent key for each child the user actually intends, and reconcile each
saved ID before deciding to create another.

Treat `queued` as local acceptance, `sending` as unconfirmed dispatch, and
`submitted` as a receipt. `completed` confirms a verified final turn.
`provider_receipt` is `not_sent`, `unconfirmed`, or `confirmed`. Only
`confirmed` means the provider's matching user message was observed. An HTTP
200, a conversation ID candidate, or a completed MCP call alone does not
confirm it. `conversation_url` appears only with a confirmed receipt and a
valid conversation ID. `subchat_list` returns the same receipt classification
and the canonical `submission_operation_id` without prompt text by default.
`send_worker` describes the local dispatch worker independently of that receipt.
A failed or finished worker is not proof that the provider did not receive input;
`not_owned` means the current controller has no worker for that saved operation.
Inspect `http_progress` and recover the original operation ID. Do not keep waiting
for a failed worker to resume, and do not replace an unconfirmed send with a new
ID or intent key. Only an explicit verified `not_sent` result establishes no send.
The saved receipt confirms delivery, not that the model finished answering;
saved text is present only for a text-bearing answer. For an image-only result, check
the saved answer type and use the optional `subchat_download_image` tool to
inspect the image bytes. For multiple images in one turn, pass the zero-based
`image_index` and verify the returned `image_count`. `subchat_wait` stops after
at most ten seconds and does
not stop Chat's generation. Its `elapsed_ms` is local call duration, and
`suggested_poll_interval_ms` is a delay before the next observation, not an
answer ETA. An interrupted reply requires inspecting the conversation before
any follow-up. `reply_output_limit` means the provider ended at its output cap;
the saved operation stays interrupted and its queued children remain unsent.
Use `subchat_observe` to reconcile a sending or submitted operation without
dispatching a queued follow-up. In send-capable sessions, `subchat_recover` and
`subchat_wait` may deliver a queued follow-up when its parent is complete.
When the catalog offers `subchat_preview`, call it only for a submitted input
whose provisional text is useful. It returns the latest 512 characters from
verified in-progress history, or no preview; it never replaces final recovery.

Use `subchat_list` to find saved operation IDs, then `subchat_status` for a
specific record. Saved prompts and answers can contain private data: request
only the records needed for the user's task, and do not copy their contents to
other tools or messages without a reason grounded in that task. The selected
local ledger and owner determine visibility; an unknown operation in another
ledger or owner is not evidence it was never sent.
`subchat_list` omits prompt text by default. Request
`include_prompt_preview=true` only when a bounded preview is needed.
The local macOS setup command can save several named Chrome profile selections.
Switching requires the expected account ID, a fresh observed account match, and
a Plugin restart. It does not move saved operations between account scopes.

For a follow-up, `subchat_message` with `mode=queue` stores input until its
confirmed predecessor is complete; recover the queued operation to dispatch
it. Pass `resources` explicitly to attach already-uploaded file references or
observed plugin references to that follow-up; the parent's resources are never
inherited. A local file path is still not an uploaded attachment ID. To change
the model before dispatch, read `queue_revision` from
`subchat_status` and call `subchat_queue_model_change` with that revision and
either a current HTTP `choice_id` or exact UI model and effort labels. A
revision conflict means another controller changed or reserved the queue;
inspect status before deciding what to do. The HTTPS gateway exposes model and
resource changes under separate OAuth scopes; its model change accepts only a
fresh HTTP `choice_id`. `mode=steer` is unsupported. Check the actual tool catalog before relying
on optional delete, download, queue watch, or authentication tools. Deletion
changes provider visibility and an unknown deletion outcome must not be
repeated automatically.

The direct HTTPS gateway may offer `subchat_save_file` under a separate OAuth
scope. It saves one verified final-answer sandbox file to a new absolute path
on the explicitly selected device without returning its bytes to the model.
Choose one stable `request_id`, keep it after a running or unknown result, and
inspect that same ID; do not create a second save for a lost response. The
source Chat account and destination device permissions are checked separately.
This tool is unavailable in the local stdio Subchat server.

For an explicitly requested local file upload on macOS, use the separate
`anywhere-subchat-library` MCP server when installed. Its
`subchat_upload_library` tool requires an absolute path and a fresh, stable
32-character `request_id`. The owner must first run
`anywhere-subchat-upload /absolute/file --operation-id ID --prepare` locally;
this authorizes that exact file and selected account for 15 minutes. A normal
upload reservation does not authorize MCP dispatch. The tool refuses an
unprepared, expired, or changed file before dispatch. It uploads
to the selected account's Library, not directly into a Chat. If its result is
missing or unknown, call
`subchat_upload_status` with the same upload operation ID; never invent a new
ID to retry an uncertain upload. A `ready` Library item ID may then be used
as an already-uploaded resource in a separate, explicit Chat send. Copy the
`attachment` object from the ready Library result into the Chat send's
`resources.attachments`; it carries the saved byte count and IDs. Do not
invent attachment metadata.

For several files, prefer `subchat_upload_library_batch` when available. Save
one operation ID per file in a JSON manifest with `files` entries containing
`operation_id` and absolute `path`. Locally run
`anywhere-subchat-upload --batch /absolute/manifest.json --prepare`, then pass
those same entries to the batch tool. It permits at most ten files and 40 MiB
total and shares one selected-account browser session. Every unsent file must
match its local approval before any upload begins. A partially completed batch
returns each file's saved ID and `dispatch_claimed`; it stops new uploads
after an uncertain file. Use `subchat_upload_batch_status` with the original
`operation_ids` after a missing response, without reading or sending bytes.
Only a fully ready batch returns a grouped `resources` object for a separate,
explicit Chat send. Retain every ID; a later explicit batch call can start
still-prepared files but only observes files already claimed for upload.
