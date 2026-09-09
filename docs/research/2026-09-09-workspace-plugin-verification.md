# Workspace UI and plugin verification

Development alpha, 2026-09-09. This is not a completed product or a real ChatGPT/Codex UI acceptance test.

Implemented independently using packaged HTML/CSS/JavaScript and the existing Python MCP server. No runtime dependency was added. The `workspace_open` tool supplies a presentation request; file access continues through separately authorized tools. UI resource discovery requires negotiated MCP Apps support and the current workspace tool grant. Only the exact content-addressed packaged resource can be read. Text-only clients retain a tool response without UI metadata.

## Verified

- Plugin Creator manifest/companion-file validation and skill quick validation pass. Marketplace `personal` resolves to this repository; repository remains private.
- Wheel content, checksums, all 53 Python runtime modules, and the HTML asset match current source in the installed version-specific uv runtime.
- Development version verified in this earlier record: `0.1.0-alpha.1+codex.20260909012358`.
- Full pytest before the final JavaScript review fixes: 489 passed, 5 skipped. After adding the JavaScript regression wrapper and final review fixes: workspace/package subset 5 passed.
- Ruff and strict mypy pass (53 modules). Windows-target mypy passed; subsequent runtime changes were HTML/JavaScript only.
- Real packaged JavaScript executed under Node with a minimal DOM and host message fixture: malformed write results retain the mutation guard; mismatched or malformed operation lookup results do not unlock it; running operations remain guarded; a matching completed record restores the submitted text/hash. Recovery queries do not resend writes.
- Additional fixture cases reject device discovery/selection operation-ID mismatches and refuse writes before dispatch when operation lookup permission is absent.
- MCP tests cover UI negotiation, fallback, grant revocation, exact resource URI matching, and presentation without reading/creating the requested file.

## Not verified / incomplete

- Node host/DOM fixtures are not browser rendering, actual host bridge interoperability, visual acceptance, or end-to-end save testing in ChatGPT/Codex.
- The UI remains experimental: image/text/extracted-document previews, one-level folder listing, text editing, and three shared settings. No formatted HTML/Markdown rendering, Office layout preservation, desktop controls, or setup wizard.
- The current plugin needs uv. Opening an app and approving OS prompts alone is not yet sufficient. PRODUCT.md records the complete easy-setup acceptance conditions.
- No OS permission, startup registration, production hosting, general publication, or official directory submission was performed in this change.
- Installed plugin pickup must be tested in a new Codex task; the running task may retain earlier tool definitions.

## Subsequent browser verification

The browser fixture was exercised after this record. See [browser verification](2026-09-09-workspace-browser-verification.md) for actual browser/engine results and the path-entry/hover fixes. This does not replace the remaining real ChatGPT/Codex host acceptance gate.
