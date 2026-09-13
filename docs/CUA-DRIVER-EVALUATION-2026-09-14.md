# Cua Driver isolated evaluation — 2026-09-14

## Provenance and limits

- Upstream inspected main: `72b8707cfbdfa3cec5b3f563acdd62ac42aee266`.
- Actual executed release: `cua-driver-rs-v0.28.1`, marked prerelease; target commit `d8028a7943087ee258dc1b4d19dc12a7cd27669c`.
- Artifact: `cua-driver-rs-0.28.1-darwin-universal-binary.tar.gz`.
- Observed SHA-256 matches the release asset digest: `6a0dde9732a68c139760ccc573899668757995a6e9190d80ccaad6db26bd6fe8`.
- Root LICENSE.md and driver Cargo workspace declare MIT; dependency redistribution audit is not completed.
- Executed from a temporary directory using `mcp --direct` through Anywhere Computer's DirectMCPContext. No production installation, service registration or permission grant was performed.
- `check_permissions(prompt=false)` reported host-attributed Accessibility and Screen Recording granted. Live direct capture readiness remained unprobed. This is not a new standalone CuaDriver app permission grant.

## Actual results

| Probe | Result |
| --- | --- |
| MCP startup/catalog | Succeeded without another inference model; cleanup confirmed |
| TextEdit exact window AX observation | Succeeded; a single observed call took 155 ms; screenshot explicitly omitted |
| TextEdit background Japanese/emoji input | Provider effect confirmed; independent subsequent AX value included the addition; action call 1050 ms |
| Chrome trial new-tab address field | Background input effect confirmed; subsequent AX value matched `https://example.com/`; action call 1121 ms |
| Chrome Return | Provider effect unverifiable; the selected trial tab remained New Tab after another observation; navigation not accepted |
| Independent Peekaboo screenshot observation | Image file contained the entered URL and New Tab content, but the command exited 1 with window-context title mismatch. Not a successful full observation |

These are single observations, not latency percentiles or a performance comparison. No mutation was automatically replayed. The trial document and tab remain available. Windows, browser page continuity, ChatGPT UI acceptance and a product-integrated Cua adapter are not verified by these tests.

## Evidence files

- `/tmp/anywhere-cua-input-receipt.json`
- `/tmp/anywhere-cua-chrome-input-receipt.json`
- `/tmp/anywhere-cua-chrome-navigation-receipt.json`
- `/tmp/anywhere-chrome-after-cua.json`
- `/tmp/anywhere-chrome-after-cua.png`

## Decision

Continue Cua Driver as the directive's first independent GUI evaluation candidate, retaining the existing explicit Peekaboo adapter. The structured element tokens and effect results are useful and actual native input worked. Do not promote this to default across operating systems yet. Evaluate browser-specific Playwright support separately; do not equate native address-field editing with successful browser page automation. Preserve the separation between host-attributed development execution and the distributable application's eventual OS permission identity.

Sources: https://github.com/trycua/cua/tree/72b8707cfbdfa3cec5b3f563acdd62ac42aee266/libs/cua-driver and https://github.com/trycua/cua/releases/tag/cua-driver-rs-v0.28.1
