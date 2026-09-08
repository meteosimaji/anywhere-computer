---
name: computer-work
description: Use Anywhere Computer for local files, document text, searches and persistent terminal sessions, with operation recovery after a lost response.
---

Check computer_status before acting. Use the reported capabilities and explicit
absolute paths. Read before editing an existing file, then supply its SHA-256;
if a conflict occurs, read again and reassess the intended edit.

Use paginated reads and searches. documents_read inspects OOXML content; it does
not render pages or evaluate Excel formulas. Do not imply complete Office support.

Use files_read_binary/files_write_binary for binary files up to 16 MiB, in chunks
up to 256 KiB. Carry the whole-file expected_sha256 across download reads and
between upload appends. Stage uploads at an unused temporary absolute path, verify
the final hash and length, then files_move to an unused destination on the same
filesystem. Each chunk is committed separately; the transfer is not atomic as a
whole. After a lost response, inspect operations_get and the staged file hash and
length before another append. files_restore can restore binary backups too.

Terminal sessions belong to the persistent agent. Preserve session IDs and output
cursors. A client disconnect does not stop a process. Use terminal_stop only when
stopping that work is intended. On an uncertain response, keep the operation ID
and query operations_get before considering another mutation. Never interpret a
missing response as proof that a write did not happen.

This plugin connects to the local agent by default. The separate remote-mcp CLI
can select an already configured SSH device. http-mcp can select an authorized
HTTP profile. http-configure/http-serve support a persistent loopback HTTP server
behind an owner-configured HTTPS proxy. These do not provision a public endpoint
or managed internet relay. GUI interaction and OCR are not implemented. Explain
those limits when they affect the requested task.
