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

## alpha 4 connection checkpoint

Source commit `75976719f2483b165b8b5b774cb28708f6379f52` and clean bundle
provenance commit `864f958` were pushed to main. The full local suite before the
version-only packaging change passed: 756 passed, 5 skipped. Ruff passed for
src/tests/scripts; mypy passed for 64 source files. After packaging alpha 4,
24 package/migration/selection tests passed. The macOS portable archive was
built offline and all 2,260 manifest file hashes matched after extraction.

The native HTTP service was upgraded and started with alpha 4. The existing
Chat connector returned version `0.1.0a4`, engine API 1, authenticated HTTP,
instance `6c2e68ad62c7436999556c3c0fd2d68d`, and runtime
`ce52717d83912225025bcebfb03f661f8cd0656bf06367654b7a48ef2d1b17e6`.
Status receipt: `62de74b6eb8c440a99dec88f40437cb7`.
The existing UTF-8 fixture was read with its original SHA-256
(`aad85ead314744ecb43ee75afae166e2`). The alpha 2 write operation
`7413366671ff4f02bf109ec846e20680` remained recoverable after this update
(`0834f6aefadd45618cc485d16db35485`). No reauthorization was performed.

The supported Codex plugin installation command returned version
`0.1.0-alpha.4`. The local engine was explicitly started with alpha 4, but
already-running Codex MCP connector processes still had alpha 3 working
directories at this checkpoint. Installation does not prove those processes
have reloaded. Shared-state selection has therefore NOT been applied to the
live installation yet: old connectors must be retired before migration,
because their old code does not recognize the new state selection. The HTTP
and local engines remain separate at this checkpoint. Stable automatic
updates and live shared-engine acceptance remain unfinished.
