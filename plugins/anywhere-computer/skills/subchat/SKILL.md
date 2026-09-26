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

If calling this server through ChatGPT's `codex_plugin_call` bridge, first open
`codex_plugin_session_open` and pass its `session_id` to tool inspection and
every stateful Subchat call. Keep the session open while sending, waiting, or
watching a queue, then close it after the result is recovered. A temporary
bridge context rejects stateful Subchat calls before dispatch. A long running
send or queue watch keeps the explicit session alive during idle periods;
`subchat_activity` reports live work without opening Chrome.

For a new HTTP-read send, inspect `subchat_catalog` with `source=http` and
choose an available choice. Set `model` to that choice's exact `model_title`,
`effort` to its exact `title`, and copy its `http_selection` unchanged, including
an explicit null `thinking_effort`. The version `label` and
`selected_display_version` are not the `model` field. If the selected transport
does not use HTTP selection, inspect `source=ui` for the exact picker model and
effort labels. Do not use a differing UI label as the HTTP send's `model`.
Do not substitute another model or effort. Save a fresh 32-character lowercase
hex request ID and one stable 32-character lowercase hex `intent_key` for each
intended child Chat before direct `subchat_send`. A returned
`submission_operation_id` is the ID for `subchat_recover` or `subchat_wait`;
it can differ from a later transport request ID for the same intent. If a
result is missing, blocked, or unknown, inspect `subchat_list` and the saved
status before another send. Never invent a new intent key for the same child.

Treat `queued` as local acceptance, `sending` as unconfirmed dispatch, and
`submitted` as a receipt. `completed` confirms a verified final turn; saved
text is present only for a text-bearing answer. For an image-only result, check
the saved answer type and use the optional `subchat_download_image` tool to
inspect the image bytes. `subchat_wait` stops after at most ten seconds and does
not stop Chat's generation. Its `elapsed_ms` is local call duration, and
`suggested_poll_interval_ms` is a delay before the next observation, not an
answer ETA. An interrupted reply requires inspecting the conversation before
any follow-up.

Use `subchat_list` to find saved operation IDs, then `subchat_status` for a
specific record. Saved prompts and answers can contain private data: request
only the records needed for the user's task, and do not copy their contents to
other tools or messages without a reason grounded in that task. The selected
local ledger and owner determine visibility; an unknown operation in another
ledger or owner is not evidence it was never sent.

For a follow-up, `subchat_message` with `mode=queue` stores input until its
confirmed predecessor is complete; recover the queued operation to dispatch
it. `mode=steer` is unsupported. Check the actual tool catalog before relying
on optional delete, download, queue watch, or authentication tools. Deletion
changes provider visibility and an unknown deletion outcome must not be
repeated automatically.
