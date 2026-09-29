# Temporary Chat read tab cleanup

Issue [#224](https://github.com/meteosimaji/anywhere-computer/issues/224) recorded
one macOS failure in which a history read left its temporary tab open. The saved
answer/interruption and original user tab must survive cleanup failures.

## Observed behavior and limits

The first attempt of [Quality run 36375126706](https://github.com/meteosimaji/anywhere-computer/actions/runs/36375126706/attempts/1)
timed out in both the initial `page.close()` and its background retry. The log
does not contain Chrome protocol events, so it cannot establish whether that
specific failure was runner scheduling, a navigation race, or a lost command.

The installed, pinned Playwright 1.58.0 implementation establishes a narrower
cause: `server/page.js` sets `_closedState = "closing"` before the first close.
Subsequent `page.close()` calls wait for `_closedPromise` without resending
`Target.closeTarget`. The previous retry therefore could not recover a Chrome
target that acknowledged the initial command but remained open. Cleanup errors
other than `TimeoutError` could also replace a successful read or its original
exception.

Upstream reports describe close/navigation races, including a successful CDP
acknowledgement without target destruction:
[Playwright #42366](https://github.com/microsoft/playwright/issues/42366) and
[the upstream tracking issue #42068](https://github.com/microsoft/playwright/issues/42068).
These reports concern other Playwright/browser versions; they support the
failure model, not a proven diagnosis of our original CI incident.

On 2026-09-29, an isolated macOS probe with Playwright 1.58.0 and Chrome
154.0.8037.58 ran two concurrent contexts with ten reload/close rounds each.
Protocol records showed 20 initial `Target.closeTarget` commands, 20 success
acknowledgements, and 20 target-detached events. Every original tab survived;
the original intermittent race did not reproduce. All page content and
credentials in the probe were synthetic fixtures.

[Quality run 36629209398](https://github.com/meteosimaji/anywhere-computer/actions/runs/36629209398)
reproduced the remaining failure on macOS: an authenticated catalog read
completed, then both `page.close()` and the fallback's attempt to attach a new
CDP session timed out. One `https://chatgpt.com/` temporary tab remained beside
the untouched original tab. The log does not prove whether Chrome stopped
responding or Playwright could not attach after it had marked the page closing.

## Cleanup contract

Before closing a read-only bootstrap tab, the reader obtains a public CDP
session and target identity for its exact temporary page. Generation-abort
closes start immediately so a delayed POST is blocked as soon as possible. The
reader waits up to five seconds for ordinary `page.close()`. If that page
remains open, it sends one `Target.closeTarget`
through the already-attached session. If target preparation failed, it retains
the older late-attachment fallback. It waits for the page's close event, not
merely the command acknowledgement. Target preparation and fallback each have a
five-second deadline; detaching a still-live CDP session has a separate
one-second limit. No shared context, original tab, account profile, or
unrelated target is closed.

Normal reads wait for this bounded cleanup. Caller cancellation preserves the
same bounded cleanup task. Failed cleanup logs only error class and closure
state, without exception text, URLs, account headers, cookies, or Chat content.
An unresponsive browser can still retain the temporary tab after the deadline;
this is reported as cleanup failure and never treated as confirmed closure.

## Verification

`tests/test_subchat_tab_cleanup.py` covers prompt closure, timeout recovery,
driver errors, acknowledged-but-unclosed targets, unresponsive attach/detach,
an attachment that fails only after page closure begins,
and caller cancellation. Real Chrome tests inject a stalled initial close and
require the fallback to destroy only the temporary target before returning the
read result. A four-context load test runs 24 history bootstrap tabs and retains
the original-tab equality assertion after every read.

The original implementation fails the new successful-read/driver-error case
and the real-Chrome fallback/isolation case. The baseline check used an isolated
source copy with only unused timeout constants supplied for test setup; no
cleanup behavior was altered.

Run the focused suite with the locked runtime:

```sh
uv run --locked pytest -q tests/test_subchat_tab_cleanup.py \
  tests/test_subchat_http_history.py tests/test_subchat_http_catalog.py
```

The local focused suite passed 159 tests. A four-worker run adding the browser
backend and deletion suites passed 206 tests. The full Quality gate and repeated
macOS runs are still required for publication acceptance; local passing tests
do not establish that every possible Chrome close race is repaired.
