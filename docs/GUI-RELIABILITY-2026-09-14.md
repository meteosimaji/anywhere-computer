# GUI reliability follow-up — 2026-09-14

## Implemented corrections

The public MCP response builder previously treated provider `is_error=true` as an
MCP error only for `mcp_call` and `codex_plugin_call`. Typed GUI calls could return
`isError=false` while their recorded data explicitly reported a failed capture or
input. Their image content also remained inside JSON instead of becoming native
MCP image items. Both omissions are corrected for `gui_observe`, `gui_type`,
`gui_click`, and `gui_key`.

The operation ledger still records completion of result collection separately
from provider success. A successful `operations_get` remains a successful lookup;
its inner result retains the original provider error. No error is turned into a
successful action and no mutation is retried automatically. The stored result is
not modified by wire projection.

The typed observation parser now reads only the normalized, bounded content that
is actually returned to the client. Truncated observations, multiple snapshot
references, and duplicate actionable element references cannot create an action
reference. A failed observation explicitly returns `action_ready=false`. Earlier
observations in that session remain invalidated. Ownership, 60-second expiry,
exact-window targeting, serialization and consumption-before-dispatch are kept.

## Regression evidence

`tests/test_gui_wire_results.py` introduces twelve cases. Before their respective
corrections, the first nine tests failed on the missing MCP error/image projection,
and the next three failed on truncated or ambiguous observations authorizing input.
After both corrections, 73 related GUI, result-envelope, image and plugin-error
tests passed. Ruff and strict mypy over 97 source files passed. These are local
macOS development results, not acceptance of arbitrary applications or Windows GUI.
The integrated branch's final full-suite and CI results are reported separately.

## Live Chat connection: observed blocker

The existing Peekaboo 4.3.4 evaluation executable was used through a new direct MCP
session. Its `window` tool found the designated synthetic TextEdit document,
window ID 10373, title `Anywhere GUI 日本語 42`. However, it classified this window
as pixels-only with `no_matching_accessibility_window`. Two typed observations
reported `AX tree incomplete`; neither returned an actionable observation.

The provider's permissions tool reported Screen Recording, Accessibility and Event
Synthesizing all granted. This report does not prove that the selected application's
AX tree is responsive. No permission change, app restart, focus fallback, OCR-based
mutation or GUI input was performed. This is still a live GUI acceptance failure,
not fixed merely by correcting its error envelope.

Live engine: version `0.1.0a9`, runtime
`edfac0c9fee95cb506624f238d33a68d9bde86f4e8469bd708832d24b26bb23a`, instance
`51fb6c4803364738ad91a5e4fb7d67c8`.

Evidence operation IDs:

- Direct session open: `c9f05adc833b4e799a07ef46495a5083`.
- Window listing: `b66c47dbb35b4ef78b22574ec4ef3fc9`.
- Failed observations: `71a711840cb04b05924f0b192f9b55db` and
  `d35022a5e44e4966855ad9dcb518a890`.
- Permission status: `e0390da341ed4b25a046aac9d2d4965a`.
- Original failed result recovered: `b4da45d7950e441f9e68cca5372d4b41`.
- Session close: `52e6f59df7e144e789a6bbb4f2ccff13`, cleanup confirmed.

### Screenshot-only follow-up

A second, independent direct MCP session used the provider's documented `image`
tool with the same app and exact window ID, background capture, and no input.
Operation `2329266599824c4385b3977d6fb653eb` returned a native PNG image successfully;
the image visibly contained `Anywhere GUI 日本語 42 ✅`. The PNG SHA-256 was
`3a250daf3343435059ab9c53cc87abf89e8434b40c97573b6eedf781fe0b12d3`.
Thus window pixel capture worked while AX observation did not. This narrows the
observed failure but does not establish its underlying cause or authorize input
without a suitable fresh action target.

## Windows boundary

The registered Windows device's catalog request failed before dispatch in operation
`3a9259068d1442ebbd3d0b29c69ed085`. A read-only probe of its configured loopback SSH
endpoint received an SSH banner. Therefore absence of an SSH listener is not the
cause established by this observation; authentication, remote launch, engine state
and protocol readiness are not yet distinguished. No guest command or VM lifecycle
change was made by that probe. The VM remains subject to its owner's command-lease
and lifecycle coordination rules. A historical VM thread is not a current lease.

## Release boundary

These changes do not implement or qualify a Windows GUI backend, restore the
unavailable TextEdit AX observation, finish existing-login browser support, or
complete PTY/ConPTY, document editing/preview, catalog caching, signed installation
or sleep/restart acceptance. They do not update the installed Chat engine or its
cached public schema. Keep those gates open until the actual target distribution
and live clients satisfy the development directive.

## Native macOS feasibility, 2026-09-21

An independent Swift 6.3.3 probe used AppKit/ApplicationServices directly, without
Chrome, Peekaboo, model inference or clipboard input. Existing Accessibility
permission was available; no permission prompt was requested. A new synthetic
TextEdit document `/tmp/ac-native-gui-20260921.txt` was opened with `open -g`.
The probe required one TextEdit process, an exact unique window title, one editable
AXTextArea in a bounded tree, and the expected current fixture value.

Observations, in order:

1. AXValue and AXSelectedText setters returned success and exact Japanese/emoji
   readback. This alone did not prove persistence. The first value appeared on
   disk later; the next value did not appear during a bounded 20-second check.
2. The observed Save menu was named `保存…`, not `保存`. Its AXEnabled value was
   false while the app was in the background. An initial AXPress returned success
   despite this. Subsequent probes rejected disabled menu actions. No save dialog
   was assumed or acted upon.
3. After checking that the target window and field were the application's focused
   window/element, the probe selected the synthetic content and sent Unicode
   keyboard events directly to that process. AX readback exactly matched
   `Anywhere native 入力経路 44\n`.
4. A Command-S event pair sent to the same process persisted that exact UTF-8
   content. The file SHA-256 was
   `a17f9b99d61e07abef04a9934625c486b2c168535c835efe2a73220672540e57`.
   The frontmost process ID remained unchanged in the mutation probes.

This is live TextEdit feasibility evidence, not an integrated native provider or
a universal background-input guarantee. The temporary probes were sequential,
not atomic with concurrent user interaction, and do not establish PID-reuse
protection, reference leases, stale-window rejection, screenshots, Windows UIA
or distribution signing. The document remains available for inspection.

Implementation requirements carried forward: preserve structured AX observation,
use explicit process/window/element identity, distinguish AX set-value from input
semantics, inspect enabled actions, and verify the resulting file independently.
An acknowledged action or matching text view must never substitute for a save
postcondition. Background process input needs explicit capability reporting and
interference checks; do not silently fall back to frontmost/global keyboard input.
