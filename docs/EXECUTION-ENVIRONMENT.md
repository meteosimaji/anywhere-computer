# Tools in a resident process

A terminal opened by a desktop or startup service can inherit a shorter PATH
than an interactive shell. A tool installed after the service started may also
be absent from its inherited PATH. An executable being installed does not prove
that the resident process can resolve it.

The post-beta-1 runtime appends these existing directories when starting terminal,
direct stdio MCP, or Codex adapter processes:

| Platform | Additional search locations, in order |
| --- | --- |
| macOS | `/opt/homebrew/bin`, `/usr/local/bin`, `~/.local/bin` |
| Windows | `~/.local/bin`, `%APPDATA%/npm` (or `~/AppData/Roaming/npm`) |
| Linux | `~/.local/bin` |

Configured PATH entries retain their precedence. Missing or relative fallback
directories and the current working directory are not added. Windows PATH keys
and duplicate entries are compared case-insensitively. The process environment
is not globally changed; existing sessions retain their own environment.
Directories are checked on each spawn, so later installations in these locations
do not require restarting the agent. Arbitrary custom install locations and
changes to the system/user PATH registry still require explicit configuration or
an agent restart. There is no recursive executable search or shell-profile loading.

Direct MCP still requires an absolute server executable path. Only its child
PATH is augmented; the SDK's minimal environment is retained, so unrelated
credentials in the agent environment are not forwarded. Codex discovery uses the
same augmented PATH, while `ANYWHERE_CODEX_EXECUTABLE` remains the explicit override.
This does not install tools, activate provider accounts or grant OS permissions.

`anywhere doctor` reports `executables_on_path` for the diagnostic process and
`executables_for_new_children` for these augmented launches (`node`, `uv`, `codex`).
`codex_selection` reports the executable selected by the adapter's actual resolver,
including `ANYWHERE_CODEX_EXECUTABLE`. Its `source` distinguishes `explicit_override`
from `path`. An invalid explicit override remains `unresolved`; it does not fall
back to another Codex on PATH. `resolved` means the path passed selection checks,
not that it is executable, compatible or authenticated (`execution_verified=false`).
It resolves paths without executing discovered programs and does not print the
whole environment. These are diagnostic-process observations, not a claim that a
different running engine or remote computer has the same environment. It does
not prove tool startup or GUI operation. Run diagnosis on the affected computer
and inspect actual operation results.

The published beta 1 remains unchanged; these changes require the newer runtime.
