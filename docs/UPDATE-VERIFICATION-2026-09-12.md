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

## Shared-engine live migration follow-up

The old alpha 3 Codex MCP connector processes were terminated, leaving the
engine and stored results intact. This task's already-bound tool transport
then returned `Transport closed`; automatic reconnection in the same task
was not observed.

The first offline migration refused an unrelated manual archive directory
`backups/pre-main-integrated-20260910`. Selection was not published; services
were restarted and authenticated HTTP readiness was confirmed. The local
migration code now selects SHA-256 backup names only, leaving unrelated
manual archives in their original location. Owned backup entries still reject
symlinks, non-files and content/hash mismatches. The regression first failed
on the original code; after correction, 18 migration tests and the package
consistency test passed. Targeted Ruff and mypy passed. This correction has
not yet been published or installed as a new runtime release.

Using the corrected local migration command with both services stopped,
shared selection completed. The existing installed alpha 4 runtime then
started against that selected store, and the native HTTP service was restored.
No credentials or original databases were replaced with older copies.

Existing authenticated Chat HTTP status receipt
`d921d103051a4178841725a08e7a6283` and a fresh MCP subprocess launched from
the installed alpha 4 plugin configuration (receipt
`411c3ad2d8854b5fa3a88564505a5f21`) both returned instance
`9b00b5fbc11c483c8551e08e5f4fe30c` and runtime
`ce52717d83912225025bcebfb03f661f8cd0656bf06367654b7a48ef2d1b17e6`.
The HTTP alpha 2 write was recovered with receipt
`53eecea5794f463bb58ae2f2f4f1447e`; the local alpha 3 status operation
`59ed6e98c3b24d5bb706a730fae500cb` was recovered via the fresh MCP subprocess
with receipt `1a594543aacc42f2bae8d910f7886aeb`.
This proves shared live routing and preservation of both histories, but does
not prove restoration of this task's closed tool transport, stable automatic
updates, GUI operation, or the remaining five-stage acceptance criteria.

## Alpha 5 update with an existing MCP session

The manual-archive migration correction was published in `f1aa686`, with
clean alpha 5 provenance in `137b785`. Nineteen targeted migration/package
tests passed; full Ruff and mypy (64 sources) passed. The offline macOS
portable build's 2,260 file hashes matched after extraction.

A real official-SDK session launched from the installed alpha 4 MCP plugin
remained open during an explicit idle engine update to alpha 5. Before:
`161aacb6cbbb4d2e9884e81162c66719` (alpha 4). After, on the same MCP session:
`f2764b5043f941c4b87482a4d2d59236` (alpha 5). Recovery of the before result
returned the identical original reply (`ced22aac69ce4b2b8f4ec4b779316d8e`),
and ping succeeded without reinitialization.

The existing authenticated Chat HTTP entrance, still running alpha 4 gateway
code at that moment, reached the same alpha 5 engine: status receipt
`7615e6b008314da0b4919ca143069e6d`, instance
`4c2600125de749a4b5659856cfa05c00`, runtime
`c35f7f9830265f3b22981fc39c152c583a2d3962aa01b65544dc120131dc1d72`.
The native startup registration was subsequently upgraded to alpha 5 and
started; the supported Codex plugin installer returned alpha 5 as well.
This proves a compatible engine update can preserve an open MCP session;
it does not prove recovery of an already-closed Codex task transport or
completion of stable discovery/automatic application. Bootstrap still needs
a persistent current-runtime selection so older compatible connectors cannot
start an older engine when no engine is running.

## Alpha 6 selected-runtime deployment

Runtime selection and interrupted-update recovery were published in
`e188e72`, with clean alpha 6 provenance in `117e9c2`. Forty-three targeted
connection/selection/HTTP/package tests passed, as did Ruff and mypy for
65 source files. The macOS portable archive was built offline, and all
2,261 manifest file hashes matched after extraction.

The idle shared live engine was explicitly updated to alpha 6; its selected
interpreter was persisted under the existing control directory. The pending
update marker was absent after completion. Native startup was upgraded and
started with the same portable installation; the supported Codex plugin
installer returned `0.1.0-alpha.6`.

Existing authenticated Chat HTTP status `aa7e04ca62444c068277cdb603e1f84c`
returned alpha 6, instance `81b2668883ce4258846f6687130fce72`, runtime
`b0e1cd9e801eae911036954fe7dbe2f5c96f286bc3f73e4209ce455166d91bfe`.
The prior alpha 5 operation `e72f17d7a9d546bc9187d888842d2f4b` remained
recoverable (`7eccb509ea29421bb9245c17b54c681c`). No reauthorization was
performed. A prior isolated real-process test verified restarting from the
saved interpreter and resuming a pending update; this deployment did not
reboot the operating system. Older connectors that predate runtime selection
still require an initial upgrade. Stable release discovery and automatic
application remain unfinished; alpha 6 is not being represented as a
CI-verified stable release.
