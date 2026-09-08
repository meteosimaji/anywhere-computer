---
name: computer-work
description: Use Anywhere Computer for local files, document text, searches and persistent terminal sessions, with operation recovery after a lost response.
---

Check computer_status before acting. Use the reported capabilities and explicit
absolute paths. Read before editing an existing file, then supply its SHA-256;
if a conflict occurs, read again and reassess the intended edit.

Use paginated reads and searches. documents_read inspects OOXML content; it does
not render pages or evaluate Excel formulas. Do not imply complete Office support.

Terminal sessions belong to the persistent agent. Preserve session IDs and output
cursors. A client disconnect does not stop a process. Use terminal_stop only when
stopping that work is intended. On an uncertain response, keep the operation ID
and query operations_get before considering another mutation. Never interpret a
missing response as proof that a write did not happen.

This plugin connects to the local agent by default. The separate remote-mcp CLI
can select an already configured SSH device; it does not provision remote access.
The current version has no GUI interaction, OCR, public HTTP endpoint or managed
internet relay. Explain those limits when they affect the requested task.
