# Verified Subchat file to a selected device

## Current boundary

`subchat_download_file` verifies an exact sandbox link in a saved completed final answer. Its backend checks the saved operation, Chat account, conversation, answer message, link, response range, and 16 MiB total limit. The current MCP result returns a bounded base64 chunk. It does not save to a device.

`AuthorizedDeviceMCP.session()` dispatches `subchat_*` to the selected account gateway and `devices_call` to `DeviceRouter`. The HTTP grant is rechecked per dispatch. A routed device uses its own saved SSH or HTTP authorization, which can change independently of the calling HTTP grant. `DeviceRouter` binds an operation ID to its exact device and arguments, marks it dispatched before forwarding, and returns `unknown` after response loss without replaying it. On the target, `upload_begin`, `upload_chunk`, `upload_status`, and `upload_commit` stage a hash-checked upload and publish only to an unused absolute path. `upload_commit` can itself have an unknown publication outcome.

These are sufficient primitives for a composite service operation, but calling them sequentially inside one transient HTTP request is unsafe. The caller could lose the response after a target chunk or commit and create a new operation that overwrites, duplicates, or incorrectly reports the saved file. The router's `devices_call` permission also does not imply that the selected device currently grants upload tools.

## Proposed operation

Add `subchat_save_file` as a new direct HTTPS tool, with an explicit OAuth scope separate from `subchat_download_file` and `devices_call`. Arguments: saved `operation_id`, exact `sandbox_link`, selected `device_id`, canonical absolute `destination_path`, and a client chosen `request_id`. Permit an unused destination only. Return metadata and a stable save ID, never file bytes or a download URL. Do not expose this tool on local stdio Subchat unless it gains the same device authorization boundary.

Put orchestration in a dedicated service module called from `AuthorizedDeviceMCP.session()`, before the ordinary Subchat and device dispatch branches. Keep the verified Chat download in `SubchatGateway`/`BrowserSubchatBackend`; do not duplicate provider URL handling in the router. Use `DeviceRouter` and the selected device's current authorized catalog for target operations. At each stage, recheck the current HTTP grant for `subchat_save_file`, its bound owner/device/client/resource and selected account, plus the current selected device route and required target `upload_*` tools. A different grant may resume only under the established same-principal and same-account rule; expiry or revocation must not be revived by historical ownership. A changed device registration or target credential must stop the operation pending explicit reconciliation.

Persist a composite record before any network or target write. Bind its stable ID to the source operation, exact link, selected account and ledger owner, destination device identity and registration fingerprint, absolute path, and request arguments digest. Never allow the ID to name a different destination or source. Record stage, target transfer ID, per-step routed operation IDs, total size, SHA-256, received offset, and final result. Lock each record across concurrent requests and restarts. A 16 MiB bounded temporary spool on the service host can hold the verified file while calculating its hash; keep it private and clean it only after a confirmed target result or explicit abort. The spool is not a user destination.

Stage the target upload using a random transfer ID durably saved with the composite record. Use chunks of at most 256 KiB and stable routed operation IDs for `upload_begin`, each `upload_chunk`, and `upload_commit`. After an uncertain target response, inspect `upload_status` and, if needed, `operations_get` on the same device with the exact prior ID. Continue only when the target's received prefix, size, digest, path, and state match the composite record. Do not create a new routed write ID merely because a response is missing. For an unknown commit, return `unknown` with the stable save ID and require status inspection; do not call commit again until target state establishes that publication has not started. If target reports `publishing` or staging ambiguity, keep the operation unknown for explicit resolution through existing `upload_resolve` semantics. Never report success based only on a completed network request; require the target's `publication_verified` state and matching path, size, and SHA-256.

The downloader must not return base64 to the MCP caller. Internal chunks may be base64 encoded only as required by the target upload transport. The composite response contains the selected device ID, destination path, byte count, SHA-256, transfer/save IDs, and verification state. Error replies must omit credentials, file content, provider signed URLs, and the private spool path.

## Acceptance tests

1. Grant filtering: the tool is absent without its own scope; a grant with only `subchat_download_file` or only `devices_call` cannot invoke it. Revocation during a long transfer stops the next stage. A reconsented same-principal grant can inspect the same record only for the same selected account.
2. Source isolation: another owner's operation, a different Chat account, an unverified final answer, a different sandbox link, and an oversized file never start a target upload.
3. Target isolation: an unregistered device, changed device registration, missing current target upload permission, invalid path, existing destination, and mismatched operation ID all fail before publication. The selected device receives only its own bytes.
4. Successful transfer: use the verified provider download fixture and real target `Uploads` implementation through the router. Confirm an exact SHA-256 match and exclusive target publication. Confirm the MCP response has no `content_base64`, signed URL, or spool path.
5. Fault injection after each durable checkpoint and target dispatch: restart the service, resume by the same save ID, and assert no duplicate source write or target publication. A lost `upload_commit` reply must remain unknown until target status proves `publication_verified`; no automatic replay with a fresh operation ID.
6. Real transport acceptance: use a selected Chat account and an independently authorized target device, with an unused destination path. Verify the target file bytes and metadata on that device. Record provider and target transport observations separately from local unit tests.

## Implementation status

`subchat_device_save.py` contains the durable runner and journal. The journal binds principal, selected account, source operation, device, destination path, and route; it stores random transfer IDs, a step history, dispatch checkpoints, and a renewable lease. The runner stores the verified source in a private spool, checks its hash when resuming, rechecks current permissions and route, compares target upload status, and never resends an unknown commit. A changed route pauses the operation for inspection.

`subchat_device_adapters.py` uses the selected account's existing verified download and routes target `upload_*` and `operations_get` through `DeviceRouter`. The direct HTTPS tool `subchat_save_file` has its own OAuth scope and explicit request ID. Local target operation IDs use a stable same-principal namespace across OAuth re-consent while each step rechecks the current grant. The tool returns only status and bounded metadata. It is exercised with the real local Engine, DeviceRouter, and Uploads implementation. A completed file from a real selected Chat account also passed through the gateway, verified download, local router, and upload target, producing a 19-byte file with the source SHA-256 under a synthetic local grant. A separate integration uses a real OAuth bearer and HTTP MCP session with a deterministic Chat download fixture: a grant lacking `subchat_save_file` is refused, while the scoped grant publishes verified bytes through the real local upload implementation and returns no file payload. A live remote device still requires acceptance evidence.

New target uploads retain both the original requested path and the target's
canonical path. Recovery compares the original path to the immutable save
request, so a remote target can resolve an alias without leaving the save
permanently at `uploading`. Legacy target records without the original path
continue to require exact remote path equality. This change has local target
and simulated remote-status tests; actual remote transport remains unverified.
The target adapter now rechecks the current OAuth grant and selected device
route after awaiting each target-tool catalog, immediately before a routed
upload call. A paused-catalog regression revokes the grant or simulates a route
change and confirms that no target operation is dispatched. This narrows the
pre-dispatch race; a request already inside a remote transport remains subject
to the target's independent authorization and outcome recovery.
