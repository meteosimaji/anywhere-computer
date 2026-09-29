# beta.33 code-quality audit

The full-file review began at source commit `c9e1885` on 2026-09-30. The
subsequent CI repairs changed browser shutdown code, Windows test scheduling
and tests. The beta.33 wheel is built from clean source commit `df1d8d3`,
recorded in its release manifest. The [file inventory](code-quality-audit.json)
records the current SHA-256, area,
line count, review outcome and short finding for every included file. It is a
review record, not a claim that testing can prove the absence of defects.

## Scope and method

| Area | Files | Inspection |
| --- | ---: | --- |
| Core engine, browser, native GUI and desktop UI | 82 | Full-file review of ownership, observation, input, cleanup, UI and tests |
| Network, authorization, delegation and media transport | 140 | Full-file review of boundary checks, wire receipts, recovery and tests |
| Runtime, packaging, CI and generated plugin configuration | 142 | Full-file review of build, startup, support files and tests |
| Subchat and its test suites | 68 | Full-file review of send, recovery, account isolation, queue, notification and tests |
| Total | 432 | Every current file hash matched a review record after integration |

The inventory includes tracked Python, JavaScript, CJS, Rust, Swift, shell,
HTML/CSS, workflow and tool configuration files. The two tracked JSON files
`scripts/ci/windows_test_costs.json` and
`tests/fixtures/anywhere-fixture-device-realm.json` are data fixtures and were
excluded from the code-file count. Documentation, lockfiles, generated wheels
and downloaded dependencies were outside the full-file review; packaging and
archive integrity were checked separately. The reviews read affected definitions
and their callers, contracts and tests, then inspected later integrated diffs.

## Concrete corrections

- Browser and native GUI cleanup now retain owned resources and update blockers
  until process termination is confirmed. Cua and Peekaboo contract discovery
  share bounded catalog pagination and reject duplicate or repeated pages.
- A macOS CI run exposed that a five-second browser close deadline matched the
  caller's five-second receipt deadline. The internal owned-browser close now
  has a longer bound, while the caller still returns an unknown receipt after
  five seconds and retains the resource until cleanup actually finishes.
- Subchat protects unclaimed sends after uncertain page cleanup, preserves later
  human edits, reports worker failures separately from provider receipts, and
  reaps an owned notification subprocess when its controller is cancelled.
  A stale test-controller reference after gateway restart was also corrected.
- Startup and Subchat file inputs reject FIFO sources before blocking reads.
  SSH child shutdown drains admitted delegated file workers. Owner-pipe JSON
  rejects nonfinite numbers and excessive nesting as protocol errors.
- Document previews now arrive as native MCP images over direct, HTTP, SSH,
  device routing and recovery paths. Workspace checks the image summary,
  byte count, MIME type and SHA-256 before displaying it. Direct clients that
  consumed the former `data_base64` field must migrate to image content;
  [DOCUMENTS.md](DOCUMENTS.md) records that contract.
- Desktop UI state handling was simplified, inherited phase keys are rejected,
  browser target validation is reused, and obsolete engine help text was
  corrected. The distributed guest test bundle now includes its dependent
  fixtures. Windows test sharding checks selected and finished test IDs. The
  native Task Scheduler XML check runs in a serial preflight group after it
  reached its 30-second bound under the two-worker shard load. The partition
  still includes this test exactly once.
- The macOS shared-runtime suite remained in its test step for over 30 minutes
  after earlier runs had reported failures before completion. The suite now
  stops after the first failure so CI can expose its traceback. Successful
  runs still execute the full collected suite. Another run and Windows shard 4
  remained in test execution well beyond their prior durations, while the
  equivalent local macOS suite passed. Shared-runtime and parallel Windows
  shards now log each test name and dump/exit a test that runs beyond five
  minutes. Verbose macOS logs identified a browser cookie test failure in one
  worker while a manual Subchat send test hung in the other. The 70 tests in
  those two files now run serially before the remaining parallel suite. The
  collection count still covers every test once. This isolates the observed
  overlap; the underlying cause remains unconfirmed until CI reruns it.

The [changelog](../CHANGELOG.md) lists the beta.33 feature work as well as
these repairs. No confirmed, unpatched defect remained in the reviewed files
at the audit cutoff. The review record retains hypotheses that lacked a
reproduction or an upstream contract; it does not change behavior to satisfy
those hypotheses.

## Verification at the audit cutoff

| Check | Observed result |
| --- | --- |
| `uv run --locked ruff check src tests scripts` | Passed |
| `uv run --locked mypy` | Passed, 159 source files |
| Serial timing-sensitive pytest group | 45 passed, 17 OS skips |
| Remaining pytest suite with four workers | 2,778 passed, 35 skips; 110.69 seconds |
| Browser control and tab/dialog suites | 51 passed; the delayed-close regression failed before its repair |
| Plugin/release candidate tests | 21 passed against the rebuilt beta.33 wheel |
| README references, release tag availability, diff check, ZIP integrity | Passed |
| Subchat final related suite | 953 passed, 1 OS skip |
| Document image path | 43 Python tests plus Workspace UI tests passed after integration; the expanded source fixture also passed real renderer and HTTP/SSH image tests |

The serial and four-worker groups exclude each other, except that the separate
21 plugin/release tests repeat cases from the broader group. Test process
counts are therefore not added together as a unique-case total. The full suite
checks local behavior; GitHub macOS/Windows/Linux CI and installed-client
acceptance are separate gates. The first PR Windows smoke run exposed a test
setup error: the synthetic macOS notification test changed global
`sys.platform` before constructing a Windows ledger. Commit `c9e1885` creates
the ledger first; its 31 local tests and the next Windows smoke job passed.
That run then exposed an iframe test race: `page.frame(name)` can be absent
while the second iframe attaches. The test now waits for each frame's body
through Playwright's `FrameLocator` before asserting ownership and stale
snapshot behavior. The next Windows smoke and all four test shards passed. A
subsequent macOS run exposed a browser-close deadline that matched the caller's
receipt deadline. The delayed-close regression failed before the repair and
passed afterward. A later Windows shard run timed out in native Task Scheduler
validation; the serial preflight change passed on Windows in 0.30 seconds. A
macOS run stayed in the suite for over 30 minutes. A subsequent run and Windows
shard 4 also remained in test execution well beyond earlier durations. The
bounded diagnostic run passed all Windows shards but exposed a cookie test
failure and concurrent Subchat test hang on macOS. Serial browser/Subchat
preflight and the remaining parallel suite await a new macOS CI result. The
beta.33 PR Quality run is required to confirm the final combined candidate on
all three operating systems.

## Remaining product and operational gates

| Issue | Unfinished acceptance |
| --- | --- |
| [#180](https://github.com/meteosimaji/anywhere-computer/issues/180) | Child-specific grants have local and loopback SSH/HTTP coverage. Confirm cross-host expiry, revocation, reconnection and a verified ordinary-Chat child identity before claiming remote deployment. |
| [#181](https://github.com/meteosimaji/anywhere-computer/issues/181) | Local Codex/Claude mailbox behavior is tested. Ordinary Chat has no automatic inbox/model-turn wakeup or linked usage record. |
| [#185](https://github.com/meteosimaji/anywhere-computer/issues/185) | Confirm the full save path on a physically remote device and remaining Windows/Linux account/browser and stop/steer behavior. |
| [#198](https://github.com/meteosimaji/anywhere-computer/issues/198) | Physical/platform authenticator registration and public HTTPS consent require owner presence. The owner requested debugging work while asleep. |
| [#219](https://github.com/meteosimaji/anywhere-computer/issues/219) | Measure repeated Windows CI stability and elapsed time against the 998-second baseline. Local tests alone cannot satisfy this. |
| [#220](https://github.com/meteosimaji/anywhere-computer/issues/220) | Six required PR Quality checks are configured. Observe this candidate held until checks pass, then verify the protected `main` release and attestations. |
| [#221](https://github.com/meteosimaji/anywhere-computer/issues/221) | The direct Codex Computer Use call still reports `unsupported_execution_context` without an owning Codex turn. A supported cross-client execution API and a model-identified ordinary-Chat action have not been shown. Anywhere's own GUI/browser tools use separate grants. |
| [#222](https://github.com/meteosimaji/anywhere-computer/issues/222) | Native Chat search remains the public-search entry. The first fixed-task Subchat benchmark has one unconfirmed baseline send and no valid usage comparison; do not infer savings or replace that unknown send. |
| [#224](https://github.com/meteosimaji/anywhere-computer/issues/224) | The bounded owned-tab cleanup repair has local regression coverage. Repeat the macOS browser/history CI run before claiming the intermittent failure resolved. |

Other capability boundaries: the native macOS path does not yet provide general
keyboard, drag or coordinate input; Windows UIA and Linux AT-SPI are absent.
MCP media receipt has been verified, but a particular ordinary Chat model's
visual or audio perception requires a separate live observation. The
[GUI comparison](GUI-AUTOMATION-PLAN.md) names these gaps against current
primary competitor documentation without claiming overall superiority.
