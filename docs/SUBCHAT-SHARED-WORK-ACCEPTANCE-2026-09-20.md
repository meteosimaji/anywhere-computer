# Shared work acceptance, 2026-09-20

## Actual ordinary Chat trials

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
