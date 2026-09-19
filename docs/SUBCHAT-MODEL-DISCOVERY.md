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
