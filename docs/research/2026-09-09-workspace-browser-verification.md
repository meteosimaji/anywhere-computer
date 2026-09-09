# Workspace browser verification

2026-09-09, macOS, real headed Chromium through Playwright CLI. The browser loaded the actual packaged workspace HTML in a sandboxed iframe with `allow-scripts` and no `allow-forms`. Its parent was a disposable local MCP Apps host fixture connected to the real MCPSession and Engine. This is browser/engine integration evidence, not an actual ChatGPT/Codex acceptance result.

## Reproducible fixture

Run `uv run --offline python scripts/verify_workspace_browser.py` from the repository. It prints a loopback URL and temporary fixture path. Open the URL in a browser. Ctrl-C closes the engine and removes the temporary fixture. The program starts no persistent service, reads no native credentials, and does not expose terminal tools. Both runs in this verification were stopped and the task browser was closed.

Requests require an exact Host header. Tool requests additionally require the parent origin and JSON content type, have bounded bodies and request timeouts, and restrict path operations to the disposable files directory. Foreign-origin requests and paths outside that directory were rejected in live checks. The fixture must not be adapted into a production server without a separate design/review.

## Observed results

| Scenario | Evidence |
| --- | --- |
| Initial resource and directory list | Real MCP negotiation and workspace result populated four fixture files in the iframe. |
| Text save | Browser edited notes.txt and pressed Save; direct disk read exactly matched the submitted text with original CRLF endings. Verified again after final UI fixes. |
| Concurrent external edit | After initial read, the fixture file was modified directly; browser Save was rejected and the external bytes remained unchanged. Draft remained visible. |
| Unsaved navigation | Back opened the discard dialog. Explicit discard returned to the directory. |
| Partial read | 250-line fixture initially showed 200 lines with `textarea.readOnly=true`. Load more produced 250 lines and `readOnly=false`. |
| Path button and keyboard | Both Open and Enter loaded the selected path after removing the form-submit dependency. |
| HTML content | The exact `<script>window.fixtureInjected=true</script>` appeared as textarea text; the frame's fixtureInjected flag remained unset. |
| PNG content | Browser image decoding completed with naturalWidth=1 and naturalHeight=1 for the 1-pixel fixture. |
| Shared setting | Read limit changed to 300 via the UI. Opening Settings again returned 300; a read-only SQLite connection confirmed the persisted value. |
| Small viewport | Settings and editor were inspected at 420×900. Controls and labels remained visible; long paths wrapped in metadata and scrolled in the input. |
| Console | Corrected page finished with zero errors and zero warnings. |

## Bugs found and fixed

1. The path entry used form submission. A sandbox that does not grant `allow-forms` blocked submission before the handler could open the path. A button action and Enter handler now call the same navigation path without form submission or additional iframe permissions.
2. The generic hover style overrode the primary button background, leaving white Save text on a very light background. An explicit primary hover rule retains the accent background; the corrected button was visually inspected while hovered.
3. The new `workspace_open` presentation tool was missing from the newly created `files` setup profile. It is now included, with a scope test; existing saved profiles are not silently broadened, and command execution/settings mutation are not added to the files profile.

Screenshots are local development artifacts under `output/playwright/`: workspace-conflict.png (before hover correction), workspace-settings-mobile.png, and workspace-editor-mobile.png (after correction). They contain only synthetic data and temporary paths. CLI transcripts live under ignored `.playwright-cli/`.

## Remaining acceptance work

Actual ChatGPT/Codex host rendering and actions, real remote-device UI selection, document preview UI, long directory pagination, display-mode changes, and OS-specific visual/accessibility testing remain unverified. An easy initial setup application, runtime distribution without manual uv installation, permission onboarding, and startup/recovery controls remain product requirements.

The existing setup implementation has reusable configuration, credential, native-login, device-registry, and startup components. The missing integration is a host-side setup controller with explicit stages and observable results, without terminal input/getpass. A conversation widget cannot bootstrap the host application before the MCP connection exists. OS permissions, native credential storage, and service registration remain local host actions rather than remotely callable generic settings mutations.

## Visual redesign requested during verification

The user requested a visual direction like the Apple online store. The official Japanese store was visually inspected as a reference. No site HTML/CSS, logos, product images, or fonts were copied. The workspace was independently restyled with a neutral light-gray canvas, white cards, large headings, blue actions, and CSS-only illustrations indicating file types (not file-content thumbnails). The renderer still reads a file only when it is opened. Settings now use separate cards, with responsive layouts, host-controlled dark colors, and reduced-motion rules.

The redesigned UI was exercised in headed Chromium: a file card opened the editor; a save produced the expected CRLF bytes on disk; the settings screen accepted a read-limit change; 420px and desktop layouts were visually inspected. A host-context message switched to dark appearance. The connected-state color was adjusted for dark contrast and Japanese heading wraps were refined. Final page console contained zero errors/warnings. Node recovery regression remained passing. A separate read-only review found no missing element IDs/event targets or HTML execution APIs in the new renderer.

Final screenshots: `output/playwright/workspace-store-desktop.png`, `workspace-store-settings.png`, `workspace-store-mobile.png`, and `workspace-store-dark.png`. These are actual browser renderings against disposable data, not image-generation mockups. The earlier screenshots in this record represent the prior visual design.

Final source validation: pytest 490 passed / 5 skipped (74.62 seconds); Ruff passed; strict mypy passed for 53 runtime modules plus the browser fixture, and Windows-target mypy passed for the runtime. Plugin Creator validation passed. Reinstalled `0.1.0-alpha.1+codex.20260909014600`; every plugin file, all 53 Python modules loaded from its version-specific uv runtime, and the redesigned HTML matched the source. The development browser and final fixture server were stopped. Repository publication and official-host acceptance remain separate outstanding gates.
