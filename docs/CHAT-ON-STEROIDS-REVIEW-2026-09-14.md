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

## Follow-up: 2.1.11 and roadmap acceptance

Rechecked upstream main on 2026-09-14 at
`788c93af1b0889ae8398185362452a9993ce8ecb`, one commit after the initial
inspection. The following conclusions come from the GitHub comparison patches
for `src/main/plugins/exposure.ts`, `src/main/plugin-refresh.ts`,
`src/main/session/input.ts` and `src/renderer/i18n.ts`; no upstream tests or
application were run. This is not a complete review of the release.

- Plugin exposure raises the emergency tool-count ceiling from 64 to 256 while
  retaining a 250,000-byte schema budget. Refresh accepts a legacy subset only
  when its declarations match the new publication; completion still requires
  the full catalog. For stage 3, test both byte and count limits, explicit
  truncation/pagination, and cache invalidation after declaration changes. Merely
  raising a ceiling is not evidence of better token efficiency.
- Input retention adds retirement of confirmed delivery receipts when their
  exact session directory is gone, before origin repair. For stage 2, test
  deletion versus temporary unavailability and ensure recovery cannot resurrect
  deliberately removed history. Anywhere's operation deduplication records have
  different semantics and must not inherit this deletion rule automatically.
- Renderer translation drops a historical WeakRef index in favor of walking
  current document nodes. For stages 1 and 3, include repeated refresh and long
  sessions in management UI responsiveness/memory measurement; this patch alone
  does not establish that Anywhere has the same defect.

The current README still describes Developer-mode connector setup and a loaded
companion extension, with extension reload and app refresh after updates. Its
automatic pairing refers to that extension workflow, not proof of a universal
install-to-AI pairing flow. Anywhere should retain provider-independent MCP and
one installed engine, make the remaining client-side setup explicit, and measure
first-use steps on a clean installation. Goal/Loop/conversation compaction are
useful workflow comparisons but do not become core dependencies or additional
release requirements merely because this competitor offers them.

Source comparison:
https://github.com/totec448-spec/chat-on-steroids/compare/1517d66dac1e7452f63b7452c88479c92a554768...788c93af1b0889ae8398185362452a9993ce8ecb
