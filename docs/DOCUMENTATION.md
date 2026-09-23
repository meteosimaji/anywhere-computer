# Documentation sources of truth

README files are entry points, not independent feature inventories. Keep a fact
at its owning source below, then link to it. The English README owns the short
source-install walkthrough; the Japanese entry links to that command sequence.
Translations may explain it, but must not introduce a separate release/status table.

| Information | Authoritative source | Derived or explanatory material |
| --- | --- | --- |
| Source package version | `src/anywhere_computer/__init__.py`, selected by Hatch in `pyproject.toml` | README generated blocks and package metadata |
| Plugin version notation | `plugin_version()` in `scripts/package_plugin.py` | Generated Plugin manifest, bundle receipt and README block |
| Python requirement / dependencies | `pyproject.toml` / `uv.lock` | README Python requirement; install guides |
| Published build and downloads | [GitHub Releases](https://github.com/meteosimaji/anywhere-computer/releases), immutable assets and release evidence | The checkout does not establish what was published |
| Actual installed runtime | `anywhere status` / authenticated `computer_status` | Neither source version nor a README proves what is running |
| Available tools / command options | Runtime catalog/schema; CLI `--help` | [Operations](OPERATIONS.md), [MCP clients](MCP-CLIENTS.md), feature guides |
| Setup, update, routing | [Setup](SETUP-CONTROLLER.md), [updates](UPDATING.md), [routing](DEVICE-ROUTING.md) | README links to these contracts instead of copying their detailed steps |
| Experimental subchat usage and limits | [Subchat guide](SUBCHAT-PROBE.md), matching source/tests | README gives an overview, not another capability matrix |
| Opt-in HTTP-only generation handoff | [HTTP-only generation contract](SUBCHAT-HTTP-ONLY-GENERATION.md), matching source/tests | No stored credentials, browser fallback, or automatic replay |
| Intended future work | [Product roadmap](PRODUCT-ROADMAP.md) | A milestone is not an implemented feature |
| Historical evidence | Local-only archive of dated acceptance, audit and research records | Do not treat old results as current acceptance or republish private evidence |
| Required checks | [Quality workflow](../.github/workflows/quality.yml) | README links to the workflow instead of maintaining another full command list |

## Updating the entry points

Historical audit and research records are kept outside this repository in a local
archive. They are not part of source distributions or linked from the public
guides. Current behavior is established by the source, tests and fresh runtime
observations; release behavior is established by published assets and receipts.

The shared README reference blocks come from existing package metadata and the
small guide index in `scripts/update_readme.py`. Do not hand-edit generated blocks.
No new version file or parallel capability database is introduced.

```sh
uv run --locked python scripts/update_readme.py
uv run --locked python scripts/update_readme.py --check
```

CI checks both languages for exact generated content and existing local file
links. It does not fetch GitHub releases, validate external sites/Markdown anchors,
or infer feature completion from filenames. Release truth stays with the release
assets, so publishing does not require replacing a hard-coded "latest" version in
both README files. Package consistency tests separately verify the bundled source,
version mapping and hashes.

For a behavior change, update the owning feature guide and its evidence in the
same PR. Change the README overview only if the product description or entry path
changes. Report the code/test evidence separately from live Chat, hardware and
published-artifact acceptance. Reviewers must still check semantic claims; a
link/version check cannot detect every misleading or forgotten sentence.
