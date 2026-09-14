# Retained-client shared-agent recovery

Date: 2026-09-14. Development evidence, not a beta or installed-runtime acceptance.
Base: `d6fcf56407d536bfb3c0fd9b399f1dd54f810de0` (PR #35), on the isolated
`chat/beta-reconnect-20260914` worktree. PR #34 is not included. The original
checkout and running service were not updated by this change.

## Defect and correction

`MCPSession.handle` obtains the catalog before dispatching a `tools/call` request.
The retained local connector's `run_mcp.catalog` called `exchange(__catalog)`
without invoking the existing startup/recovery controller. Its recovery in
`execute` therefore could not be reached after the agent exited. The shared-agent
HTTP session similarly forwarded its catalog request directly to an unavailable
agent. Initial CLI/HTTP startup checks did not cover these retained sessions.

Both catalog paths now call `ensure_agent(..., replace_idle=False)` before fetching
the current catalog. This reuses existing startup locking, selected-runtime
resolution, process identity checks and update-recovery behavior. It neither
replaces a live compatible agent nor force-stops an unresponsive live process.
Local catalog responses must also have `state=completed`, not merely a list field.

HTTP authorization is checked before preparing the agent and checked again by
`local_execute` after the asynchronous startup wait. Revocation during that wait
must not leave the old grant usable for a subsequent write.

There is no dispatch retry loop. An engine loss after the catalog check can still
produce an unconfirmed outcome; callers must recover the original operation ID.
No public tool/schema, version, authorization scope, dependency or data format was
changed. Native startup registration, production credentials and installed
runtime selection remain untouched. Catalog readiness adds a control-plane check;
no latency or catalog-cache improvement is claimed.

## Verification scope

`tests/test_shared_agent_reconnect.py` adds 14 cases:

- Retain each stdio connector session or authenticated HTTP session, commit a
  Japanese/emoji file, stop or kill only its disposable agent, then make the first
  request a catalog lookup or an operation lookup. Confirm the old result and
  hash survive, the instance changes, the runtime remains the same, and an exact
  repeated create resolves to its recorded result without modifying the file.
- Lose a stdio write response after the actual write commits. Confirm `unknown`
  retains the original operation ID, only one write is dispatched, and a separate
  lookup recovers the completed result.
- Revoke the HTTP grant during the recovery wait and confirm no target file is
  created. This is a regression guard for the newly introduced wait, not evidence
  that an identical wait existed before the change.
- Send four simultaneous catalog requests after killing the fixture agent and
  confirm only one replacement starts, for both entry paths.
- Inject an owner-identity rejection and confirm there is no write, replacement
  process or fallback, for both entry paths.

These tests use real disposable engine processes, local sockets, SQLite ledgers
and, for HTTP, real HTTP requests, authentication and the same MCP session ID.
The stdio recovery cases exercise the real `run_mcp` callbacks and `MCPSession`
with an in-process stream harness; they are not a separate external Chat client.
The existing official-SDK subprocess protocol test is also included in regression
runs. Native credential provisioning is replaced with a synthetic fixture value;
no user credential store or installed service is modified.

The four pre-existing related modules passed before editing: 28 passed, 1 skipped.
After correcting an import-interfering test fixture, the first six new cases on
unchanged source produced 5 failures and 1 pass. Four failures demonstrated the
catalog/recovery defect; the fifth was the new authorization-wait guard. All six
then passed with the source correction. The final expanded 14 cases passed.

Final local verification: the full suite passed with 1,249 passed and 18 skipped
(out of 1,267 collected). Skips are platform-specific Linux/Windows checks and
native registration reserved for disposable CI runners. Ruff passed; strict mypy
passed over 97 source files. Source distribution and wheel builds succeeded, and
the full suite includes the bundled-source/checksum consistency test.

The development runtime remains version `0.1.0a9`, with source runtime ID
`63bfeb21868d89a4c16a7759033c12174d596fd6753ae1ae050ae1f9497de6d8`.
This is not the runtime currently installed in the user's live connection.

Local receipts are retained in the worktree's ignored `output/` directory:
`reconnect-before.log`, `reconnect-before.xml`, `reconnect-after.log`,
`reconnect-after.xml`, `reconnect-expanded.xml`, `package-build.log`,
`full-suite.log` and `full-suite.xml`. The final handoff records the full-suite,
static-check and build outcomes; these must not be inferred from collection or
process-start acknowledgements. The bundled plugin is regenerated from the
changed source and remains an unverified development artifact, not a signed beta.

## Remaining release gates

Merge and qualify the pending startup/CLI changes through the normal review path,
then test an actual Windows candidate from the same Chat connection. The inspected
Windows route failed before its tool catalog was obtained; Mac tests and Windows
CI on other PRs do not qualify that route or this patch on Windows.

Next reconcile each installed entry point against the selected engine's version,
runtime and instance, without silently expanding grants or assuming that changing
the engine also refreshes a client's public tool schema. Qualify first install,
update, reconnection, native credentials, sleep and OS restart against the actual
Mac/Windows distribution. This patch addresses only one retained-client recovery
gap; it does not complete public relay, GUI/browser, PTY/ConPTY, document editing,
catalog caching or the full beta directive.
