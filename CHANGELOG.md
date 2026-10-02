# 変更履歴

公開パッケージと未公開の開発作業を分けて記載します。alpha版は正式stableではありません。
日付付きの実機検証記録はローカルの履歴資料として保管しています。

## 0.2.0b64 / Plugin 0.2.0-beta.64 — unpublished candidate (2026-10-02)

- Include the unpublished beta.62 MCP export, catalog diagnosis and setup-status
  fixes without changing that bundle's wheel bytes. Beta.63 belongs to the
  separate Subchat intent-recovery candidate.
- Half-close the TCP stop reply before waiting for its client to close, bounded
  to two seconds. Do not let process exit race an outstanding Windows reply read;
  retain active-work refusal, exact replies and original transport failures.
- Record bounded, secret-free transport phases and independent fixture health
  on platform-test failures. Do not retry requests or extend read deadlines.

## 0.2.0b62 / Plugin 0.2.0-beta.62 — unpublished candidate (2026-10-02)

- Export local MCP JSON/TOML with the installed interpreter and explicit state
  directory. Show the JSON entry after interactive local setup; do not overwrite
  client settings, initialize credentials or start an agent during export.
- Keep JSON/TOML exports copyable through legacy console encodings, preserving
  Unicode paths including non-BMP characters with the appropriate format escapes.
  Supersede the unpublished beta.59/60/61 bundles without changing their wheel bytes.
- Report unavailable, malformed or duplicate engine catalogs separately from
  authenticated engine readiness. Fail doctor rather than report success when
  the catalog is unconfirmed; keep actual AI client acceptance separate.
- Preserve safe startup failure categories in management and reconcile once
  without repeating a start after a lost acknowledgement. Show catalog evidence
  and localized next steps without exposing exception contents.
- Capture bounded initial-frame bootstrap messages and RPC timing on browser
  readiness failure. Keep the original assertion and deadline without retrying
  initialization or recording connection draft fields.
- Commit setup status binding, claim and receipt in one durable transaction.
  Let status inspect an asynchronous save without waiting or repeating it; retain
  separate durable claims before configuration writes and delegated operations.
  Failed or invalid status requests retain their exact operation-ID binding;
  a failed commit cannot return a successful status receipt.
- Preserve an authenticated, validated local RPC reply when peer shutdown resets
  only the socket close waiter. Keep original read/authentication/validation
  failures and cancellation; never repeat an operation to handle that reset.

## 0.2.0b57 / Plugin 0.2.0-beta.57 — unpublished personal-use candidate (2026-10-02)

- Recover public connection fields after frame recreation on hosts with the
  optional private widget-state API. Bind them to the same MCP session and target,
  retain uncertain-save recovery, and require a new review after restoring edits.
  Explain when the host cannot retain the fields; never replay planning or saving.
- Add an explicit fixed `public-core` HTTP tool offering, validated during setup,
  save, reload, update and direct service construction. Existing full configurations
  retain their scopes. This personal full package still contains private features
  and is not a private-code-free public distribution or an OS sandbox.
- Recover hash-conditional preview and atomic edit of one existing plain-string
  XLSX cell. Retain a backup and reject unsupported, signed, macro-enabled,
  compatibility-markup or shared-worksheet packages. Existing grants do not expand.
- Report native macOS GUI support truthfully on Windows/Linux and preserve typed
  Subchat startup 401/403 reasons without relabeling unknown submissions as unsent.
- Reject additional Computer Use aliases before starting an unsupported direct
  execution context. Extend exact-file read-only and frame-recreation regressions.
- Retain owned browser cleanup and correct bounded fixture observations. Natural
  intermittent browser stalls remain under investigation; no input retry or
  timeout extension is added.

## 0.2.0b55 / Plugin 0.2.0-beta.55 — unpublished candidate (2026-10-01)

- Build from published beta.52 with the connection draft repair only; the
  separate unpublished beta.53/54 diagnostics remain under review.
- Keep edited connection fields across status refreshes and requests to show
  the already open local connection view. Require explicit discard on navigation.
- Preserve reviewed fields when a save conflicts or its response is unknown,
  alongside the actual saved configuration. A later status resolves the held
  fields only when it confirms the exact reviewed configuration as configured.
- Retain existing uncertain-operation recovery without automatically resending
  a save, and cover refresh and real configuration conflicts in Chrome tests.

## 0.2.0b52 / Plugin 0.2.0-beta.52 — published (2026-10-01)

- Include the unpublished beta.51 repairs with a new immutable package version.
- Preserve delegated catalog and ordinary calls through the strict beta.48
  loopback schema by limiting the receipt extension to operation recovery.
- Negotiate authenticated Engine receipt metadata before child recovery;
  unsupported engines require a launcher update and lookup of the original ID,
  without replacing active work or accepting an unbound result.
- Recheck child and parent authorization and expiry after the feature probe;
  revocation during that wait prevents forwarding the operation lookup.

## 0.2.0b51 / Plugin 0.2.0-beta.51 — unpublished candidate (2026-10-01)

- Include the unpublished beta.50 UI and connection repairs without replacing
  its versioned wheel. Update the management preview to alpha.11.
- Identify files without an extension correctly and wrap long type labels at
  narrow widths while retaining the complete file name.
- Put opening the enrollment page and copying its displayed code before the
  result check. The native host chooses the active, verified provider URL;
  cancelled, expired and completed attempts cannot dispatch a browser request.
- Preserve the existing attempt on browser dispatch failure and provide a
  selected-code fallback when clipboard permission is unavailable.

## 0.2.0b50 / Plugin 0.2.0-beta.50 — unpublished candidate (2026-10-01)

- Include the unpublished beta.49 connection, delegation, Subchat and upload repairs.
- Validate the POSIX upload verification flags at runtime without referencing
  unavailable Windows constants. Missing flags stop verification instead of
  falling back to an unsafe open.
- Give connection consent, passkey enrollment and authentication outcomes a
  consistent offline layout. Keep exact tool scopes inspectable, show passkey
  waiting and cancellation truthfully, and separate phone enrollment links from
  browser-owned cross-device authentication prompts.
- Keep permission inspection before mobile approval, prevent duplicate passkey
  submissions and stale completion after denial or expiry, and explain the
  distinct credential and registration-history limits without unnecessary resets.
- Align file, settings, connection and desktop management pages around their
  controls. Wrap long file names, distinguish disabled actions, and preserve
  uncertain-write recovery. Native callbacks report code receipt separately from
  completed connection, with a hash-pinned static stylesheet.

## 0.2.0b49 / Plugin 0.2.0-beta.49 — unpublished candidate (2026-10-01)

- Preserve existing OAuth scopes when publishing additional HTTP tools. Existing
  clients can rotate their tokens; expanded access requires fresh owner consent.
- Allow canonical POSIX root grants and serialize delegated writes with ordinary
  file writes. Authenticate recovery receipts for engine-backed delegated tools
  without changing ordinary operation lookup responses.
- Preserve the Subchat challenge rejection across the first HTTP association.
  Record authentication failures before any draft as known-unsent, retain page
  ownership until closure is verified, and match background pages to the exact
  Chrome target created by that operation.
- Allow an explicit, separately verified fresh enrollment grant for a recovered
  registration's first TLS channel while retaining its original request identity.
- Verify POSIX upload destination identity and content before recording success.
  Changed or unverified publication stays unknown with recovery data retained;
  this does not isolate other processes running as the same OS user.

## 0.2.0b48 / Plugin 0.2.0-beta.48 — published (2026-10-01)

- Clamp blocked-tab diagnostic remaining time to the configured retention limit,
  avoiding a floating-point display above 300 seconds. Keep the original expiry
  deadline and verify deterministic rounding and eventual cleanup.
- Include the individual-file grants prepared in beta.47. Observe shared-agent
  recovery and owned browser cleanup before parallel CI workers. Exercise normal
  and slow browser closure through the actual cleanup task and preserve failures.

## 0.2.0b47 / Plugin 0.2.0-beta.47 — unpublished candidate (2026-10-01)

- Issue delegated read-only access to individual files with repeatable
  `--read-file` entries. Do not grant the containing directory or descendants.
- Confine selected-file reads through POSIX directory descriptors or Windows
  pinned handles, rejecting ancestor and leaf symlink substitutions. Preserve
  existing root grants, per-operation revocation, expiry and reconnect checks.
- Verify owner issuance, HTTP authentication, selected-file reads, sibling and
  write refusal, reconnect and revocation with isolated real HTTP sessions.
  Ordinary Subchat credential handoff and cloud computer registration remain
  separate acceptance requirements.

## 0.2.0b46 / Plugin 0.2.0-beta.46 — published (2026-09-30)

- Preserve blocked-tab ownership and activity after an unconfirmed close. Report
  cleanup_pending, forbid further display and allow explicit cleanup to finish.
- Protect diagnostic tabs from both direct MCP and explicit Plugin session idle
  expiry, including a catalog failure that creates the blocked tab.
- Reveal the exact owned background Chrome process on macOS using a verified
  native helper. Check PID, process start and bundle identity before and after
  activation; report OS foreground evidence separately from CDP acknowledgement
  and challenge completion. Ordinary Chrome profiles are never a fallback.
- Observe expiry completion with a bounded close check in the regression test,
  preserving the retention deadline instead of relying on 100 ms CI scheduling.

## 0.2.0b45 / Plugin 0.2.0-beta.45 — unpublished candidate (2026-09-30)

- Retain the exact newly created blocked Subchat tab for bounded local inspection
  through status, show and close in the same send-capable Plugin session. Keep
  normal operation in the background; expiry and session exit close the owned tab.
- Preserve the rejection latch and original operation/receipt. Display requests
  never solve a challenge, resend, open another profile, or claim OS foreground
  visibility or authentication success. Do not expose this tool through read-only
  mode or the HTTPS gateway.

## 0.2.0b44 / Plugin 0.2.0-beta.44 — published (2026-09-30)

- Resume phone result checks after history-cache restoration without extending
  expiry or processing responses from a paused poll.
- Exercise persisted page lifecycle events before QR selection and during
  observation, preserving the script and DOM used by a restored page.

## 0.2.0b43 / Plugin 0.2.0-beta.43 — unpublished candidate (2026-09-30)

- Add a phone-registration QR to the existing, locally authorized owner-passkey
  page. The original browser observes the verified credential-store result,
  removes the QR, clears the ticket and displays completion automatically.
- Bind result checks to the browser cookie, CSRF, ticket and origin. Serialize
  registration and observation so ticket consumption cannot race completion.
  Failed saves, expired links, removed keys and network errors never imply success.
- Exercise independent desktop and phone browser contexts with an isolated
  WebAuthn authenticator; this does not claim physical phone-camera acceptance.

## 0.2.0b42 / Plugin 0.2.0-beta.42 — unpublished candidate (2026-09-30)

- Classify non-timeout Playwright home-navigation errors as navigation_failed
  in UI catalog, HTTP catalog bootstrap and send preparation. Preserve the
  distinct navigation_timeout result and known-unsent operation recovery.
  Network errors before dispatch no longer produce an unknown send outcome.

## 0.2.0b41 / Plugin 0.2.0-beta.41 — unpublished candidate (2026-09-30)

- In browser-prepared HTTPX Subchat sessions, retain the controller-owned
  authenticated catalog bootstrap page for new-conversation preparation.
  Revalidate its URL, empty idle composer, conversation history and exact
  model/effort selection before sending. This avoids an unnecessary second
  home navigation; it does not bypass provider challenges.
- Close unused bootstrap pages on session shutdown and invalid catalogs.
  Preserve separate pages for read-only and ordinary browser-send adapters,
  and never discover or claim existing user tabs.

## 0.2.0b40 / Plugin 0.2.0-beta.40 — unpublished candidate (2026-09-30)

- Include Subchat device-save scopes in HTTP tool upgrades while retaining
  explicit consent for existing restricted grants. Enable desktop Start when
  diagnostics confirm a stale endpoint left by a stopped engine.
- Give each sandbox file its own device-save identity, preserving legacy
  transfer checkpoints and duplicate protections. Accept verified matching
  JSON/HTML artifacts and empty files while rejecting mismatched responses.
- Share the 2 MiB image budget across video frames and reduce quality/size when
  necessary so HTTP/SSH delivery and operation recovery accept the result.
- Recreate the Windows owner-pipe listener after transient accept failures;
  report a fatal error if a replacement listener cannot be created.
- Revalidate macOS AX elements in the traversal order used by their full or
  compact observation. Preserve bounded traversal and process/window identity.
- Preserve provider interruption/output-limit exceptions in HTTP-only recovery
  so terminal state and its reason are saved instead of leaving a turn reserved.
- Distinguish browser challenges, login requirements, navigation errors and
  timeouts in Subchat preparation and UI catalog diagnostics. This reports a
  blocked browser navigation; it does not bypass challenges or establish that
  generation has been restored.
- Detect challenged home navigation during HTTP catalog bootstrap immediately
  and stop new catalog/preparation attempts in the same controller session.
  Preserve existing authenticated history recovery without browser bootstrap.

## 0.2.0b39 / Plugin 0.2.0-beta.39 — development prerelease (2026-09-30)

- Report how many active HTTP grants still lack the tools requested by
  `http-add-tools`, including deliberately restricted grants. Mark fresh
  consent as required in that case without expanding or replacing those grants.
- Explain that updating a ChatGPT developer plugin's tool list does not change
  the connected account's OAuth grant; the owner must review any new consent.

## 0.2.0b38 / Plugin 0.2.0-beta.38 — development prerelease (2026-09-30)

- Prepare an exact temporary Chat tab CDP target before closing read-only
  bootstrap tabs. Reuse that connection if Playwright's close stalls, while
  keeping generation-abort closes immediate. Retain the original tab and the
  read result or exception across bounded cleanup.
- Separate serial macOS browser-control and Subchat-browser CI processes with
  bounded step durations. Close incomplete local HTTP fixture connections at
  teardown so Python 3.12's server wait cannot stall indefinitely. Give a
  loaded Windows browser fixture enough outer scheduling time while retaining
  its short internal dispatch/cleanup limits.

## 0.2.0b37 / Plugin 0.2.0-beta.37 — unpublished candidate (2026-09-30)

- Add optional bounded local audio transcription with an explicitly installed
  OpenAI Whisper checkpoint. Return text and model identity to ordinary Chat
  when its client cannot interpret an MCP audio item. Never fetch a model or
  upload the clip during a tool call.
- Reap the local transcriber on timeout or cancellation and limit the source,
  clip, checkpoint, result and temporary-file exposure. Keep audio item delivery
  as a separate capability.

## 0.2.0b36 / Plugin 0.2.0-beta.36 — development prerelease (2026-09-30)

- Clarify that native AX actions invalidate observations but keep the owned
  session alive. Reuse that session to observe the same window after an action,
  and close every session opened during the task.
- Document ChatGPT's developer-plugin tool refresh after HTTP scope changes and
  the beta.35 ordinary-Chat browser and native GUI acceptance results.

## 0.2.0b35 / Plugin 0.2.0-beta.35 — development prerelease (2026-09-30)

- Report a detached iframe as an unavailable browser frame when Chromium
  rejects an in-flight observation before Playwright delivers its detach event.
  Keep a closed tab or browser distinguishable from a missing frame.

## 0.2.0b34 / Plugin 0.2.0-beta.34 — development prerelease (2026-09-30)

- Describe browser tab closure as website interaction on the OAuth consent
  page, including when it is the only requested browser capability.
- Run browser-control and browser-backed Subchat integration tests before
  parallel macOS/Linux workers after CI observed a cookie-test failure and a
  stalled manual-send test in concurrent workers. Preserve full test coverage
  and bounded diagnostics.

## 0.2.0b33 / Plugin 0.2.0-beta.33 — development prerelease (2026-09-30)

- Add observed frame IDs, frame-scoped semantic browser actions and open-shadow
  form labels. Reject stale observations after same-URL reloads and frame detach.
  Keep frame element coordinates distinct from the full-tab screenshot.
- Manage owned tabs and popups in one isolated browser context, and expose
  observed JavaScript dialogs with explicit accept/dismiss responses. Modal
  dialogs in another owned tab wake blocked observations without autoaccepting.
- Include exact-window native macOS screenshots alongside Accessibility data,
  and permit explicitly observed secondary AX actions such as increment and
  page scroll. Revalidate the target before input and require reobservation
  to confirm its effect. Existing Screen Recording permission is required.
- Restore validated MCP images and audio across HTTP, SSH and device routing.
  Match MIME, byte count and SHA-256, preserve original operation recovery, and
  keep media bytes out of the JSON text presented to models.
- Return rendered document previews as native MCP images, including through
  device routing and operation recovery. Check the image receipt in Workspace
  before displaying it; direct consumers of the former `data_base64` field
  must read the new image content.
- Prepare up to ten Library attachments in one bounded browser session, with
  immutable per-file request bindings, durable receipts, complete preflight
  and no further dispatch after an uncertain upload. A real two-file, one-Chat
  acceptance verified both contents and saved resource references.
- Bound temporary history-tab cleanup and retry only the exact owned CDP
  target when Playwright's existing close promise cannot resend the command.
- Keep browser and native GUI ownership while shutdown is unconfirmed, so a
  guarded engine update cannot discard a live input session. Close admitted
  SSH file workers before their delegation stores close, and reject nonfinite
  or overnested owner-pipe JSON as a protocol error.
- Let an already-owned browser shutdown continue beyond the five-second caller
  receipt deadline, with a bounded internal close attempt. This permits slow
  Chrome shutdown to release capacity after an initially unknown receipt.
- Expose late Subchat send-worker failures separately from provider receipts.
  Classify a rejected draft as unsent only when interception and confirmed
  closure of its owned page prove that no generation request escaped.
  Retain a page-specific request guard if rejected-page cleanup is unconfirmed.
- Preserve literal paragraph boundaries when inserting Subchat drafts, including
  terminal newlines and decorated URLs. Compare exact text after the editor's
  queued mutation processing; do not trim input or retry a rejected insertion.
- Keep the rejected-page send guard when page cleanup is unconfirmed, bound
  background browser shutdown, and recheck authorization after HTTP client
  construction before issuing a delete request.
- Show the same session-local send-worker diagnostics over local MCP and HTTPS.
  Reap an owned desktop notification process on cancellation or timeout without
  changing its durable queue event.
- Reap media decoder processes when the caller is cancelled, as well as after
  timeout. Keep actual media delivery and model perception as separate checks.
- Separate desktop capability, resource, identity and device renderers. Display
  browser/media/document-preview capabilities and describe helper manifest
  verification accurately; retain cleanup blockers even when counts are absent.
- Package the existing macOS system-audio helper in portable archives and
  verify it after relocation. A permission check does not prove recording or
  receiving-model audio perception.
- Add a read-only measurement harness for one Chat versus parallel Subchats,
  and document primary-source competitor comparisons and unverified gates.
  No usage reduction or comparison win is claimed without measured evidence.
- Reduce explicit credential state-test KDF cost while retaining real
  production-work-factor tests. Separate Windows KDF and ACL measurements;
  distribute remaining Windows tests across four VMs with two workers each,
  and verify collected and finished test IDs before the existing required gate.
  Actual CI stability, elapsed time and total VM time need separate measurement.

## 0.2.0b31 / Plugin 0.2.0-beta.31 — development prerelease (2026-09-28)

- Stop waiting for ChatGPT's deferred resources after the document response
  commits. Verify the actual composer and picker before preparing a Subchat
  send, so an already usable page can proceed even when `DOMContentLoaded`
  remains pending.
- Preserve safe preparation failure reasons and return the saved operation ID
  and provider receipt in failed send and status responses. Continue to reuse
  the exact intent and request ID after a verified unsent failure.
- Clarify when independent Subchats save work, how to run them while the parent
  continues, and which context and tools ordinary Chat children actually get.

## 0.2.0b30 / Plugin 0.2.0-beta.30 — development prerelease (2026-09-28)

- Add bounded console and page-error history to the isolated browser, with
  redacted source routes and owner-scoped pagination.
- Add fresh-observation hover, unique select-option choice and bounded element
  or document scroll, with new observations and selection/scroll receipts.
- Correct typed Peekaboo guidance using the installed 3.0.0-beta3 MCP schema:
  its window listing tool and exact-window input contract differ from the
  compatible adapter, which rejects it before GUI capture or input.

## 0.2.0b29 / Plugin 0.2.0-beta.29 — development prerelease (2026-09-28)

- Add bounded current-DOM source, redacted response/failure history and compact
  source-claimed publication details and visible links to the isolated browser.
- Add exact-target browser keys, drag and drop, local file input and exclusive
  browser download with a SHA-256 receipt. Keep owner and observation bindings,
  and preserve unknown-action recovery.
- Forward bounded MCP audio items without duplicating base64 in model text.
  An optional local FFmpeg decoder provides short WAV clips and selected video
  frames as MCP media items; receiving-model perception needs separate proof.
- Record verified competitor capabilities, current coverage and outstanding
  native desktop, browser and media gaps in the GUI guide.

## 0.2.0b28 / Plugin 0.2.0-beta.28 — development prerelease (2026-09-28)

- Add short, breadth-first macOS Accessibility observations and exact role,
  label or identifier actions. Recheck app, window, value and full-window
  selector uniqueness before input; reject changed or ambiguous targets.
- Discover focused and main windows when an app exposes an empty AXWindows list.
  A live Calculator check confirmed sidebar toggles in both directions,
  including when its history exceeds the detailed tree's element limit.
- Resolve isolated-browser role and label targets at action time so page
  rerenders do not leave a stale element handle. Keep exact-one and
  actionability checks before dispatch.
- Include bounded, whitespace-normalized HTML form label metadata and
  CSS-pixel element boxes in browser observations, with an optional rendered
  JPEG viewport delivered as an MCP image item rather than base64 text.
- Record the comparison with Computer Use, Peekaboo, Playwright MCP,
  browser-use, Stagehand and Skyvern, plus the remaining GUI implementation
  and acceptance work.

## 0.2.0b27 / Plugin 0.2.0-beta.27 — development prerelease (2026-09-28)

- Show a separate provider receipt state and confirmed conversation link for
  each saved Subchat send. Keep one durable intent key per intended child, with
  no child count inferred from request wording.
- Add `subchat_observe` to reconcile a saved send without dispatching a queued
  follow-up. Mark send-capable recovery and waiting as potentially sending.
- Add bounded browser accessibility snapshots and exact role/name or label
  actions tied to a short-lived snapshot ID. Keep CSS actions available.
- Correct published MCP risk hints for Subchat, Library upload, local browser
  startup, audio capture, plugin-session close and bounded device discovery.
  Generic external plugin calls retain the destructive hint.
- Codex Computer Use remains unavailable through the direct plugin bridge
  because its owning model-turn execution context cannot be supplied there.

## 0.2.0b26 / Plugin 0.2.0-beta.26 — development prerelease (2026-09-28)

- Remove `pip`, `ensurepip` and C extension headers from portable application
  ZIPs after installing the pinned dependencies. A same-runtime macOS build
  shrank by 4.04 MB (5.8%); the Python license remains bundled.
- Keep the portable interpreter focused on running Anywhere Computer and
  document the build-machine `uv` dependency installation boundary.

## 0.2.0b25 / Plugin 0.2.0-beta.25 — development prerelease (2026-09-28)

- Apply the selected shared Engine's current read and write line limits to
  delegated child file operations after migration.
- Keep existing `subchat_list` OAuth grants limited to operation summaries;
  prompt previews require the separate `subchat_prompt_preview` consent scope.
- Use four test workers on macOS and Linux after a local 2,532-test run
  completed in 89.86 seconds, down from 171.47 seconds with two workers.
  Keep Windows at two workers after four-worker runs exposed timing-sensitive
  failures; reliable end-to-end CI time takes priority over one fast run.
- Resolve release tags without treating a missing tag's HTTP 404 body as an
  existing ref; reject a published version tag or unfinished draft bound to
  another main commit.
- Resolve the unpublished draft by its requested tag when comparing uploaded
  asset digests; GitHub's REST tag endpoint cannot find a draft without a tag.
- Keep the short-timeout Windows owner-pipe tests in the serial CI preflight
  rather than competing with parallel test workers.
- Check Subchat save recovery by the first uncertain receipt, same-ID final
  result and single target write; a renewed caller may skip another unknown
  response when background reconciliation finishes first.

## 0.2.0b24 / Plugin 0.2.0-beta.24 — unpublished candidate (2026-09-28)

- Consolidate private Subchat selection writes while keeping account checks and
  send consent intact.
- Publish a new version from successful `main` CI only after five portable
  archives pass provenance and matching-release checks. Include `SHA256SUMS`.
- Run time-sensitive CI preflight tests once instead of repeating them in the
  remaining suite. Physical-host acceptance remains a separate check.

## 0.2.0b11–b23 — development candidates (2026-09-27)

- Add owner passkey enrollment and revocation, and bounded delegated child
  permissions for tools, devices, paths and expiry.
- Add a local Codex/Claude peer mailbox with delivery receipts, presence and
  restart recovery. Delivery does not wake another model turn.
- Extend Subchat with saved operation discovery, file attachments and Library
  upload, queued model and resource changes, durable mutation receipts, and
  explicitly armed automatic follow-ups.
- Confine Windows upload, delegated create and peer credential publication to
  verified handles and pinned directories. Add platform CI coverage.

## 0.2.0b10 / Plugin 0.2.0-beta.10 — beta prerelease (2026-09-26)

- Use the installed Microsoft Edge for isolated browser sessions on Windows,
  while retaining Chrome on macOS and Linux and explicit channel overrides.
  Report a failed local browser startup before dispatch with a specific error.

## 0.2.0b9 / Plugin 0.2.0-beta.9 — unpublished candidate (2026-09-26)

- Provide a stopped, explicit ACL migration for existing custom Windows state
  directories, with preflight checks for unsafe links, unreadable ACLs, and
  matching live processes. Default state migration remains separate.

## 0.2.0b8 / Plugin 0.2.0-beta.8 — unpublished candidate (2026-09-26)

- Verify the selected Windows Edge or Chrome account with a bounded same-origin
  browser GET during dedicated Subchat setup. This avoids an intermittent 403
  observed in the separate HTTPX authentication GET without changing the send
  transport or its account pin.

## 0.2.0b7 / Plugin 0.2.0-beta.7 — unpublished candidate (2026-09-26)

- Give Windows setup catalogs a fresh private state directory and prepare test-owned temporary state with strict ACLs before exercising runtime paths. Legacy installed state still requires the explicit stopped migration.

## 0.2.0b5 / Plugin 0.2.0-beta.5 — unpublished candidate (2026-09-26)

- Make Windows ACL handling type-check cleanly on both Windows and macOS
  before packaging the release wheel.

## 0.2.0b4 / Plugin 0.2.0-beta.4 — unpublished candidate (2026-09-26)

- Create Windows state and dedicated browser directories with the current user's
  protected ACL, including when launched elevated. Reject inherited or legacy
  ACLs and provide a stopped-process migration for the standard alpha engine
  and Subchat state locations.
- Share a checked browser definition across Subchat setup, Plugin selection and
  CLI launch for the supported Chrome and Windows Edge routes.
- Align Windows browser login tests with the headed offscreen request path.

## 0.2.0b3 / Plugin 0.2.0-beta.3 — unpublished candidate (2026-09-26)

- Treat Windows ConPTY closure after the shell exits as end of output while
  retaining the Job until descendants exit. Keep active-shell read errors visible.
- Read framed ConPTY input without a buffered stdin lock during worker shutdown.

## 0.2.0b2 / Plugin 0.2.0-beta.2 — unpublished candidate (2026-09-26)

- Keep Windows saved-state Subchat tools available when a legacy Chrome profile or
  an account override is configured without a selected dedicated browser. Explain
  the required setup and keep sending disabled.
- Preserve quoted executable and script paths when starting Windows ConPTY
  commands through `cmd.exe`, including portable installations in paths with spaces.

## 0.2.0b1 / Plugin 0.2.0-beta.1 — unpublished candidate (2026-09-26)

- Add a selected, account-pinned Windows Chrome or Edge path for ordinary Chat
  Subchat. Sending remains disabled until the owner explicitly enables it.
- Keep macOS browser-prepared HTTP sends in the background; recover uncertain
  submissions and same-conversation replies from saved receipts.
- Improve native macOS GUI reads when an optional Accessibility value fails,
  and render Japanese document previews with local fonts.
- Bundle portable runtimes for macOS, Windows and Linux. The standard archives
  do not include LibreOffice or Poppler, and OS signing remains separate.

## 0.2.0a67 / Plugin 0.2.0-alpha.67 — unpublished candidate (2026-09-26)

- Keep Finder elements visible when an optional AXValue read fails, while
  refusing input before dispatch with an explicit non-comparable value error.
- Require saved Windows send consent even with transport overrides, and stop
  reading an unselected dedicated login after revocation.
- Clarify Windows first-login and missing-browser failures before beta testing.

## 0.2.0a66 / Plugin 0.2.0-alpha.66 — unpublished candidate (2026-09-26)

- Add an explicitly selected, account-pinned dedicated Chrome or Edge profile
  for Windows Subchat browser-prepared HTTPX sends. Keep the Windows Plugin
  read-only until the owner enables sending for that selection.
- Launch the selected Windows browser headlessly for generation preparation;
  preserve the existing macOS background path. Windows live generation remains
  an acceptance gate before the beta release.

## 0.2.0a65 / Plugin 0.2.0-alpha.65 — unpublished candidate (2026-09-26)

- Render Japanese DOCX text with system fonts in the isolated macOS preview.
  Keep inherited Fontconfig sysroot settings from hiding those fonts.
- Verify visible PNG pixels after creating and editing a Japanese document.

## 0.2.0a64 / Plugin 0.2.0-alpha.64 — unpublished candidate (2026-09-26)

- Resolve macOS native GUI apps by a unique regular process when background
  helpers share the bundle ID. Use the kernel process start time to retain
  window and element identity when AppKit does not report a launch date.
- Recheck the selected PID, bundle ID, activation policy and process start time
  before binding Accessibility elements, and reject ambiguous app selections.

## 0.2.0a63 / Plugin 0.2.0-alpha.63 — unpublished candidate (2026-09-26)

- Check the selected Chat surface again before sending, including follow-ups.
  Keep Work suggestion buttons distinct from the selected conversation mode.
- Require fresh consent when adding Subchat or delegation tools to an HTTP
  connection, and describe direct and indirect capabilities on the consent page.
- Clarify model selection and user-requested delegation in the Plugin skills.
  Keep permission decisions in the host's existing controls.
- Improve workspace focus, contrast and saved permission summaries without
  introducing another permission dashboard.

## 0.2.0a62 / Plugin 0.2.0-alpha.62 — unpublished candidate (2026-09-26)

- Render bounded DOCX, XLSX and PPTX page previews on macOS. Strip spreadsheet
  formulas and defined names from the private render copy while preserving the
  original file and its hash.
- Add explicit macOS Chrome profile ID discovery, account-pinned selection and
  revocation for Subchat. Constrain profile sources to the current user's Chrome
  store or a managed snapshot, and require owner authentication for initial HTTP
  tool selection.
- Add an optional portable renderer bundle input with license and provenance
  checks. Standard portable builds do not yet include the renderer.

## 0.2.0a61 / Plugin 0.2.0-alpha.61 — unpublished candidate (2026-09-26)

- Keep an owned Browser tab's last explicit navigation outcome visible through
  later document or SPA navigation. Observe an uncertain navigation without
  replaying its request.
- Add a hash-bound macOS DOCX page preview with bounded, sandboxed local
  rendering and a text fallback when rendering is unavailable. The renderer
  remains an external prerequisite; XLSX/PPTX rendering is not yet supported.
- Report unexpected Subchat send failures as unconfirmed outcomes with a saved
  operation ID and no automatic retry.

## 0.2.0a56 / Plugin 0.2.0-alpha.56 — unpublished candidate (2026-09-26)

- Close the browser and Playwright driver when isolated session creation is
  cancelled before registration, preventing orphan processes after a disconnect.
- Disable Python bytecode writes for native manager status and enrollment
  children so those readers do not alter a verified portable runtime.

## 0.2.0a49 / Plugin 0.2.0-alpha.49 — unpublished candidate (2026-09-25)

- Keep Subchat sessions open through active browser preparation and uncertain
  completion, so follow-up collection does not lose the in-flight result.
- Add an offline owner-password reset that revokes existing grants and requires
  explicit device re-enablement. The password minimum is now eight characters.
- Clarify OAuth consent risks, requested tools, password recovery, and callback
  destination; prevent accidental double submission of a consent decision.

## 0.2.0a48 / Plugin 0.2.0-alpha.48 — unpublished candidate (2026-09-25)

- Count a live Subchat generation stream after its initial send receipt so
  Plugin and direct MCP sessions remain available until collection finishes.
- Preserve field-specific model-selection errors after a controller restart.
- Keep direct MCP sessions alive during active or unconfirmed Subchat work.

## 0.2.0a47 / Plugin 0.2.0-alpha.47 — unpublished candidate (2026-09-25)

- Reject mismatched Subchat UI model and effort labels against the selected
  HTTP catalog choice before browser dispatch or HTTP generation.
- Keep an explicit Plugin bridge session open while Subchat background work is
  active, including after the initial tool call has returned.

## 0.2.0a46 / Plugin 0.2.0-alpha.46 — unpublished candidate (2026-09-25)

- Warn on the owner consent page when a client omits available Subchat tools.
  Approval still grants only the tools the client requested.

## 0.2.0a45 / Plugin 0.2.0-alpha.45 — unpublished candidate (2026-09-25)

- Explain macOS Accessibility setup when the first native GUI window request
  fails, including the need to open a new session after granting permission.
- Point public HTTPS 403 diagnostics to both edge access rules and the origin
  Host-header setting without treating either as a proven cause.

## 0.2.0a44 / Plugin 0.2.0-alpha.44 — unpublished candidate (2026-09-25)

- Advertise only the HTTP tools enabled for this device in OAuth discovery.
  Prevent ChatGPT app registration from offering known but unavailable scopes.

## 0.2.0a43 / Plugin 0.2.0-alpha.43 — unpublished candidate (2026-09-25)

- Allow a bounded longer startup window for a freshly launched agent under
  load, keep concurrent connectors waiting through that window, and report a
  child process that exits before readiness immediately.

## 0.2.0a41 / Plugin 0.2.0-alpha.41 — unpublished candidate (2026-09-25)

- Retry an HTTP MCP tool call after 401 or 404 only when the authenticated
  server marks a pre-dispatch authentication or session rejection. Treat an
  unmarked response as an unknown outcome and retain the operation ID for
  recovery instead of replaying a possible write.

## 0.2.0a40 / Plugin 0.2.0-alpha.40 — unpublished candidate (2026-09-25)

- Expose an owner- and account-scoped, read-only Subchat submission list over
  the authenticated HTTPS gateway so a client can recover saved operation IDs
  without opening Chrome. Continue to require exact-ID validation for legacy
  grants rather than listing their submissions.

## 0.2.0a39 / Plugin 0.2.0-alpha.39 — unpublished candidate (2026-09-25)

- Reclaim isolated browser sessions after their page closes or browser
  disconnects, so unexpected exits cannot exhaust the four-session limit.
  Report a closed page as ended in status diagnostics.

## 0.2.0a38 / Plugin 0.2.0-alpha.38 — unpublished candidate (2026-09-25)

- Require an explicit plugin session for namespaced Subchat send and recovery
  tools before dispatch. Resolve the matching namespaced activity tool so
  background work keeps its owning session alive.

## 0.2.0a37 / Plugin 0.2.0-alpha.37 — unpublished candidate (2026-09-25)

- Wait briefly for an existing Chat's message history to appear before preparing
  a follow-up. Keep an empty or ambiguous history as a pre-dispatch failure.
- Report a bounded reason code for unavailable or ambiguous history instead of
  collapsing these preparation failures into `unknown`.

## 0.2.0a36 / Plugin 0.2.0-alpha.36 — unpublished candidate (2026-09-25)

- Invalidate a Codex plugin tool selection when the server's reported
  authentication status changes, without rejecting normal runtime startup.

## 0.2.0a35 / Plugin 0.2.0-alpha.35 — unpublished candidate (2026-09-25)

- Protect explicit bridge sessions while their Subchat controller reports an
  active send, recovery or queue watch. Probe activity before idle cleanup,
  and retain the session when the probe cannot establish a safe stop.
- Persist sanitized late preparation failures so an expired controller does
  not erase their reason. Require the activity tool before a stateful bridge
  call, including a call to an older manual MCP registration.
- Require a session for stateful Subchat tool names even if the MCP server has
  a custom registration name. Document the bridge workflow in the Subchat
  skill and guides.

## 0.2.0a31 / Plugin 0.2.0-alpha.31 — unpublished candidate (2026-09-25)

- Require an explicit, persistent ChatGPT plugin bridge session for Subchat
  send, message, recover, wait and queue-watch calls. A temporary context now
  rejects these calls before dispatch, instead of losing pending work on close.

## 0.2.0a30 / Plugin 0.2.0-alpha.30 — unpublished candidate (2026-09-25)

- Make `subchat_wait` observe an active browser preparation instead of returning
  a stale `prepared` row immediately. Bound each wait, suggest a later poll
  while preparation remains active, and surface delayed preparation failures.

## 0.2.0a29 / Plugin 0.2.0-alpha.29 — unpublished candidate (2026-09-25)

- Resolve an unbound queued follow-up through its same-owner predecessor chain
  after OAuth re-consent. Bound the walk and require a matching saved account
  before exposing any legacy operation to the new grant.

## 0.2.0a28 / Plugin 0.2.0-alpha.28 — unpublished candidate (2026-09-25)

- Keep an account-matched queued follow-up recoverable after OAuth re-consent
  when its saved predecessor belongs to the same historical grant. Unbound
  queues and a different Chat account remain private.
- Make both delayed-preparation retry tests observe the same operation's
  eventual result when a slow worker returns its acknowledgement first.

## 0.2.0a27 / Plugin 0.2.0-alpha.27 — unpublished candidate (2026-09-25)

- Let a freshly authorized grant recover an exact legacy Subchat operation
  saved under an older grant of the same owner, device, and OAuth client.
  Require the saved Chat account binding to match before crossing grants;
  unbound or other-client operations remain private.

## 0.2.0a26 / Plugin 0.2.0-alpha.26 — unpublished candidate (2026-09-24)

- Preserve compatibility with older submission readers by moving completed
  answer types into an atomic side table, including migration of existing rows.
- Complete image-generating answers with multiple images while requiring an
  unambiguous single image for the current download operation. Recover legacy
  text-and-image answers against the exact saved final turn.
- Settle intercepted browser requests when a completed generation is canceled,
  and use a fresh owned tab for the next queued turn. Bound the wait for HTTP
  headers while leaving the generation stream itself unbounded.
- Surface delayed preparation errors through the original gateway operation
  and allow an explicit retry with the same ID and unchanged input.
- Reject a changed wheel under an existing Plugin version during clean release
  packaging. The previous alpha.25 wheel remains a separate immutable build.

## 0.2.0a25 / Plugin 0.2.0-alpha.25 — unpublished candidate (2026-09-24)

- Keep a browser-prepared HTTPX generation stream alive after its HTTP 200/SSE
  headers so other Subchats can progress. Save the root conversation identity
  as it arrives and recover the final answer through authenticated history.
- Preserve late send preparation errors for status and recovery instead of
  leaving an unexplained `prepared` result. Report known model picker errors
  without exposing provider request contents.
- Bind generated images to the exact final turn, including image-only and
  text-plus-image answers. Add bounded image download verification.
- Add a macOS account-pinned Subchat setup command and clarify that UI model
  labels differ from HTTP catalog display versions.
- In a live source checkout, a new and follow-up Chat turn completed with
  HTTPX generation and history recovery. Sampled recording frames kept the
  user's fullscreen video in front. The installed Plugin and other OSes still
  require separate verification.

## 0.2.0a24 / Plugin 0.2.0-alpha.24 — unpublished candidate (2026-09-24)

- Add opt-in, account-pinned direct Subchat tools to the authenticated HTTPS
  MCP service. Existing grants require a new consent before these tools appear.
- Keep HTTP generation sends attached to the service across connection loss and
  recover by the caller's operation ID. Scope saved operations to each grant.
- Prepare Chrome account checks in background tabs. A recorded macOS OAuth
  `/mcp` send returned HTTP 200, and a separate recovery returned the final
  ordinary Chat answer without a visible Chrome window in sampled frames.
- Allow same-ID retries after safe preparation failures and keep recovery
  available when uncertain send IDs reach the gateway's capacity.
- Start the optional HTTPS Subchat gateway on discovery so an expired Chrome
  login does not stop the other HTTP tools. Retry discovery after login returns.
- Guard dedicated macOS Chrome processes across an owning Python process crash;
  the guardian stops only the profile and owner token it was given.

## 0.2.0a23 / Plugin 0.2.0-alpha.23 — unpublished candidate (2026-09-24)

- Launch the macOS dedicated Chrome profile and create its owned tabs in the
  background for browser-prepared HTTPX Subchat sends. In one recorded live
  run, new and follow-up turns completed in the same ordinary Chat while a
  YouTube fullscreen window remained in front. Chrome is still required for
  each turn; independent HTTP-only generation remains unverified.
- Match current ordinary Chat message IDs during submission recovery while
  retaining the legacy turn-key path. Keep uncertain sends unreplayed.
- Improve background Chrome cleanup and startup diagnostics. The temporary
  loopback CDP endpoint remains a local control surface while Chrome runs.
- Make the peer credential ownership check portable to Windows.

## 0.2.0a22 / Plugin 0.2.0-alpha.22 — unpublished candidate (2026-09-24)

- Add owner-bound click and fill operations to isolated browser sessions. A
  post-action observation failure returns an unknown outcome instead of
  encouraging a duplicate action. Existing-profile browser control remains
  outside this slice.
- Add an explicitly provisioned local peer MCP mailbox for Codex and Claude
  Code style clients, with scoped credentials, per-session presence, bounded
  delivery and recovery. It does not inject messages into model conversations
  or start model turns; parent Chat and Subchat integration remains open.
- Verify a21 in a live installed Codex Plugin and an ordinary ChatGPT Chat:
  both paths created a Subchat, recovered an exact final answer, and matched
  the saved child conversation. The common engine and ChatGPT HTTP service
  were updated to a21; this does not establish browser-free generation.

## 0.2.0a21 / Plugin 0.2.0-alpha.21 — unpublished candidate (2026-09-24)

- Bind explicit HTTP generation to the selected account with an authentication
  GET before reserving a send. Improve read-only startup and account-pin errors.
- Use the current conversation and Sentinel preparation values, per-endpoint
  route headers, and a fresh turn trace in the HTTP generation attempt. Track
  Cookie rotation through one in-memory HTTP client. Independent all-HTTPX
  generation remains unverified.
- Keep native GUI helper sessions usable after recognized nonfatal errors and
  report unavailable Computer Use execution contexts accurately.
- Add an opt-in browser-prepared HTTPX Subchat transport. A logged-in Chrome
  snapshot prepares each turn; HTTPX sends the single generation POST. A live
  GPT-5.6 Sol Instant new Chat and same-conversation follow-up completed, and
  HTTPX history retrieval confirmed the saved input and answer. Independent
  browser-free generation remains unverified.
- Update the ordinary Chat composer and model picker contracts, bind account
  identity from an auth GET when the browser omits its account header, and
  retain the HTTPX client through slow SSE delivery. Uncertain sends are not
  replayed.

## 0.2.0a20 / Plugin 0.2.0-alpha.20 — unpublished candidate (2026-09-24)

- Finish HTTP generation operations as `preflight_failed` when the durable
  generation dispatch claim was never made. Keep the last failed preparation
  stage and HTTP status visible without replaying the operation. Claimed sends
  remain uncertain until history proves an outcome.
- Allow a bounded Cookie in the explicit HTTP session handoff so current
  observed Chat request headers can be bound to the session. Authorization,
  account, origin, and exact Cookie matching still apply. Independent all-HTTPX
  Chat generation remains unverified.

## 0.2.0a19 / Plugin 0.2.0-alpha.19 — unpublished candidate (2026-09-24)

- Accept current observed ordinary-Chat generation header names while preserving
  exact authorization and account binding through a verified Cookie when the
  request has no account header. Keep generation route headers off preparation
  requests. This resolves a handoff compatibility defect; independent HTTPX
  generation remains unverified.
- Add a names-only request-shape comparison utility and record the headless UI
  challenge that occurs despite successful authenticated HTTP GETs.

## 0.2.0a18 / Plugin 0.2.0-alpha.18 — unpublished candidate (2026-09-24)

- Add a Claude Code marketplace and local MCP Plugin configuration. The
  installed Claude Code CLI can reach GitHub and local sources, and its bundled
  computer and Subchat MCP servers connect. Claude cloud sessions and Chat/Cowork
  still require a separately configured HTTPS endpoint.
- Log only fixed response categories for an HTTP-only generation 401/403 to
  support diagnosis without retaining the response body or secrets. Independent
  HTTP-only generation remains unverified.

## 0.2.0a17 / Plugin 0.2.0-alpha.17 — unpublished candidate (2026-09-23)

- Preserve exact pre-a16 autostart definitions when inspecting and upgrading
  existing registrations. New registrations disable Python bytecode writes;
  existing receipts remain verifiable until an explicit upgrade.
- Include Sentinel finalize in the value-free transport probe so its request
  ordering and response status can be compared without recording credentials,
  token values or message bodies. This does not establish HTTP-only generation.

## 0.2.0a16 / Plugin 0.2.0-alpha.16 — unpublished candidate (2026-09-23)

- Prevent the portable Python runtime and its supported child processes from
  writing import bytecode into the verified payload. Discard build-time bytecode
  before sealing the manifest so repeated verification remains valid.

## 0.2.0a15 / Plugin 0.2.0-alpha.15 — unpublished candidate (2026-09-23)

- Redact malformed native GUI helper responses and accept only known helper
  error codes. Invalid helper output can no longer appear in a returned error.
- ChatGPT retried an a13 pre-dispatch Subchat operation with a14's readiness
  checks and recovered a saved final answer. This confirms browser-assisted
  Plugin sending, while independent HTTPX generation remains unverified.

## 0.2.0a14 / Plugin 0.2.0-alpha.14 — unpublished candidate (2026-09-23)

- Wait up to ten seconds for an idle ordinary Chat composer and observed model
  menu during browser-assisted Subchat preparation. Reject a visible draft,
  active generation or changed conversation immediately. Surface allowlisted
  preparation reasons without exposing provider error text.
- A ChatGPT-side a13 MCP send reached the Plugin but stopped in `prepared` before
  dispatch; the same saved operation passed preparation in a later direct probe.
  The exact original failing stage was not retained, so this readiness repair
  later passed a ChatGPT-side live send check using the same operation ID.

## 0.2.0a13 / Plugin 0.2.0-alpha.13 — unpublished candidate (2026-09-23)

- Add an explicit `browser-send` mode to the bundled Subchat Plugin. It exposes
  `subchat_send` through the existing dedicated Chrome transport while keeping
  the default HTTP observation mode read-only. The two modes use the same
  logged-in dedicated profile at different times; the send mode minimizes its
  Chrome window and reports `generation_transport=browser_prepared`. A live
  catalog probe observed Chrome briefly become the foreground app despite
  minimization, so this mode is not enabled by default.
- A live local MCP session sent a new ordinary Chat and a follow-up in the same
  conversation, then recovered both final answers. Independent HTTPX generation
  is still unverified and remains a release gate.

## 0.2.0a12 / Plugin 0.2.0-alpha.12 — unpublished candidate (2026-09-23)

- Send observed Sentinel protection headers only on the generation POST. A
  current successful UI control omitted them from Sentinel and conversation
  preparation requests. HTTP-only generation remains unverified.

## 0.2.0a11 / Plugin 0.2.0-alpha.11 — unpublished candidate (2026-09-23)

- Accept the current observed Chat requirements-token header in an explicit
  HTTP generation handoff without changing its value. HTTP-only generation
  remains unverified; the known 403 is not resolved by this compatibility fix.

## 0.2.0a10 / Plugin 0.2.0-alpha.10 — unpublished candidate (2026-09-23)

- Write the standard default namespaces in OOXML package metadata. The prior
  DOCX could be read internally but LibreOffice rejected it. Verify generated
  DOCX with an isolated LibreOffice PDF conversion when available.
- Ordinary Chat HTTP-only generation remains unverified.

## 0.2.0a9 / Plugin 0.2.0-alpha.9 — unpublished candidate (2026-09-23)

- Keep Chrome-profile cookies in HTTPX's scoped, in-memory cookie jar so
  `Set-Cookie` updates reach later requests. Check a generation handoff against
  the current cookie before sending; explicit handoffs keep their behavior.
- Bound preparation JSON while receiving it and reject compressed generation
  streams before SSE decoding. HTTP-only provider generation remains unverified.

## 0.2.0a8 / Plugin 0.2.0-alpha.8 — unpublished candidate (2026-09-23)

- Reject compressed sandbox download responses and inspect raw response chunks,
  preserving the byte limit before any HTTP content decoding could expand them.

## 0.2.0a7 / Plugin 0.2.0-alpha.7 — unpublished candidate (2026-09-23)

- Bound sandbox download metadata while receiving it, so an oversized response
  cannot be buffered in full before validation. Bound file chunks before appending.
- a6 passed all five Quality CI jobs, including the Windows full suite and
  relocated portable verification. ChatGPT accepted status/capabilities, but
  blocked Subchat catalog/list before dispatch; Codex completed both tools.

## 0.2.0a6 / Plugin 0.2.0-alpha.6 — unpublished candidate (2026-09-23)

- A temporary Chat catalog tab close timeout no longer replaces the completed
  read or its original error; one bounded cleanup retry is logged if needed.
- Expose a saved final-answer sandbox file as a bounded, account-checked,
  read-only MCP download. Cross-Chat Library upload remains unsupported.
- Windows CI exercised the ConPTY resize acknowledgement; macOS CI exposed the
  tab-close race being corrected in this candidate.

## 0.2.0a5 / Plugin 0.2.0-alpha.5 — unpublished candidate (2026-09-23)

- Windows ConPTY resize now waits for a worker acknowledgement before reporting
  the new dimensions.
- Added an explicit, target-scoped Cua GUI adapter and removed unverified provider
  images from its observation output.
- Improved read-only HTTP Subchat catalog defaults and unknown-operation errors;
  refined same-build diagnostic evidence without changing authorization.
- Refuse dirty Plugin release bundles by default, pin CI action revisions, and
  provide private vulnerability reporting guidance.
- Headless Chrome-profile HTTP authentication and model catalog returned 200;
  this candidate has not yet passed new/follow-up HTTP generation acceptance.

## 0.2.0a4 / Plugin 0.2.0-alpha.4 — unpublished candidate (2026-09-23)

- Dedicated Chrome login rejects a different selected account immediately after
  the HTTP authentication GET, before requesting the model catalog.
- Explicit HTTP generation handoffs accept a successful observed request that omits
  `openai-sentinel-chat-requirements-prepare-token`. Other required headers and
  account/origin checks remain enforced; the missing header is never synthesized.
- Simplified duplicated HTTP error recording without changing retry semantics.
  Auth and catalog HTTP 200 were rechecked with the logged-in dedicated profile;
  new HTTPX generation for this candidate remains unverified.

## 0.1.0b1 / Plugin 0.1.0-beta.1 — published prerelease (2026-09-14)

- Common local Skills and hash-checked resource reads without Codex; persistent
  collection configuration through `anywhere skills-configure`.
- Windows startup recovery when the owner pipe is unavailable, while preserving
  a live unresponsive agent and its work.
- Retained MCP/HTTP clients prepare the selected agent before catalog lookup;
  authorization is rechecked after startup and dispatched writes are not replayed.
- Typed GUI calls preserve provider errors and native images. Incomplete or
  ambiguous observations cannot authorize input.
- These changes are integrated through PR #36. They are not included in the
  published alpha 9 ZIP. Beta 1 freezes existing features; remaining roadmap
  features are explicitly outside its supported scope.

## 0.1.0a9 / Plugin 0.1.0-alpha.9 — published prerelease (2026-09-13)

- ChatGPTのHTTP入口に、明示的な端末指定のdevices_list / devices_tools / devices_callを追加。
  共通エンジン構成ではCodexと端末登録を共有。既存の制限付き認可は自動で拡張しない。
- HTTP認可ごとに転送操作IDを分離。同じ端末・同じ認可で結果を回収し、応答喪失時に再送しない。
- 応答喪失後も既知のHTTPセッションIDを保持し、結果照会や終了で再利用する。
- Windows試験のシェル特殊文字を修正し、全件試験を2並列化。重点試験を先に実行する。

## 0.1.0a8 / Plugin 0.1.0-alpha.8 — 開発版

- OSによるMCP起動拒否でセッション枠が残る不具合を修正。
- 端末の同時起動上限を保護し、入力開始後の送信・観測例外をunknownとして保存。
- 端末コマンドより長寿命の所有プロセスを導入。POSIXの同じプロセスグループ、WindowsのJob内の
  子が残る間はセッションと更新ブロッカーを保持し、終了要求時にまとめて停止。
- 直接MCPの一覧に要約・全文検索・名前指定を追加。検索はページ単位、続きcursorを保持。
- Peekaboo向けgui_observe/gui_click/gui_type/gui_keyを追加。観測の所有者・期限・一回使用を検証し、
  限定した座標情報を検証して保持。座標クリックは未提供。追加モデル推論は行わない。

## 0.1.0a7 / Plugin 0.1.0-alpha.7 — 開発版

- `http-add-tools`：認証情報を保持したHTTPツール追加。旧全件認可のみ拡張し、制限付き・失効・期限切れは保持。
- HTTP公開対象54ツールの機械受け入れ試験、Peekabooによる電卓の連続操作・回収を行う明示的実機試験を追加。
- Computer Useの送信元認証拒否を、秘密本文を保存せず固定分類で診断。

- GitHub stable候補の取得、出所証明・ハッシュ・OS／CPU・API／台帳互換性の検査。
- 隔離展開、同梱Pythonの起動前検査、候補の保存、待機・中断後の再開。
- 1回の更新を実行する `anywhere update` と、開発中の常駐監視への接続。
  自動更新は既定で無効。利用者が明示的に有効化する。正式stableによる実機適用は未確認。
- Codexを経由しない直接MCP接続の内部実装。試験サーバーの状態保持と、
  Playwright MCPを通した隔離ブラウザーの実操作を確認。
- 開発版の `mcp_session_open/status/close`、`mcp_tools`、`mcp_call` を登録。
  接続元別のセッション、同時起動上限、操作ID再送時の結果回収、更新待機を検証。
  カタログのページ取得と、実行中の呼び出しを保護するアイドル終了を追加。
  画像結果のMCP中継と台帳回収を検証。一般向け配布は未完了。
- README、配布・運用・更新ガイドの整理。

## 0.1.0a6 / Plugin 0.1.0-alpha.6 — 2026-09-12

- 明示更新で選択した実行ファイルとruntime IDを保存し、通常接続時の起動先に使用。
- 切替前に中断記録を保存し、同じ候補から明示的に再開可能にした。
- 不明・破損・消失した選択先へ、古い同梱版で暗黙に戻らないようにした。
- macOSの共通エンジンと常駐版へ適用し、既存HTTP認証による接続と旧操作結果の回収を確認。

## 0.1.0a5 / Plugin 0.1.0-alpha.5 — 2026-09-12

- エンジン移行で、SHA-256名の管理対象バックアップと無関係な手動アーカイブを区別。
- 既存のMCPセッションを維持したエンジン更新と、更新前の結果回収を確認。
- macOS実環境でChatとCodexの共通エンジン利用を確認。

## 0.1.0a4 / Plugin 0.1.0-alpha.4 — 2026-09-12

- HTTP入口から認証済みローカル通信へ転送する共通エンジン経路を追加。
- 保存先の選択、台帳・転送・バックアップのオフライン移行、中断記録からの続行を追加。
- 互換APIのエンジンを、クライアントのコードハッシュが異なるだけでは置換しないよう変更。

## 0.1.0a3 / Plugin 0.1.0-alpha.3 — 2026-09-12

- macOSの常駐登録解除で、受付から実際の解除完了までの遅延を扱うよう修正。
- 台帳移行、遅れて完了した画像の回収、更新時の停止境界の競合などの監査対策を反映。
- macOS常駐版の更新と、既存HTTP接続からの再接続・旧操作結果の回収を確認。

## CI・配布基盤

公開commit `b6e0561`で、mainのQuality CIが生成した配布ZIPに出所証明を付け、
同じworkflow・commit・refの証明として検証する処理を追加しました。
run 34693834133はmacOS・Windows・Ubuntuすべて成功しました。
これは正式stableの公開や、未公開変更のCI成功を意味しません。
