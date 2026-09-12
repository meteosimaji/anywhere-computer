# Alpha 3 update and reconnection verification

The user authorized pushing the audit fixes and updating the installed plugin and
service. Source commit `fc4f2870a6dc71d9106ccaee33916c88cdde9f6b` builds
`0.1.0a3` / plugin `0.1.0-alpha.3`; `ab5912b` records clean source provenance.
This alpha is not a validated stable-channel release.

## Completed checks

- Local milestone suite: 722 passed, 5 skipped before the final native-removal fix.
  Ruff passed; mypy passed for 61 source files.
- Final native-removal fix: 30 targeted startup/recovery/package tests passed;
  focused Ruff and mypy passed. The regression failed before the fix.
- macOS portable artifact: all 2,257 manifest file hashes verified after extraction;
  isolated engine startup and fixture file write succeeded.
- Installed Codex plugin through `codex plugin add anywhere-computer@personal`:
  receipt reported `0.1.0-alpha.3`. Directly started the installed stdio command,
  initialized MCP and read the fixture successfully using the user's local engine.
- Existing Chat HTTP plugin reconnected after service upgrade without a new login,
  a new endpoint, or reinstalling its Chat connector. It returned `0.1.0a3`.
- Both fresh stdio and existing HTTP entrances reported runtime hash
  `35613b31074662e0d434279048071975799439d385d0b40c2297fe306f058d78`.
  Their instance IDs differ: they still own separate engines.
- HTTP operation `7413366671ff4f02bf109ec846e20680` created a harmless temporary
  UTF-8 fixture before the alpha 3 restart. After restart, `operations_get` recovered
  the completed result; file read returned the same SHA-256:
  `ef9d12c394f89dc8edcedf2ddd8daa1fe51abaaefc6820fd704fd6bbc8706096`.
  An operation recorded by the older alpha 1 service was also recovered after migration.

## Problems exposed by the real update

Native service removal can acknowledge before macOS finishes removing the job.
The previous immediate query incorrectly rejected a valid transition. Removal now
waits up to five seconds while checking that the observed registration still matches;
it does not issue a second removal. The packaged alpha 3 upgrade command completed
successfully on the actual machine. Startup was explicitly started and its running
state confirmed before checking authenticated HTTP connectivity.

Already-running Codex MCP processes retain their old Python code. That old code
calls `ensure_agent` for every request and can replace an idle newer local engine
with its own older build. Installing the new plugin does not hot-reload those running
processes. A fresh installed-plugin stdio process was verified, but this existing
Codex task's old local MCP connection has not been hot-reloaded. Do not claim all
open tasks have adopted the new runtime.

## Remaining work

Use a stable launcher and a common engine for both entrances, prevent stale clients
from replacing newer engines, and reconnect across updates using the existing
credentials, endpoint and operation IDs. Add verified stable-release discovery,
compatibility checks and automatic idle application. Preserve active work and
provider authorization boundaries. Independent browser/native GUI implementation
and the remaining five-stage acceptance criteria remain unfinished.
