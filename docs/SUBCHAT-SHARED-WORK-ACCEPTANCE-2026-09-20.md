# Shared work acceptance, 2026-09-20

## Hidden-tab transport and provider choice observation

A later ordinary-Chat follow-up to the implementation reviewer appeared idle in
the owning-app read projection while the actual page showed a choice between
remaining in Chat and continuing in Work. Selecting Stay in Chat allowed the
existing request to continue and finish; no new prompt was sent. The reviewer
produced `/tmp/ac-audio-integration-review.md` and reported nine non-capturing
audio/package tests passing. The parent read the artifact. This is a live
provider-choice observation, not proof that the production subchat adapter
detects or resolves that choice. Do not classify every missing answer as Thinking
or silently select Work.

The browser adapter now selects the visible Stay in Chat button only after
confirming the saved user message and conversation, and only when exactly one
Stay control and one Continue in Work control are visible. It then leaves the
submission pending for a later answer observation. The existing offline browser
integration reproduces the old indefinite wait and verifies Japanese/English
choices, missing/ambiguous controls, zero Work starts and no second Send. This
automated change has controlled-browser evidence; the earlier live continuation
was a controller action, not a live acceptance of this new adapter code.

A separate benign follow-up in the copy-review conversation explicitly selected
the observed GPT-5.6 Sol/high option. One UI send produced a visible user receipt
and the exact final answer `AC_TRANSPORT_PROBE_20260920`. The tab remained hidden.
Network observation found POST `/backend-api/f/conversation/prepare` returning
JSON and POST `/backend-api/f/conversation` returning HTTP 200 with
`text/event-stream`. The send included authentication, browser preparation state
and Sentinel verification headers. No credential values are retained here.
The observed model identifier is evidence for that request, not a constant for
future model selection. The isolated send observation reported no event loss;
the earlier page-load capture was truncated and is not a complete trace.

This proves browser-mediated HTTP/SSE transport, not independent browser-free
authentication or renewal. No standalone authenticated replay or challenge
bypass was attempted. New-conversation creation, expiry, browser termination and
stream reconnection were not covered by this probe. Preserve the existing ledger
and transport boundary: a hidden browser is still a browser dependency, and HTTP
success alone is not proof of a completed answer. Saved-state CLI operations
without Playwright have separate package-level evidence; they do not establish
browser-free live Chat submission.

## Actual ordinary Chat trials

### HTTP lifecycle and Codex readback follow-up

A hidden test tab was reopened at the previously known copy-review conversation.
The model menu initially reported Latest; the controller explicitly selected
GPT-5.6 Sol/high before sending. The bounded model-selection observation contained
usage/subscription reads, not a dedicated model-change POST. This does not prove
that model changes never make network requests. The next generation POST carried
the observed model and effort; these values are not a future model catalog.

One deliberately long, tool-free test request used POST
`/backend-api/f/conversation` and received HTTP 200 `text/event-stream`.
Clicking Stop caused a separate POST `/backend-api/stop_conversation`, with
`conversation_id` and `exclude_async_types` body keys, returning JSON HTTP 200.
Some more output appeared after clicking Stop before the UI became idle. Treat
stop requested and stop observed as different states. This trial does not prove
immediate server-side cancellation or termination of separately running tools.

A subsequent same-conversation request was sent once and returned SSE HTTP 200.
The exact final `AC_FOLLOWUP_AFTER_STOP_20260920` was verified both in the UI and
through Codex `read_thread`, with the corresponding exact user prompt, user ID
and answer ID. Thus app-MCP readback can recover this final without copying from
the page. It is not evidence that a standalone Anywhere process can authenticate
to that app MCP. The stopped earlier turn also appeared as `completed` in this
projection (its long text was explicitly truncated at the requested limit).
Keep local cancellation and truncation records; do not infer successful task
completion solely from that projected status.

The generation request exposed an Authorization header and Sentinel/conduit
verification header names. Browser cookie metadata included split HttpOnly,
Secure session cookies. Values were not exported. The request's ordinary CDP
event did not show Cookie, and matching extra-info was unavailable, so actual
cookie transmission and cookie-to-Authorization derivation remain unverified.
The later reload trace was truncated and cannot establish the auth refresh
sequence. A completed SSE response-body lookup returned no data; this trial
does not establish standalone SSE body parsing or browser-free auth renewal.

A subsequent targeted response-body capture succeeded for
`/backend-api/models`: 21 model entries, three version groups and their
intelligence presets. The captured mapping distinguished Latest/Pro from
5.6/Pro, and exposed per-model `is_work_mode_model` and thinking efforts.
These are dated account-specific observations, not hardcoded supported models.
Using the exact observed catalog URL (including the same query parameters),
two read-only in-origin HTTP trials returned 200: cookies without an explicit
Authorization header yielded six model entries; adding the Authorization from
this same session's observed request yielded 21, including both Pro versions.
No token values were printed or persisted. This demonstrates an authenticated
catalog GET, not browser-independent login/refresh or authenticated generation
replay. Do not mistake a 200 response with a reduced public catalog for the full
account catalog. `/backend-api/tpp/models/` was also observed returning JSON
200; its semantics were not established by this trial.

The separate external app-MCP preflight found a provided live endpoint, but
the actual subprocess catalog call failed with RPC -32603. In-app `read_thread`
succeeded as described above. Keep these two connection outcomes separate;
there is no verified standalone readback bridge yet.

The existing readback probe now retries only `ConnectionError` within its
bounded read window, preserving exact message identity and returning unknown
without resend on exhaustion. Empty projections and Thinking remain pending;
permission/RPC errors propagate. Its matcher also accepts caller-supplied known
interrupted message IDs, preventing a stopped turn's completed projection from
being promoted. This is a readback helper contract, not automatic detection of
unobserved user stops or a new production stop endpoint. Controlled regressions
cover disconnect -> empty -> Thinking -> answer, permanent disconnect, known
stop, and permission errors.

`scripts/probe_subchat_transport.py` attaches only to one exact existing Chat
tab over an existing loopback CDP endpoint. It observes at most 300 seconds,
retains bounded allowlisted metadata and reports dropped events. It does not
read request headers/bodies/cookies, send prompts, create tabs, navigate, focus,
or close the browser. Example (requires a developer-provided existing endpoint):

```sh
.venv/bin/python scripts/probe_subchat_transport.py \
  --cdp-endpoint http://127.0.0.1:9222 \
  --page-url https://chatgpt.com/c/KNOWN-CONVERSATION-UUID \
  --seconds 30 --output /tmp/subchat-transport-new.json
```

The script is a diagnostic, not a product HTTP backend. Its real offline browser
test verifies request/response correlation, no retained secrets or unrelated
requests, listener removal and preservation of the existing page. It does not
certify live authentication, cancellation, or assistant completion.

The development browser adapter created ordinary Chat A with GPT-5.6 Sol and the
observed medium effort label. Through the installed Anywhere HTTP plugin, that
Chat created Python code in a dedicated temporary directory and reported a zero
exit code with sum 16 and mean 5.333333333333333. A same-conversation follow-up
read and hash-conditionally updated that file; its new output was sum 26, mean 6.5,
count 4. Independent host reads and executions confirmed both versions/results.

Fresh ordinary Chat B discovered the same installed runtime and read the same
file. Its reported current values and SHA-256 matched the host file. It attributed terminal startup rejection to ChatGPT's safety checks. This is
the model's report, not an independently established root cause; its execution
and output retrieval did not pass. No alternative execution path was dispatched
by the controller to override this rejection. This difference is not evidence
that changing command spelling or selecting another model repairs authorization.

A direct installed-plugin status read separately confirmed ready, version
0.2.0a1, runtime ID a82ce393e115c5b1025a0c8777823d74357ee120f660892d27aca78d71084c53,
and zero active sessions. The installed file/terminal runtime is distinct from
the development subchat adapter under test.

## Deterministic engine acceptance

`test_shared_file_handoff_rejects_stale_edit_and_survives_restart` sends two
independent sequences of real engine requests against one temporary workspace:

1. Create code and read it from both sequences.
2. Update through one sequence and reject the other's stale hash replacement.
3. Verify the winning content remains, then execute that file and collect 42 with
   exit code zero.
4. Close/reopen the engine and recover both the file and a recorded operation.

This uses the actual file, terminal, and ledger implementations without a model.
It does not simulate browser identity, HTTP authentication, or model approval.
Arbitrary shell writes and external editors do not acquire the file API's hash
precondition merely by sharing a directory.

## Outstanding product work

The controller supplied the same path explicitly in the two prompts. There is
still no persistent shared-work object that binds parent and child Chats to an
authorized device/workspace or verifies each Chat's tool connection. That product
integration, two-Chat editing acceptance, stale-edit rejection through actual
Chats, unavailable-device behavior, and broader model/effort coverage remain open.
CoS requirements inform this design; CoS runtime ownership is not a dependency.

## Acceptance policy and HTTP reproduction

Per the user's clarified acceptance criterion, deterministic reproduction through
the MCP/HTTP tool boundary is the functional gate. Live Chat trials supplement
that gate with observations about discoverability, UI changes, and usability.
A model-reported platform rejection alone does not establish a plugin defect.

The existing `test_code_write_run_edit_and_recover_from_fresh_http_client` uses
the actual MCP SDK and loopback HTTP server, real files, and real subprocesses.
It opens a fresh client, discovers tools, creates Python/CSV inputs, executes the
code, reconnects with a new MCP session, recovers a prior operation, updates the
code, and executes again. Reusing a terminal request ID must not create a second
process. The test now also attempts a stale hash edit through `tools/call` and
verifies failure, preservation of the winning file, and successful execution of
that file. Authentication in this fixture is static; OAuth/grant checks have
separate tests. This does not reproduce ChatGPT's model or platform policy.

## Nested subchat transport acceptance

`test_http_direct_mcp_subchat_receipt_and_process_restart` exercises actual SDK
requests through HTTP -> Anywhere engine -> direct stdio MCP -> subchat SQLite.
It discovers the child tools, sends a Unicode prompt, receives `sending`, waits
for the recorded answer, closes the child, opens another HTTP connection and
child process, and repeats the same submission ID. The provider's persistent
send counter remains one. Only the browser/model provider is deterministic;
this is not an authenticated ChatGPT service or OAuth acceptance test.

The direct-MCP session currently serializes complete calls. Consequently a
`subchat_wait` invoked through that same session occupies its call slot until
return, even though the inner subchat browser lock is released between polls.
Use short bounded waits/recover calls on this route. Parallel SDK requests to the
subchat server directly and nested Anywhere calls have different concurrency
properties; the inner lock regression must not be advertised as end-to-end
parallel scheduling. Task scheduling and authenticated workspace binding remain
unfinished product work.

## Parallel ordinary Chat work: preparation fixed, automatic recovery incomplete

Two distinct ordinary Chat submissions used the live observed `GPT-5.6 Sol`
model and medium effort. Submission preparation shared verified arrow navigation
with model discovery after an offline regression demonstrated that an ignored
Home key left the previous maximum effort selected. Eighty subchat tests passed.
The same dedicated browser remained running for both submissions.

The review Chat created `/tmp/ac-subchat-review-20260920/test_review.py` through
its available tools. Its close-during-direct-send finding was independently
reproduced locally before implementation; the session now joins direct calls and
rejects new calls after close. The resulting 81-test subchat suite, full source
mypy, and targeted Ruff checks passed. The other proposed regressions were not
accepted merely because the Chat called them bugs.

The ideas Chat produced a completed answer, independently visible through a
second browser view. It recommended state/reason diagnostics, but also suggested
retrying an unknown send under a new ID; that suggestion was rejected. No fresh
competitor-source verification was demonstrated by this answer.

Automatic recovery is not accepted yet: the review prompt was visibly saved with
`test_review\.py` (one literal backslash before the dot) instead of the submitted
`test_review.py`, so exact receipt matching failed. The ideas answer was complete
in the UI while adapter recovery still returned `submitted`. No resends, forced
completion records, or relaxed text comparisons were used. These are live defects
or unresolved observations, not evidence that two-tab result recovery passed.

A later read of the known ideas conversation URL recovered the exact user text
and completed answer through the production adapter, without resending. The
saved operation became `completed`. This establishes recovery after reopening
that conversation, not the cause of the original tab's stale observation.

The review's queued-cancel finding was also independently reproduced: local
preparation survived successful cancellation and retained the browser lock.
Cancellation now joins the matching local preparation, including direct sends,
only after the store accepts cancellation. It never cancels already-dispatched
generation. Waiting observers receive the saved cancelled state. All 83 subchat
tests and full source mypy passed; the review's ordinary read-timeout cancellation
claim is not treated as a defect without evidence of harmful effects.

Preparation failures now return `preparation_failed` with `dispatched=false`,
separately from a send whose receipt is unknown. A regression test fails on the
previous generic error response, verifies that preparation failure never calls
Send, retries the same identity after correcting preparation, then loses the
receipt and confirms that a repeated request does not send again. Provider error
text is not returned. The live high-effort follow-up remains `prepared`; no
receipt or completed review is claimed for that attempt.

A later read of the known conversation found the high-review prompt and its
completed answer while the controller record remained `prepared`. No controller
Send or `begin_send` was recorded for that attempt; the actor/path that submitted
it is not established. The copied user message also expanded bare URLs into
Markdown links, so exact receipt matching has not been demonstrated. The earlier
statement that this live attempt was unsent describes the local ledger only, not
the remote conversation. Do not replay it or rewrite the ledger to claim success.
The answer and its local reproduction artifacts can be reviewed independently.

The review's cancelled-draft finding was independently reproduced with the
current source and the supplied offline browser fixture: the cancelled operation
retained its tab/draft, while a peer Chat still submitted. This is a cleanup issue,
not evidence of duplicate delivery. Closing such a tab must preserve unrelated
user edits and already-dispatched work; no unconditional tab cleanup is adopted.

Upstream was rechecked at `8f76ccc790917b01ee758da6687a1cf9b576ba8a`. Open CoS
issues [326](https://github.com/totec448-spec/chat-on-steroids/issues/326) and
[327](https://github.com/totec448-spec/chat-on-steroids/issues/327) describe stale
pending/waiting states. Their useful requirement is evidence-based delivery
diagnostics and actionable recovery. Time alone is not adopted as permission to
replay an uncertain send or stop a legitimately Thinking model.

The user subsequently reported that they may have pressed Send. The browser
adapter now finishes model/effort/history preparation before exposing any prompt.
The service saves `sending` and the baseline first; only then does the adapter
insert the draft. Final draft/history/URL checks and the click run in one browser
task. An observed DOM change after manual send or edit leaves the reservation
intact and prevents a second click. Controlled browser regressions reproduce the old pre-reservation failure,
then verify manual-send receipt recovery with one click and preservation of a
user replacement draft with zero clicks. This is not proof of the original
operator's exact actions or live serialization recovery for the earlier prompt.

The new ordinary high-effort review completed through the adapter's own
`subchat_recover`, including the exact prompt receipt and answer identity:
conversation `6aaf8dd8-f038-83e8-be31-5195d4b83a39`, user message
`e8e541eb-0033-44ab-9c22-80239065714c`. It produced a separate reproduction under
`/tmp/ac-subchat-manual-review`. Independently rerunning that test against its
pinned old wheel confirmed two sends; the companion no-draft-before-reservation
regression passes against the current source. These are controlled browser tests,
not a claim that every manual click race or historical serialization mismatch
is resolved. The old-payload review also identified the existing pre-identity
process-loss limitation; no timeout-based replay or extra runtime was adopted.

Two further ordinary Chats used the installed Anywhere direct-MCP path with
GPT-5.6 Sol at its observed high-effort setting. Both receipts and final answers
were recovered through `subchat_recover`: conversations
`6aaf90ef-f7ac-83ee-a0ef-0a5624be14e5` (implementation review) and
`6aaf9138-20f4-83ee-b3f6-2a7b42bad9fe` (copy evidence). Their reported runtime
`0.2.0a1`, instance `c463943ae1ef4eb68613ffb27a6703af`, matches the parent's
status observation. The installed engine routed to the development subchat
entry; this is not evidence of a globally updated installed wheel.

The implementation reviewer produced a delayed-DOM manual-send fixture. The
parent read and independently ran it: two click effects were reproduced against
the merged source. It does not prove a live ChatGPT double submission. The
adapter now observes Send clicks, composer submit events and non-composing Enter
before exposing the draft. An intervening gesture prevents the automatic click,
even with unchanged DOM; it does not establish successful delivery. Listeners
are removed after the attempt. Controlled click/Enter/submit regressions fail
before this change and pass after it, preserving the unknown reservation until
the independently published message receipt appears. User input is not blocked.

The same ordinary Chat received a queued follow-up, with message identity
`4cd1cbd2-288b-4f75-81d1-04c63838fbb9`, and returned a completed review of
`b00351b2c1801a48dafd10fd4f28c208ba6aec4f`. It verified the delayed-gesture cases
and listener cleanup but identified a non-submit accessory button's Enter as a
false positive. The parent independently reproduced that predicate failure and
narrowed Enter observation to the composer textbox. The regression also checks
Shift+Enter, composing Enter, ordinary editor Enter and listener removal. This
review and follow-up used the actual plugin; its UI-race tests remain offline.

The copy reviewer demonstrated why Copy plus rendered text need not identify
the original Markdown source uniquely. No broad unescaping, URL normalization,
substring matching or promotion of historical uncertain sends is adopted.
Dispatch-bound serialization evidence remains a proposal requiring independent
verification; it is not an exact-source recovery guarantee.

The lazy-start CLI was additionally checked from the extracted bundled wheel in
a temporary working directory, verifying its import path rather than relying on
the editable checkout. The actual JSON-lines subprocess returned saved status,
completed recovery and unsent cancellation. The actual stdio MCP subprocess,
through the SDK client, initialized, listed tools and returned saved status and
the completed answer. Both exited successfully without creating the supplied
Chrome profile. These checks used an isolated seeded ledger, not a new live Chat.
The browser dependency remains installed; the result establishes that these
saved-state operations do not launch Chrome, not a browser-free new-Chat API.

A follow-up review was also sent to ordinary conversation
`6aaf90ef-f7ac-83ee-a0ef-0a5624be14e5` through the owning Codex app's send tool,
without browser typing. Its read tool returned the exact new user message
`cf4abf62-1a3e-4623-8588-d01c38cf055a`. At this observation no corresponding
assistant answer was available. Receipt is confirmed; review completion and
standalone Anywhere access to that app transport are not established. The
request is not resent merely because a later status says idle.

That review subsequently completed (assistant message
`f814c699-3017-478f-87a5-80edfbffc642`). The parent inspected and independently ran
its three isolated factory/reuse/cleanup tests; all passed. PRs 80 and 81 then
merged after all five CI jobs passed, reaching main `8a0e9f2`.

The same two ordinary Chats subsequently shared a temporary CSV workspace. A
created and ran a Decimal-based Python summarizer; B read the same files,
verified their hashes and independently ran it (total `21.60`). A conditionally
appended a row; B's old-hash write was rejected, and the parent independently
confirmed the retained updated file and total `23.00`. B recovered the rejection
through its original HTTP connector with operation
`c0f11c7a2d8445cf84e3f64f2d6b7a10`. The parent's local lookup returned unknown:
HTTP grant namespaces are distinct from local operation IDs. The source and
18 HTTP/remote tests independently confirm the scoping mechanism. This is not
permission to bypass namespaces or to replay a failed lookup through another
connection. It also does not establish per-subchat authorization or a product
work-context binding; the parent supplied the exact workspace in each request.
