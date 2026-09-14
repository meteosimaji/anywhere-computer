# Implementation status and beta acceptance

Snapshot: 2026-09-14. This is a capability inventory, not a beta release claim.
Main includes PRs #25–32. The common-skills work is a subsequent development change.
Installed alpha 9, the development checkout, CI artifacts, and published releases
are distinct. Consult each PR for current merge status.

## Product direction

English is the default for public documentation and connection UI. Japanese
instructions remain available. The CLI is the primary setup and maintenance
interface; the management app is optional and shares the same controllers.
This changes presentation and priority, not the required pairing, recovery,
multiple-PC support, or platform acceptance. No extra per-operation approval UI
is planned. Client approvals and initial connection authentication remain separate.

## Capability inventory

| Area | Implemented and evidence | What must still be delivered |
| --- | --- | --- |
| Files and search | Engine file tools, hash-conditional writes, backup/recovery, bounded search and result retrieval. Existing fresh Chat acceptance reports Mac operations; not every reported operation was independently reconciled. | Preserve these paths through packaging, updates and remote routing; complete Windows VM acceptance through a fresh Chat. |
| Terminal | `sessions.py` owns persistent pipe processes, input, output cursors and cleanup. | Actual PTY and Windows ConPTY, terminal resize and interactive programs. A pipe session is not a PTY. |
| Operations | Persistent engine ledger, owner/device binding, result recovery and duplicate-dispatch prevention. | End-to-end recovery across public relay credentials, OS sleep/restart and real release updates. Do not promise terminal memory survives an engine or OS restart. |
| MCP and optional Codex | Direct stdio MCP sessions plus optional Codex Plugin discovery/calls. State continuation and result recovery have tests and historical Chat receipts. | Packaged independent backends, compatibility policy and realistic cross-platform acceptance. Codex-owned Computer Use is not a generally usable external backend. |
| Skills | Optional Codex catalog remains. Development `skills_list` / `skills_read` discover .agents/skills or explicit roots and read hash-checked SKILL.md and UTF-8 resources without Codex. | Persistent custom-root setup, packaged release and cross-platform acceptance. Reading a Skill does not install its tools. See [common skills](COMMON-SKILLS.md). |
| Documents | Office text extraction (`documents.py`); basic DOCX/XLSX creation (`document_writer.py`). | Rendered preview, meaningful diffs and targeted edits that preserve unrelated document content. Text extraction is not Office app automation. |
| Multiple PCs | Saved SSH/HTTP device router; exact device IDs, authorized catalogs and operation bindings. Isolated relay tests include multiple devices. | Unified account/relay-backed device listing and selection from one AI connection; Windows VM end-to-end success. Saved registration does not prove reachability. |
| Setup | Shared setup/controller functions; management status/checks; PR #26 adds interactive `anywhere setup` using existing flows. Isolated Mac local startup, persistence after CLI exit, status and stop were exercised. | Bundled dependency-free installation, pairing-to-AI setup, OS startup integration and clean Mac/Windows first-install acceptance. Current HTTPS setup still needs hosting details. |
| Relay | Isolated authenticated WebSocket transport, signed execution envelope, grant checks and request-scoped HTTP authentication; incompatible protocols rejected before dispatch. | Credential issuance/storage/renewal/revocation integrated with the resident PC client, production-capable account/device service and public-service acceptance. Public deployment remains a separate approval boundary. |
| Update | Verified candidate preparation, waiting, activation and interrupted update records. Default manual, automatic stable opt-in. PR #27 discovers installed `gh`. | Bundle the verifier; prove signed release installation and recovery on both target OSes, then reconcile every entry point against the same updated engine. Finding `gh` is not dependency-free installation. |
| GUI | Optional Peekaboo adapter. Historical Mac Calculator and TextEdit observations/actions/result recovery; Peekaboo 4 exact-window capture is required by the typed adapter; foreground fallback removed after a real cross-app input failure. | Default backend selection, redistribution/version policy, Windows GUI, focus/DPI/Japanese-input/stale-reference/disconnection suite. Earlier input mismatches remain relevant; individual successes do not qualify arbitrary apps. |
| Browser | Direct MCP transport exercised with isolated Playwright and a synthetic page. | Packaged default, existing login/tab workflow, sustained tasks and measured latency/output. Do not equate generic MCP connectivity with finished browser integration. |
| Public readiness | MIT project; English README with Japanese companion in PR #25; English OAuth screen in this change. | Remaining CLI/UI translations, supported-backend license/version decisions, beta artifacts and complete acceptance. GitHub publication does not imply listing in an AI client's directory. |

## Completion order

1. Close the current CLI/setup changes with Windows CI and package verification.
   Investigate the lifecycle test's initial authentication timeout without weakening
   password verification or crash-recovery assertions.
2. Connect pairing, credential lifecycle and resident PC transport through the shared
   controller. Prove two PCs are selectable from one AI connection in an isolated
   installation before public deployment.
3. Exercise app close, reconnect, sleep, restart and update with identity and ledger
   continuity; complete bundled installation and verification-tool delivery.
4. Finish selective catalog retrieval/invalidation and measure requests, bytes and
   latency on identical tasks. `device_router.py` now filters the returned catalog by name/query or omits schemas
   for summaries; upstream retrieval remains a full current catalog. Caching,
   invalidation and end-to-end latency measurement remain unfinished.
5. Select and integrate independent GUI/browser defaults; qualify Mac and Windows
   separately. Finish PTY/ConPTY, document preview/editing and common Skills.
6. Run fresh ChatGPT 5.6 acceptance plus machine-reproduced authenticated HTTP/MCP
   acceptance against the actual candidate; publish beta only after all requirements
   in the development directive are reconciled with evidence.

## Evidence and limits

- [Fresh Chat acceptance](FRESH-CHAT-ACCEPTANCE-2026-09-14.md): installed alpha 9,
  Mac results and Windows failure; not acceptance of these development changes.
- [Setup controller](SETUP-CONTROLLER.md): isolated management and CLI receipts.
- [Relay](RELAY-PROTOTYPE.md): protocol and isolation boundaries.
- [GUI](GUI-MCP.md): provider versions, successful cases and unresolved mismatches.
- [Execution history](EXECUTION-ROADMAP.md): historical evidence, not a current
  feature checklist. Older “not implemented” statements are dated observations.

Current full acceptance is incomplete. CI success is evidence for its tested layer,
not proof of Windows VM, fresh AI client, public relay or release-update behavior.
