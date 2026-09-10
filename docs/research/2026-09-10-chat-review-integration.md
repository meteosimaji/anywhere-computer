# Chat review integration — 2026-09-10

The user supplied a review bundle against commit
`9e93c78b9e0d87c03cd6e4c6ae5095cd70140783`.
Its source and patch SHA-256 values match the manifest and current source.
The archive was inspected without executing its documentation commands or extracting paths.

## Confirmed defect and bounded correction

`codex_plugins._tool_result` validates and normalizes the child MCP `isError`
boolean into `is_error`. The engine records completed execution, which does not
imply a successful child operation. MCPSession previously discarded that distinction.
For `codex_plugin_call` only, a literal `is_error is True` now sets outer MCP
`isError`. Operation ID, ledger state, diagnostics and content are preserved.
Other tools' data do not acquire new error semantics. No new runtime dependencies.

The shared MCPSession is used by stdio, authorized HTTP, remote bridge and HTTP
client adapters. Tool catalog checks still precede dispatch. No grant expansion,
retry, approval auto-acceptance or Codex model inference was introduced.
The supplied 15 regression cases were run against the actual installed package:
before the source fix, 2 failed and 13 passed, matching the reported defect.
A further official MCP SDK / HTTP test exercises result normalization, engine
execution and the outer response using a simulated child plugin rejection.
This does not certify a live third-party plugin or desktop GUI operation.

## Live connection observation

On this inspection, no listener was found on local TCP 18768 and the public MCP
URL returned HTTP 530. This is separate from the error propagation defect.
The successful 2026-09-09 ChatGPT status/root metadata calls remain historical
acceptance evidence, not a guarantee of current reachability.
The report's 23 visible tools is a Chat-side observation; the cause has not been
proven. Do not expand grants merely to expose additional tools.

## Next acceptance gates

1. Restore a persistent service independent of a coding tool session. Verify
   public reachability, authorization, current catalog and actual Chat calls.
2. Complete a scoped scratch-workspace read/edit/test/output round trip, including
   a stale-hash conflict and an unsuccessful command. Do not claim filesystem
   sandboxing from a tool allowlist.
3. Add effective capability diagnostics distinguishing implementation, deployed
   version, grant scope and measured availability; omit credentials.
4. Improve plugin result forwarding, explicit interaction outcomes and scoped
   session retention. Verify existing Computer Use discovery and execution before
   building a separate screen/streaming implementation.
5. Extend skill/workspace context with independently checked diffs and test receipts.

The primary architecture remains Chat-controlled reasoning and direct execution;
Codex model delegation and continuous human screen streaming are separate work.
This integration does not claim completion of those subsequent gates.

## Validation on the Mac checkout

- Full suite after the correction and imported regressions: 583 passed, 5 skipped.
- Targeted suite including the subsequently added official SDK HTTP regression:
  46 passed. The combined test set therefore has 584 passing cases; the single
  full-suite run did not include that last added integration test.
- Ruff: passed for src/tests/scripts. Mypy: passed for 58 source files.
- Bundled plugin wheel regenerated; source/bundle consistency tests passed.
- `uv build`: source distribution and wheel built successfully.
- An initial `python -m build --no-isolation` attempt failed because the development
  environment lacks hatchling; the configured isolated build above succeeded.
- `git diff --check`: passed. New test names checked for collisions.
- Running portable service and installed Codex cache have not been upgraded by
  this patch. Public ChatGPT integration is currently unavailable as recorded above.
