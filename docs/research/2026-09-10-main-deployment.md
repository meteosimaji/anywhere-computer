# Main integration and production deployment

Date: 2026-09-10. Implementation commit: `e9bb787`.

## Git integration

Fetched the private GitHub repository before merging. `codex/plugin-sessions`
(`6d48a87`) already contained `codex/chatgpt-plugin-bridge` (`0f60947`), including
the image relay patch (`997c03c`). The two outstanding local files were inspected
and committed as `ad1b49b`: the installed-plugin probe now honors the bridge's
pinned Codex executable resolver, with three preflight regression tests.

Merged the GitHub session branch without conflicts, then fast-forwarded `main`
to `e9bb787` and pushed it. `git ls-remote` confirmed the same commit on GitHub.
All existing local and remote branches were ancestors of main at that point.
No existing branch or worktree was deleted and no history was rewritten.

## Validation of the integrated implementation

- Full pytest: **675 passed, 5 skipped**.
- Ruff passed for `src`, `tests`, and `scripts`.
- Strict mypy passed for 60 source files.
- The official MCP SDK authenticated HTTP session fixture passed: one-shot
  counters `[1, 1]`, retained counters `[1, 2]`, reconnect and token refresh,
  native image reception, operation replay, stale-catalog rejection, grant
  isolation, explicit closure, and cleanup.
- The installed OpenAI documentation plugin completed two read-only calls in
  one session. Instrumented sends contained no `turn/start`, `turn/steer`, or
  `thread/resume`. This is not a subscription-usage measurement.
- Built the bundled wheel and standalone macOS arm64 portable archive from
  the integrated checkout. Portable runtime verification passed 2256 manifest
  entries, file roundtrip and the regex child check.

## Production switch

The registered runtime was changed from `plugin-inspect-20260910` to
`portable/main-integrated-20260910/Anywhere Computer` under the existing
application-support directory. Source, supervisor and connector runtime identity:

```
5ae13ad825cf7bde3fd8f2aa63b72ad8d58d5612c5a24946f27eddc91413070f
```

Before stopping the old service, its startup receipt, HTTP configuration and a
consistent SQLite authorization backup were saved outside the repository with
owner-only permissions. Keychain credentials were retained.
The backup is retained under
`backups/pre-main-integrated-20260910` in the application-support directory.

The first owned startup-uninstall attempt reported that removal was not yet
confirmed and preserved the receipt. A subsequent status query confirmed the
job was no longer enabled/running; repeating the owned uninstall then succeeded.
This observation concerns removal confirmation, not proof of a bootstrap race.
Installing the new runtime succeeded, followed by `native_running: true` and
reachable matching metadata on both loopback and public HTTPS.

While the HTTP service was stopped and its process lock held, the configured
device's allowed tool set was expanded by exactly the three session tools, and
its generation incremented. Existing OAuth grants were not expanded. The
resource, client, device and redirect configuration were preserved.

ChatGPT initially reused the old 46-scope authorization request even after a
tool refresh. The owner-authorized three session scopes were explicitly added
to that browser authorization request; the normal password/consent flow then
completed. The resulting new grant contains 49 scopes, is not revoked, and has
`expires=0` (no grant expiry). A subsequent ChatGPT action refresh displayed all
three session tools and `session_id` on catalog/call inputs. Existing access
token expiry and refresh rotation were retained.

## Production ChatGPT acceptance

The existing ChatGPT conversation "Anywhere Computer確認" completed the requested
flow against the deployed public connection. Its final response and all eight
reported external operation IDs were checked against the production ledger,
using the grant-scoped external-to-internal ID mapping. All matched completed
operations with no errors:

- `computer_status`: ready, Darwin, matching integrated runtime identity.
- `codex_plugin_session_open`: open, idle timeout 1800 seconds.
- Catalog summary and exact `openaiDeveloperDocs.list_openai_docs` schema:
  successfully retrieved, including `call_arguments` and read-only metadata.
- Two calls with `arguments={"limit":1}`: both successful, `is_error=false`,
  same session ID, no truncation. ChatGPT displayed the returned document title.
- Session status: the same session remained open.
- Session close: closed with `cleanup_confirmed=true`.

The first call's external operation ID was
`94f83a5da6034d04bf374068a9b89d67`; the second was
`aa52172623cf46c7b889b2dd006822c8`. The verified closure operation was
`11001cebe1d948f488e01a261b0b2c40`. These are historical receipts, not live
session credentials. No operation needed recovery through `operations_get`.

ChatGPT observed 49 granted tools versus 50 Engine tools in `computer_status`.
This is expected: `operations_recent` is explicitly local-only and excluded
from remote grants by `authorization.LOCAL_ONLY_TOOLS`.

## Acceptance boundaries

Native GUI input, real desktop-image display in ChatGPT, Windows/Linux live
deployment and sleep/logout/reboot recovery are not claimed by this deployment.
An indefinite connection grant does not make a plugin session permanent:
session idle timeout remains 30-1800 seconds, and engine restart ends sessions.
