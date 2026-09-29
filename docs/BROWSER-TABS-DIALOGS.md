# Isolated browser tabs and explicit dialogs

Source implementation and synthetic browser acceptance, 2026-09-30.
This does not establish installation in a resident engine or receipt by an ordinary Chat client.

## Competitor and upstream basis

[Playwright MCP](https://github.com/microsoft/playwright-mcp#tools) exposes tab management
and dialog handling alongside semantic browser actions. Those were concrete omissions in the
single-tab Anywhere Computer adapter. This slice adds equivalent task primitives with existing
connection ownership, typed schemas and durable operation receipts; it does not claim overall
superiority to other browser agents.

[Playwright pages](https://playwright.dev/python/docs/pages) documents multiple pages in a
browser context, context page events for new tabs/popups, and background interaction without
bringing a tab to the front. Anywhere Computer uses its pinned Playwright 1.58.0 and keeps one
separate ephemeral browser/context per session. Additional tabs share that context's cookies,
but never attach an existing profile or another session.

[Playwright dialogs](https://playwright.dev/python/docs/dialogs) explains that registering a
dialog listener disables default dismissal and that unresolved modal dialogs can stall page
actions. The adapter therefore records dialogs before responding, interrupts blocked page waits,
and provides separate observation/response paths. Page text never authorizes its own acceptance.

## Tool contract

| Tool | Input identity | Result or effect |
| --- | --- | --- |
| `browser_tabs` | Owned `session_id` | Exact tab IDs, safe URL routes, pending dialog metadata, capacity and rejected-popup count. |
| `browser_tab_open` | Owned `session_id` | One blank tab in the same ephemeral context. List tabs after an unknown creation receipt. |
| `browser_tab_close` | Owned `session_id`, `tab_id` | Closes only that tab; closing the final tab releases browser and driver. Unsaved state is discarded. |
| `browser_dialogs` | Owned `session_id`, `tab_id` | Observed dialog ID, type, message, default value and pending/response-unconfirmed state. Does not wait on the page action lock. |
| `browser_dialog_handle` | Owned `session_id`, `tab_id`, observed `dialog_id` | Explicit `accept`/`dismiss`; optional `prompt_text` only for accepting a prompt. Returns response receipt separately from page observation. |

`browser_close` continues to close the entire owned session. There is no implicit selected tab.
The session supports at most eight registered tabs; additional popups are closed without
beforeunload handling and counted. Metadata text is bounded. Each tab has its own snapshots,
frames, console/network cursors and dialog identity. Existing role/label/frame validation applies
unchanged to every tab. Ended/stale/cross-session/wrong-owner IDs are rejected.

If an excess popup cannot be closed, the adapter closes only its owning session.
The session remains in the registry and blocks engine updates until browser/driver
cleanup is confirmed. `browser_tabs` reports `state="closing"` and
`cleanup_in_progress`; new input is refused while closing. A bounded caller wait
does not cancel Playwright's shared driver-stop operation or falsely report that
cleanup finished. Reinspect the same session after an uncertain close receipt.

Snapshots list tabs so an action that opens a popup has a discoverable destination. A pending
dialog returns `state="dialog_open"` with no usable snapshot ID; no DOM/image result is fabricated.
Page work wakes when any dialog in that ephemeral context opens and also has a bounded wait.
An early popup alert can block its opener; the opener returns the popup in `tabs` without
pretending the dialog belongs to the opener. Actions already dispatched keep
their unknown outcome; canceling a local wait does not assert that the browser action was undone.

A dialog response is claimed before dispatch. An uncertain response cannot be sent again even
under a new operation ID. A later bounded observation can establish that the modal has closed;
`last_dialog_response.outcome` still stays `unconfirmed`. A confirmed response followed by a failed
page observation returns the confirmed receipt and `observation.state="unavailable"` instead of
implying the user should answer again. Existing operation-ID recovery never replays the action.

The tools address JavaScript `alert`, `confirm`, `prompt` and `beforeunload` dialogs. Browser-native
permission, print and authentication windows are not claimed as supported. Tests below exercise
alert, confirm and prompt; beforeunload-specific acceptance is not included.

## Verification

`tests/test_browser_tabs_dialogs.py` runs disposable headless Chrome against a synthetic loopback
HTTP site. It verifies same-session cookie sharing, separate-session isolation, exact popup IDs,
wrong-owner/stale references, a lost new-tab receipt, eight-tab capacity, excess-popup cleanup,
explicit confirm accept/dismiss, prompt text, alerts, early popup dialogs, observation/dialog races,
final-tab cleanup, lost response recovery, and confirmed response with unavailable post-observation.
The engine test reuses the original operation ID and checks an unknown click is never replayed.

`tests/test_http_capability_acceptance.py` discovers and invokes all five tools through an
OAuth-authorized local HTTP MCP fixture and checks the page after explicit dismissal. Existing
browser regression tests include navigation, locators, frames, open shadow roots, files and
startup/cancellation cleanup. Schema/annotation checks, consent descriptions and real stdio MCP
discovery cover the newly registered contracts.

No selected Chat account, private conversation, production browser profile or foreground app is
used by these tests. Acceptance here is macOS Chrome/Playwright; other installed browsers and
operating systems require their corresponding CI/runtime checks.
