# Stateful plugin sessions: implementation and live acceptance

Date: 2026-09-10. Base: `0f60947` (`codex/chatgpt-plugin-bridge`).
Work branch: `codex/plugin-sessions`, in a separate worktree.

## Why this step

The existing bridge created and closed an App Server context for every catalog
inspection and tool call. This is suitable for one-shot requests, but loses
in-memory plugin state needed by multi-step workflows. The next increment retains
an explicitly opened context rather than adding an unrelated GUI implementation.

The shared catalog/dispatch code was factored into `PluginContext`; the existing
one-shot entry points remain. `PluginSessions` owns bounded lifetimes and locks.
Engine handlers select one-shot or session-backed execution with an optional ID.
Authenticated peer identity travels in execution context, never in tool arguments.

## Contract

Three tools open, inspect lifetime state and close a plugin session. The existing
catalog/call tools accept an optional `session_id`. There are 50 Engine tools.
New tool scopes are not silently added to existing OAuth grants.

At most four contexts may be live or awaiting confirmed cleanup. Idle timeout is
30-1800 seconds, default 300; a five-second reaper also runs without client polls.
Status polling does not extend lifetime. Calls within one context are serialized
by rejection (`session_busy`), not by an unbounded queue. Active work cannot be
closed or expired by another observer.

The same grant can continue after HTTP MCP reconnect and token rotation. Another
grant, even for the same owner/device, cannot inspect, call or close its session.
A workspace mismatch is rejected. Engine restart does not recreate old sessions;
a replayed completed open operation remains a historical result, not proof of a
currently live context. `operations_get` keeps its existing external-ID contract.

Every call still checks the current descriptor fingerprint. Stale catalogs do
not dispatch and do not destroy a healthy session. An uncertain dispatched result
invalidates and closes the context without retrying the action. Closing is not a
rollback of external effects. Cleanup confirmation covers the owned App Server,
not arbitrary independently running children or remote services of a third party.

## Live fixture acceptance

`verify_plugin_sessions.py` used the actual installed `codex-cli 0.153.4` and an
isolated CODEX_HOME containing only an in-memory counter MCP fixture. The client
was the official MCP Python SDK, over authenticated loopback HTTP with the real
OAuth grant store, AuthorizedDeviceMCP adapter and operation ledger.

Observed checks:

- One-shot calls returned counter values `[1, 1]`; a retained session returned
  `[1, 2]`, including an HTTP disconnect and access/refresh-token rotation.
- Retrying an identical operation ID returned the same recorded result without
  incrementing again. `operations_get` recovered it with the external operation ID.
- An intentionally stale fingerprint stopped before dispatch, left the counter
  unchanged, and allowed subsequent inspection/use of the same context.
- Another grant could not inspect, close, call or recover the first grant's work.
- A legacy grant's catalog still contained only its original three permissions.
- A synthetic PNG arrived as native ImageContent, without base64 duplication in
  structured text. This is not a real desktop screenshot or a GUI interaction.
- Explicit closure confirmed cleanup and refused later calls without recreating
  the runtime. Revoked grants no longer authenticated. Fixture children were
  checked by PID plus creation time and were no longer running after cleanup.

A separate opt-in installed-plugin check called
`openaiDeveloperDocs.list_openai_docs` twice through one session: two successful
calls, exactly one thread/start, and confirmed closure. The report records no
retrieved document contents or production credentials.

Both checks instrument actual App Server sends. All started threads were ephemeral;
no `turn/start`, `turn/steer` or `thread/resume` was sent. This is a protocol trace
check, not a measurement of billing or subscription usage counters.

The sanitized machine-readable results are in
`2026-09-10-plugin-sessions-live.json` beside this record.

## Validation environment correction

A first focused SDK subprocess test exposed a wrong-environment mistake: using
another worktree's virtualenv plus PYTHONPATH let the parent import new code, but
an isolated SDK child still imported the older editable installation. It returned
47 tools rather than 50. No expectation was weakened to accept this result.

A dedicated virtualenv was created with `uv sync --offline --locked --python 3.12`.
The interpreter and imported package paths were checked against this worktree.
`test_runtime_provenance.py` now compares the current source fingerprint with an
isolated Python subprocess. A deliberate negative control using the old environment
failed as expected; the dedicated environment passed.

The first complete suite also exposed four old tool-count assertions and a
subsequent aggregate catalog count. They were updated to the deliberately added
three tools (50 Engine tools, 56 including local routing/setup tools), not removed.

## Packaging and deployment boundary

The bundled wheel and checksums were rebuilt without changing runtime dependency
requirements or the lockfile. A standalone portable archive was built offline and
staged under the application support root at
`portable/plugin-sessions-20260910/Anywhere Computer`.

Portable verification passed file roundtrip, regex child, terminal reconnect,
busy-stop refusal, owned agent shutdown, fixture credential removal and 2256
manifest file checks. Source and staged runtime fingerprints were identical:

```
5ae13ad825cf7bde3fd8f2aa63b72ad8d58d5612c5a24946f27eddc91413070f
```

The production launchd registration was not migrated or restarted. Its existing
`plugin-inspect-20260910` runtime remained registered/running. No production OAuth
permissions or existing user edits were changed. The original modified
`scripts/verify_codex_plugins.py` and untracked `tests/test_codex_plugin_probe.py`
were preserved byte-for-byte and were not included in this branch.

The new tools have not been exercised through the production ChatGPT connection.
Doing so requires deployment, ChatGPT tool-definition refresh and explicit consent
for the added tool scopes. Local authenticated HTTP acceptance is not presented
as completion of that separate production acceptance gate. Native GUI control,
Windows/Linux live testing, sleep/logout/reboot recovery and the pre-existing
launchd upgrade timing issue were outside this patch.

## Reproduce

```sh
uv sync --offline --locked --python 3.12
.venv/bin/ruff check src tests scripts
.venv/bin/mypy src
.venv/bin/python -m pytest -q
.venv/bin/python scripts/verify_plugin_sessions.py --codex /absolute/path/to/codex
```

Add `--installed-cwd /absolute/workspace` only for the explicit registered-docs
probe. Full lifecycle, ID distinctions and consent details are documented in
`docs/PLUGIN-SESSIONS.md`.

## Final validation result

- Complete suite in the dedicated worktree virtualenv: **671 passed, 6 skipped**.
  Skipped tests are not claimed as executed coverage.
- Ruff (`src tests scripts`): all checks passed.
- Strict mypy (`src`): no issues in 60 source files.
- Official-SDK authenticated HTTP/native-Codex fixture and opt-in installed-docs
  check: passed; sanitized protocol traces are retained beside this record.
- Bundled wheel/source consistency and portable runtime verification: passed.
- No runtime dependency, lockfile, production grant or original-worktree edits
  were added to this change.
