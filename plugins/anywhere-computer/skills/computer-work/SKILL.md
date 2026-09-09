---
name: computer-work
description: Use Anywhere Computer for local files, document text, searches and persistent terminal sessions, with operation recovery after a lost response.
---

Check computer_status before acting. Use the reported capabilities and explicit
absolute paths. Read before editing an existing file, then supply its SHA-256;
if a conflict occurs, read again and reassess the intended edit.

Use paginated reads and searches. search_start supports filename_glob,
excluded_directories, whole_word, max_files and max_depth. Inspect truncated,
limit_reason, skipped and directory_errors before claiming a complete search.
Use regex only when explicitly selected; filename/directory filters use basename globs.
Document searches support bounded OOXML content and may truncate long fields. documents_read inspects OOXML content; it does
not render pages or evaluate Excel formulas. Do not imply complete Office support.

Use files_read_binary for binary files up to 1 GiB (a full hash scan per call),
and files_write_binary for files up to 16 MiB, in chunks
up to 256 KiB. Carry the whole-file expected_sha256 across download reads and
between upload appends. Stage uploads at an unused temporary absolute path, verify
the final hash and length, then files_move to an unused destination on the same
filesystem. Each chunk is committed separately; the transfer is not atomic as a
whole. After a lost response, inspect operations_get and the staged file hash and
length before another append. files_restore can restore binary backups too.

For efficient large downloads, use download_begin with a fresh 32-hex transfer_id,
then download_read ranges up to 256 KiB. The saved copy survives source changes
and engine restart. Verify chunk and whole hashes at the receiver, then download_close.
Keep the ID; inspect download_status and operations_get after a lost begin response.
A closed ID cannot be reused; a new HTTP grant cannot access an old grant's copy.

For larger uploads to new destinations, use upload_begin with a fresh 32-hex
transfer_id, final byte length and SHA-256; keep this ID. Send contiguous upload_chunk
requests, inspect upload_status after interruptions, then upload_commit. Declared
limit is 1 GiB per upload. Unknown publication must not be retried automatically:
upload_resolve confirm_published verifies the destination without publishing again.
Use discard_staging only when discarding that transfer's database chunks is intended;
it leaves destination and leftover staging_path files untouched. A new HTTP grant
cannot resume an old grant's transfer. A completed record is not a live file check.

For owner maintenance on the local host, the transfers CLI lists stored transfers
with explicit --transfer-area local/http and --transfer-kind upload/download.
transfer-release uses the listed --storage-id to close a download or abort a receiving
upload. This interrupts the selected transfer; inspect its metadata first. It never
resolves uncertain publication or deletes the source/destination. These commands
are local administration, not remote MCP tools.

Terminal sessions belong to the persistent agent. Preserve session IDs and output
cursors. A client disconnect does not stop a process. Use terminal_stop only when
stopping that work is intended. On an uncertain response, keep the operation ID
and query operations_get before considering another mutation. Never interpret a
missing response as proof that a write did not happen.

The local connector provides devices_list, devices_tools and devices_call. List registered
devices, then obtain the selected device's authorized tool schemas with devices_tools.
Pass its explicit device_id, tool and arguments to devices_call. The reserved local ID
addresses this connector's local agent; ordinary tools still operate locally. A cached
ready observation is not a fresh connection check. Never infer the target from a device name.
Keep device_id together with every session, search, transfer and operation ID. After a
lost routed response, use devices_call on that SAME device to invoke operations_get,
passing the original operation ID inside arguments and a fresh ID for the lookup request.
Do not repeat a mutation with a new operation ID. The result is nested under data.result.
Registration and login are explicit local CLI actions; routing never opens a login flow
or exposes saved credentials. Remote catalogs cannot forward to further devices.

The separate remote-mcp CLI
can select an already configured SSH device. http-mcp can select an authorized
HTTP profile. http-configure/http-serve support a persistent loopback HTTP server
behind an owner-configured HTTPS proxy. http-watch can supervise a configured
server in the foreground with bounded crash restarts; it does not repair hangs,
network outages, or restore terminal sessions after a server crash. Optional
tunnel-token/tunnel-run commands use an already configured cloudflared tunnel,
with native-keyring credentials handed to a child through an OS pipe and bounded
child crash restarts. Setup accepts a token only in a hidden interactive prompt.
Never put it in arguments, environment, files or chat. A token-sent event is not
public connectivity evidence. tunnel-forget removes local credentials only;
provider revocation and DNS are separate. These commands do not provision a public endpoint
or managed internet relay. http-doctor probes only configured loopback metadata;
metadata_reachable does not prove authenticated readiness or public HTTPS reachability. GUI interaction and OCR are not implemented. Explain
those limits when they affect the requested task.
