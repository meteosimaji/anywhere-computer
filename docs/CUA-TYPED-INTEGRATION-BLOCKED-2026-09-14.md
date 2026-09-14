# Cua typed-adapter continuation: blocked, not implemented

Date: 2026-09-14. Base: `243d68ed25db9d2bbbf3c02de0b33439c1adafca`.
This branch preserves proposed acceptance tests and live evaluation evidence. It
must not be merged as a completed feature: the typed Cua adapter is absent.

## Live native operation established

The existing Cua Driver 0.28.1 evaluation binary was opened using `mcp --direct`
through this ordinary Chat's explicit Anywhere Computer MCP session. No Codex
model, foreground escalation, permission change or driver setting change was used.

The previously designated synthetic TextEdit document was selected by process
75975 and exact window 10373. A new Cua observation returned its text control and
a screenshot, both containing `Anywhere GUI 日本語 42 ✅`. One `set_value` action
changed the observed text control to `Anywhere GUI 日本語 43 ✅`. A separate
observation returned both the AX value and screenshot showing `43`. A later fresh
observation supplied the token used to restore `42`, and another observation
confirmed restoration. This establishes this native control and direct-MCP path,
not a completed portable backend or acceptance of other applications/OSes.

Evidence operation IDs:

- Cua session open: `5587c8d3c8ff472999f272d7d989dd96`.
- Fresh body and screenshot: `acfe4716c8034274847d91f3ec0ee44e`.
- Background replacement: `34dac774f1d2412bb238692a504f9457`.
- Independent value and screenshot readback: `5abd34c9b3f44e1fac586b614cce8dad`.
- Fresh observation before restoration: `fd244d221cf24bb5a1097bd516e5c601`.
- Restore original body: `b00b9bd5d98248f4872e5b24430d4ab7`.
- Restored body readback: `06cf6c67800c4ba5be0e7a79ec3dbc58`.
- Cua session close: `a14eda611cde4ca59907aadb1ba98b64`, cleanup confirmed.
- Screenshot-only Peekaboo session close: `bf5e6c312a674a5594815add882e6188`, cleanup confirmed.

The installed engine remained version `0.1.0a9`, runtime
`edfac0c9fee95cb506624f238d33a68d9bde86f4e8469bd708832d24b26bb23a`, instance
`51fb6c4803364738ad91a5e4fb7d67c8`. This live test is not a deployment of PR #36.

## Why the observation must be scoped

Cua's structured window response also included application-wide menu-bar nodes
outside the requested window. Their contents are deliberately not reproduced in
this document. A typed adapter must validate the target and parent graph, then
project only the selected window subtree before returning or persisting content.
Filtering the provider query can set `elements_complete=false` even when the
underlying full snapshot is complete; do not conflate query projection with an
incomplete capture. The proposed adapter explicitly selects Cua and never falls
back from a refused action or silently escalates to foreground input.

## Implementation boundary and preserved work

`tests/test_gui_cua.py` defines 25 proposed contract cases for explicit provider
selection, exact PID/window/token routing, owner/TTL/consumption, global typed-GUI
serialization, bounded and unambiguous observations, excluding unrelated menu
nodes, query visibility, and no implicit/compound input. Against the unchanged
base source, all 25 fail because the required Cua adapter/model extensions are
not implemented. These are pending feature specifications, not a new failure in
main's existing tested behavior.

The attempt to create `src/anywhere_computer/gui_cua.py` through `files_write` was
blocked by OpenAI before execution, with no operation ID. A separate file-info
request confirmed that the file does not exist. The denied creation was not
repeated through a terminal, another tool, another filename or another model.
No part of that rejected source was installed or pushed. This branch retains the
already-created tests rather than weakening them or pretending they pass.

## Outstanding acceptance gates

The committed main changes already fix retained-client recovery and typed GUI
error/image projection. Cua native text editing now has independent live evidence,
but typed integration, packaged backend/version/license qualification and Windows
GUI remain open. The saved Windows SSH endpoint returned an SSH banner, yet its
Anywhere Computer catalog failed before dispatch. Guest diagnosis/deployment
requires coordination with the VM owner's command/lifecycle lease.

Browser existing-profile continuity, PTY/ConPTY, targeted document edits and
previews, authorization-aware catalog caching/measurements, dependency-free first
installation, signed update/entrypoint consistency, and real sleep/OS-restart
acceptance remain open. This branch does not close those tasks and does not claim
beta readiness. Completed PRs must be distinguished from this blocked test-only
continuation.
