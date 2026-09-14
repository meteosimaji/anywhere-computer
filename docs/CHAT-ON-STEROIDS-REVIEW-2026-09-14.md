# Chat On Steroids reference review

Inspected on 2026-09-14 at commit
`1517d66dac1e7452f63b7452c88479c92a554768` (`package.json`: 2.1.0).
Source: https://github.com/totec448-spec/chat-on-steroids/tree/1517d66dac1e7452f63b7452c88479c92a554768
This is a bounded source/document review, not installation, test execution or a
security audit. No upstream application or extension was executed or installed.

## What was verified

- `LICENSE` is MIT with attribution/notice preservation. Package dependencies and
  separate native/plugin notices exist; this review does not clear every bundled
  component for redistribution.
- `package.json` uses Electron 44.3.0, TypeScript, MCP libraries and node-pty
  1.2.0-beta.15. Adopting the whole application would add another runtime and
  execution stack to Anywhere Computer.
- `extension/manifest.json` registers ChatGPT page content scripts, including
  MAIN-world scripts, and permissions for localhost ports 8765–8769. This is a
  ChatGPT/browser integration, not a provider-independent MCP-only architecture.
- `src/main/mcp/surfaces.ts` defines Core, Desktop and Plugins discovery surfaces,
  stable names and platform/capability-dependent tool projections. Its setup
  helpers omit unusable Desktop capabilities. Registration and authorization
  enforcement across all consumers were not audited.
- `src/main/session/resume-gate.ts` tracks pending replacement conversations with
  a 60-second expiry, preventing premature creation of a competing local session.
  This establishes the gate implementation, not successful end-to-end compaction.
- `src/main/session/retention.ts` serializes retention sweeps and supplies a stop
  hook. Actual pruning and data-preservation behavior were not evaluated.

## Documented behavior, not reproduced here

`docs/setup.md` describes developer-mode MCP setup, an OpenAI tunnel ID/key or
alternative tunnel, and loading a companion extension. Updates may require both
extension reload and connector refresh. Goal, Loop and Compact & Resume include
browser orchestration; the document explicitly distinguishes it from a public
ChatGPT automation API. This is not evidence that general pairing or updates
require no user setup, or that account limits disappear.

## Decisions for Anywhere Computer

1. Reuse the idea of stable, capability-aware setup descriptions and bounded
   discovery. Measure the existing selective catalog before adding connectors;
   multiple surfaces also increase the user's setup burden.
2. Add recovery race cases to acceptance: a new connection must not become a new
   device accidentally, completed work must not restart, and reconnect must not
   be presented as recovery of an old process or REPL.
3. Keep a documented retention policy for diagnostic artifacts without deleting
   the authoritative operation history in ways that allow replay.
4. Do not make a ChatGPT content-script extension, conversation recorder, Electron
   runtime or worker-model orchestration a core dependency. The present directive
   targets an independent execution agent for multiple AI clients.

The inspected repository is useful for workflow presentation and recovery test
ideas. It is not a substitute for authenticated multi-PC outbound relay,
provisioning, installed macOS/Windows acceptance or public service operations.
