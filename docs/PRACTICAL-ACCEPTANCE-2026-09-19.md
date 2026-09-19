# Practical acceptance: code execution and browser playback

## Live installed engine

The installed macOS engine reported `0.1.0b1`. Through its actual Anywhere MCP
file and terminal tools, the trial created Python code and a UTF-8 CSV containing
four Japanese-category sales records in a dedicated temporary directory. Python
ran with exit code 0. A separate file read recovered JSON with total `3500.00`,
book sales `2000.00` and music sales `1500.00`, matching independently specified
expectations. A fresh source read supplied the SHA-256 for a conditional code
replacement; the write returned the original hash as its backup ID. A second run
exited 0 and the recovered file additionally contained two records per category.
All three terminal processes, including directory setup, were confirmed exited.

One malformed test request ID was rejected before execution. The test caller
corrected it to the required 32 lowercase hexadecimal characters before starting
the directory-creation command. This was a caller error, not a successful retry
of an operation with unknown effects.

## Repeatable HTTP acceptance

`tests/test_http_mcp.py::test_code_write_run_edit_and_recover_from_fresh_http_client`
uses the official MCP SDK, a real loopback HTTP server, actual file tools and real
terminal processes. It discovers tools in each fresh connection, creates source
and CSV through MCP, executes the program, verifies stdout and the generated JSON,
closes the first connection, recovers its read receipt from a new connection,
conditionally edits the source and reruns it. Both starts are deliberately sent
twice with identical request IDs. An append-only program output confirms exactly
two executions, not four.

The fixture uses static test authentication. It does not prove OAuth onboarding,
ChatGPT model behavior, VM connectivity, OS restart recovery or GUI automation.
It runs on the platform executing pytest; passing locally does not qualify
Windows. It is a practical workflow acceptance test, not a regression for a
newly discovered runtime defect.

## Browser playback boundary

Codex's owning browser tool opened Chrome and YouTube, then played Bach's first
cello-suite prelude. The visible player showed a pause control, unmuted audio,
and elapsed time advancing from 0 to 2 seconds. Speaker output was not measured.
The tab was retained for the user. This was a Codex browser-tool trial, not an
Anywhere browser-provider acceptance. Those routes must not be conflated.
