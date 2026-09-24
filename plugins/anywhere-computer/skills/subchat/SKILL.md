---
name: subchat
description: Use the Anywhere Subchat MCP to inspect saved ChatGPT conversations or send and recover an ordinary Chat message when the selected transport permits it.
---

Use the `anywhere-subchat` MCP server for ordinary ChatGPT conversations. Check
`subchat_capabilities` before choosing a workflow. Its read-only mode can inspect
saved submissions and authenticated history but cannot send. A send-capable mode
still requires browser preparation; it is not independent HTTP-only generation.
Do not infer provider authorization from a successful local call.

For a new send, inspect `subchat_catalog` with `source=ui` for the exact `model`
label and effort label shown in the Chat picker. In a send-capable HTTP mode,
also inspect `source=http` and copy the matching choice's `http_selection`,
including an explicit null `thinking_effort`. The HTTP catalog's
`selected_display_version` can differ from the UI model label: for example,
`5.6` and `GPT-5.6 Sol` are distinct fields. Do not substitute another model
or effort. Save a fresh 32-character lowercase hex
request ID before `subchat_send`. If its result is pending or unknown, retain
that ID and use `subchat_recover` or `subchat_wait`; never replay an uncertain
send with a new ID.

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
