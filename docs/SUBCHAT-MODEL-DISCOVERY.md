# Dynamic ordinary-Chat model discovery

Status: source investigation and live UI observations; not an implemented API.
Model names must not be hard-coded. A preferred test model is not a product default
or evidence that another account can select it.

## Upstream implementation inspected

At CoS revision `c5ab88714d3bdfa7acc5861aa1c3903e0263eaa6`,
[`extension/content.js`](https://github.com/totec448-spec/chat-on-steroids/blob/c5ab88714d3bdfa7acc5861aa1c3903e0263eaa6/extension/content.js)
elects an idle, empty ordinary-Chat composer for catalog inspection, fences the
page epoch and deadline, and avoids generation and attachments. Its
[`extension/chatgpt-dom.js`](https://github.com/totec448-spec/chat-on-steroids/blob/c5ab88714d3bdfa7acc5861aa1c3903e0263eaa6/extension/chatgpt-dom.js)
inspects each version and verifies restoration of the original model/effort and
picker closure. This is not a public OpenAI model-list API: `readPickerState`
exchanges nonce-bound messages with a MAIN-world helper. Copying the outer menu
loop alone would omit that dependency and would not produce a working catalog.
No upstream code is copied here.

CoS [issue #311](https://github.com/totec448-spec/chat-on-steroids/issues/311)
reports discovery remaining in Loading despite a connected browser. Connectivity
must therefore remain distinct from catalog readiness. That report is not a
locally reproduced Anywhere defect.

## Observed surface and implementation requirements

The current account's ordinary-Chat picker exposed model versions and a separate
power control. The specific labels observed are temporary account observations,
not stable IDs. A selected power label is not proof that all other positions are
available or have the same meaning for another version. A retirement notice was
visible on one version, further demonstrating why a static list is unsuitable.

The adapter still to be implemented must:

- Discover model/effort pairs from the active account; distinguish display labels,
  observed identifiers, disabled choices and notices without inventing aliases.
- Bind observations to their source/account context and observation time. Expose
  unknown, pending, failed and stale results rather than an endless Loading state
  or a successful empty catalog. A retained older catalog must be marked stale.
- Inspect an owned empty ordinary Chat, preserving drafts, attachments and ongoing
  generation elsewhere. Restore and verify any changed selection before success.
- Re-discover before using an unavailable saved choice; report its disappearance
  or restriction rather than silently selecting a different model.
- Treat UI/schema changes and failed selection restoration as explicit failures.
  Do not infer provider IDs from version-name regular expressions.
- Verify the actual selected pair before submission, keep submission separate from
  result collection, and allow read-only waiting to resume while Thinking continues.

The current external Codex-app connection remains a separate unresolved transport
issue documented in `SUBCHAT-PROBE.md`. An owning-app read or a manually inspected
browser menu does not establish an Anywhere model-list tool. The existing file,
terminal and direct MCP service must continue without this optional adapter.

## Live menu traversal, 2026-09-19

An owned empty Chrome Chat was inspected through visible menu roles and keyboard
navigation. Initial selection was Latest / medium. The expanded version list
showed Latest, GPT-5.6 Sol and GPT-5.5. Each version's power control was traversed,
with the displayed status read after each transition. All three exposed these
five labels in order: Instant, medium, high, extra high, Pro (Japanese UI labels
were `Instant`, `中程度`, `高`, `極高`, `Pro`). GPT-5.5 showed a retirement notice
for October 14. These are dated account observations, never executable defaults.

Selection was restored to Latest / medium and the picker was confirmed closed;
no prompt was sent. This proves UI traversal and restoration in this environment,
not canonical provider model IDs, quota availability, generation with all pairs,
a background adapter or an Anywhere catalog API. Some unselected menu content
also contains generic locked-access text, so mere DOM presence must not be taken
as a reliable per-choice availability signal. Successful selection is stronger
UI evidence, but still does not promise a successful subsequent generation.


## Visible-menu extractor prototype

`scripts/subchat_model_menu.js` provides a read-only function for an already
opened model menu. It extracts displayed labels, separate notices, checked state
and explicit DOM disabled state. These are not provider IDs or quota guarantees.
The active menu must be unique; hidden/inert model panels are excluded. Missing,
changed or ambiguous markup returns a non-success state rather than an empty
successful catalog. This distinction matters because the live simple picker
retains its model rows inside an inert, aria-hidden panel.

A live expanded Chrome menu produced three labels and separated the retirement
notice from its model label without version-name matching. No model selection or
message submission was performed. The extractor is experimental, not registered
as a production MCP tool. It does not yet enumerate effort labels, open menus,
manage login, cache catalogs or create subchats.

The optional real-DOM test uses local HTML in headless Chrome, with arbitrary
future model names, Japanese notices, hidden/inert panels, duplicate labels,
multiple checked rows and changed markup. Run with the browser extra installed:
`uv run --extra browser pytest -q tests/test_subchat_model_menu.py`.
It skips when the optional Playwright dependency or Chrome is absent; such a skip
is not browser acceptance. On the development Mac, this test and the subchat
collector tests passed (28 tests, no skips).


The same prototype now includes `observeSubchatEffort(document)`. It reads the
unique visible keyboard control's minimum, maximum, current integer position and
its associated status description. The current UI's hidden thumb is only read
inside that visible control. Missing descriptions, non-integer/out-of-range
positions and hidden controls return non-success states. Values and descriptions
are observations, not canonical API effort IDs. A live menu reported 0..4/current
1 with a medium description; DOM tests instead use seven positions and an
arbitrary label to prevent a fixed five-level assumption. This remains current
position observation, not full traversal or verified restoration of settings.


`scripts/subchat_efforts.py` adds bounded traversal using supplied DOM-read and
keyboard-step callbacks for an owned empty Chat's already open control. It
confirms every adjacent step, collects descriptions at each position, and verifies
return to the original position and description before reporting success. A
changed range, missing key effect, transport failure or changed restored description
cannot return a complete catalog. At most 32 positions are supported by this probe.
It does not reconnect/reopen or resubmit a prompt after failure.

Tests cover dropped keys, response loss, range changes and description changes.
A real headless Chrome fixture connects the DOM extractor and keyboard callbacks,
collects seven positions and verifies restoration. This is browser-fixture
acceptance, not live ChatGPT traversal through the standalone adapter. Page
ownership, empty-composer checks and login remain caller prerequisites and are
not implemented by this helper. No production tool is registered yet.


## Dedicated browser probe entry point

`probe_subchat_catalog.py` connects these parts to Playwright. It creates a new
page in a dedicated profile, requires an empty root ordinary Chat with Chat
selected, opens the picker and reads the model rows. It collects efforts only for
the currently selected model, verifies restoration and unchanged model rows, and
confirms picker closure. Existing drafts fail the preflight without edits.

```sh
uv run --extra browser python scripts/probe_subchat_catalog.py --profile /absolute/dedicated/profile
```

Use only a dedicated authorized profile after closing its other browser process;
the probe owns and closes its browser context. Do not use the normal Chrome
profile. The optional `--headed` flag makes this probe visible. Login is not
performed automatically. Browser/connection errors return `probe_unconfirmed`,
without raw peer text, and no prompt is submitted. This entry point has been
verified against a locally served browser fixture (including a preserved draft),
not authenticated live ChatGPT. An account login is still needed for that gate.
The reported efforts are explicitly for the selected model only, not every model.


## Standalone failure-path acceptance, 2026-09-20 JST

The actual CLI was run with disposable unauthenticated profiles against ChatGPT.
It did not report catalog success, submitted no message, and left no processes
referencing the test profile after exit. Initial output was an unconfirmed timeout.
A focused recheck found an HTTP 403 and a waiting-page title; this does not prove
why the service rejected that request. No challenge bypass was attempted.

The probe now returns `page_unavailable` with the observed HTTP status before
waiting for model controls on a failed navigation. Repeating the corrected CLI
produced `page_unavailable`, status 403, submitted false, and no surviving profile
processes. Visible Japanese/English Login buttons yield `login_required` (verified
with browser fixtures). Other unmatched UI still fails as unconfirmed; no universal
locale or headless compatibility claim is made. This failure-path acceptance does
not establish authenticated catalog extraction. The separate headed login profile
was left untouched for the user's pending login.
