# Research routes and measurable Subchat efficiency

This guide addresses [#222](https://github.com/meteosimaji/anywhere-computer/issues/222).
The comparison below was checked against primary documentation on 2026-09-29.
It identifies useful implementation patterns; it does not establish a benchmark win.

## Route the work to the smallest useful tool

Use the ordinary Chat's native search for fresh public facts when available.
Open the actual primary page before treating a search snippet as evidence.
Use Anywhere Computer's browser for interactive pages, authenticated sessions,
semantic controls, source inspection, console/network diagnostics, downloads,
and screenshots. Record publisher, source title, URL, observation date and the
claim supported. A returned URL alone does not prove the page was read.
Selected-account tools require their own grants; `work_context` records
provenance without giving a child the parent's tools, files or history.

Keep a task in the current Chat when its phases share context, require repeated
clarification, or take less time than delegation and recovery. Separate only
independent, bounded questions with the exact input needed and a concise output
contract. Allocate stable intent and request IDs before sending. Recover an
unknown send with those identities; do not create another Chat to obtain a
clean-looking response. No limit is inferred from numbers inside prompt text.

## Primary-source comparison with current code

| Tool / documented behavior | Anywhere Computer evidence and next useful improvement |
| --- | --- |
| [Playwright MCP](https://github.com/microsoft/playwright-mcp) uses accessibility snapshots; its README recommends considering CLI + skills for coding-agent context efficiency. [Playwright CLI](https://github.com/microsoft/playwright-cli) stores snapshots in files and supports partial/depth-limited snapshots and finding matching nodes. | `browser_control.py` already binds observations to actions and bounds result size. Scope observations to the relevant page/frame and request only needed content. Measure tool-output bytes and available token counters before asserting that switching transport saves usage. Ordinary Chat may lack the coding agent's shell access. |
| [Chrome DevTools MCP](https://github.com/ChromeDevTools/chrome-devtools-mcp) offers traces, performance insights, network inspection, screenshots and console diagnostics. Its [tool guidance](https://github.com/ChromeDevTools/chrome-devtools-mcp/blob/main/skills/chrome-devtools/SKILL.md) uses page IDs, snapshot element IDs, filters, pagination and file output for large results. | Anywhere Computer already exposes bounded `browser_network`, `browser_console`, `browser_source` and media observations. Preserve those cursors and fetch detail after selecting an event. A network log is evidence of a request, not proof that the model understood its response. Full performance-trace analysis remains distinct from current request diagnostics. |
| [Stagehand v4 caching](https://docs.stagehand.dev/v4/best-practices/caching) keys eligible results on instruction, page content and options. Its managed cache requires Browserbase; local-browser calls still run inference. Cache status is observable. | Anywhere Computer's operation receipts prevent replay but are not an inference cache. Reusable recipes may save rediscovery when their input and page state match, but must reobserve semantic targets after navigation. Never cache a completed side effect as permission to execute it again. Do not advertise Stagehand's hosted-cache behavior as a local plugin feature. |
| [Claude Code subagents](https://code.claude.com/docs/en/sub-agents) have separate contexts and scoped tools; their requests consume the same account limits. Its guide recommends the main conversation for shared-context or latency-sensitive work, and subagents for independent work that returns a summary. | Subchat's durable receipt/intent store and caller-supplied `work_context` support traceable work, but a normal Chat is not a Codex subprocess with inherited files/tools. Measure setup, dispatch, waiting, evidence review and synthesis, not just the children's generation time. |

Built-in Chat search and an external plugin are separate integration surfaces.
The [ChatGPT search guide](https://help.openai.com/en/articles/9237897-searching-the-web-with-chatgpt)
describes cited Chat search; the [Responses API search guide](https://developers.openai.com/api/docs/guides/tools-web-search)
describes API controls. Neither implies that a plugin can invoke the ordinary
Chat's internal search transport. This iteration adds measurement rather than
an undocumented search endpoint or an unmeasured task planner.

## Local measurement tool

`scripts/measure_subchat_research.py` has three commands: `plan`, `capture`, and
`report`. It never sends a message, opens a browser, or modifies the ledger.
The plan explicitly allocates one baseline worker and a chosen number of parallel
workers. The count is a benchmark input, not a production child limit.

Prepare a small public task file and a selection JSON with the exact observed
`model`, `effort` and optional `http_selection`. Then create the plan:

```sh
uv run --locked python scripts/measure_subchat_research.py plan \
  --task-file /absolute/private/task.md \
  --selection /absolute/private/selection.json \
  --parallel-children 3 --output /absolute/private/plan.json
```

The plan records a task hash, separate run IDs and one stable intent per worker.
Before any send, save an empty `start` snapshot for that arm:

```sh
uv run --locked python scripts/measure_subchat_research.py capture \
  --plan /absolute/private/plan.json --mode single_chat --phase start \
  --database /absolute/private/operations.sqlite3 --unowned \
  --output /absolute/private/single-start.json
```

Use the actual connection's owner via `--owner` instead of `--unowned` when
required. An absent owner scope never expands to all accounts. In each send,
set `work_context.task_id` to the arm's `run_id` and include the task file's hash
in `work_context.inputs`. Attach its actual contents or public source URLs in
the prompt: the reference metadata does not make a local file readable by Chat.
Save the returned original operation ID alongside its intent before polling.

Capture `checkpoint` snapshots during work and one `final` snapshot after
receipt recovery, evidence review and final synthesis. The parallel arm uses
`--mode parallel_subchat`. Every snapshot must have a distinct output path;
existing evidence is never overwritten. The reader discovers all submissions
with the run tag inside that owner, so an accidentally created eighth child is
visible even if only three intents were planned.

```sh
uv run --locked python scripts/measure_subchat_research.py report \
  --plan /absolute/private/plan.json \
  --snapshots /absolute/private/single-start.json /absolute/private/single-final.json \
  /absolute/private/parallel-start.json /absolute/private/parallel-final.json
```

The public JSON omits prompts, answers, account identifiers, conversation URLs,
provider message IDs, local paths, operation IDs and intent keys. Private
snapshots retain operation/intent identities and hashed receipt bindings for
local reconciliation. They contain no raw Chat content. Bounds are 256
operations per arm, 128 snapshot files, 2 MiB per file, 16 MiB of selected ledger
bodies, and a five-second SQLite query deadline. Unsupported or incomplete
inputs produce a fixed error without echoing private validation data.

### Interpreting measurements

- `workflow_wall_seconds` spans the empty start to the final observer snapshot.
  Include preparation, dispatch, waiting, tool use, verification and synthesis
  in that window. Shared one-time catalog setup should be recorded separately
  and described equally for both arms.
- `actual_child_count_lower_bound` counts distinct confirmed new conversations.
  `child_count_complete=false` means at least one send still lacks a provider
  receipt. It is not permission to send again. Also inspect unexpected intents,
  missing planned intents, not-sent operations and completed operation count.
- HTTP overlap uses retained `generation_request` through `history_final`
  checkpoints. The latter is observation time, not the provider's internal
  finish time. Missing/expired/out-of-window checkpoints make overlap `null`.
  The ledger retains only 64 HTTP checkpoints per operation; ordinary UI sends
  may have no complete HTTP interval.
- `tool_calls` is an optional observed count supplied by the reviewer. It stays
  `null` when the transcript or counter is unavailable; the script does not infer
  calls from answer length.
- The optional `rubric` contains accuracy, source support, completeness and
  traceability, each scored 0–2, plus a hash of the reviewed final output.
  Zero means missing/wrong, one partly met, two fully checked. These are explicitly
  reviewer scores, not automatic factual verification. Preserve the reviewed
  output and source checks locally.
- Optional `usage` counters require provider, unit, coverage, an identical meter
  hash across arms, an evidence-file hash, and actual before/after values. Use
  `whole_workflow`, `workers_only`, or `account_window` coverage honestly. Reset,
  negative and nonfinite counters are rejected. Only matching whole-workflow
  counters are compared; account percentages are not a per-run bill.
- This ledger currently records no model token counts. Without an independently
  available counter, usage and usage comparison remain `unavailable`. One pair
  cannot establish repeatable savings; the report keeps `savings_conclusion` as
  `not_established` even when recorded counters differ.

## Bounded real acceptance

Use one combined ordinary Chat and three independent children, exactly four
new Chats, with the same observed model/effort and one repetition. Save all four
request IDs and intent keys before dispatch, coordinate exclusive use of the
selected browser profile, and stop sending once those four sends are reserved.
An unknown send is recovered under its original ID. Do not change model after
a failure and describe the result as a same-model comparison.

Use three fixed primary-document questions:

1. From Playwright MCP/CLI documentation, which representation supports semantic
   interaction, and which documented output controls reduce unnecessary context?
2. From Stagehand v4 caching documentation, what forms the cache key and does
   managed caching apply to a local browser?
3. From Claude Code subagent documentation, when is one conversation preferable,
   and do subagent requests consume separate or shared usage limits?

Give the combined Chat all three questions; give each parallel child one.
Require concise answers with exact official source titles/URLs, observation
status, and a truthful statement of the search/open tools actually used. If
browsing is unavailable, require that limitation instead of an invented claim
of having opened a source. Self-reported tools are not verified tool traces.

Expected facts for reviewer scoring: Playwright accessibility/semantic targets
and scoped/file-backed snapshots; Stagehand instruction/page/options keys,
model configuration excluded and Browserbase-only managed cache; Claude's
shared-context/latency tradeoff and shared account usage limits. Source support
requires checking the exact cited primary page. Combine child findings into the
same final answer format as the baseline, then capture the final observer time.
Record any missing search capability, overlapping work, extra Chat or recovery
cost instead of silently replacing an unsuccessful arm.

## Real diagnostic run, 2026-09-29 to 2026-09-30

The first authorized pair did not produce a valid speed or quality comparison.
The fresh catalog offered GPT-5.6 Sol / Instant, with the exact HTTP selection
`version_id=5.6`, `preset_id=0`, `model_slug=gpt-5-6-instant` and
`thinking_effort=null`. Four request IDs and four intent keys were saved locally
before execution. The combined baseline was invoked once; the three independent
workers remained unsent after the baseline did not progress.

| Arm | Planned workers | Attempted sends | Receipt | Actual new-Chat count |
| --- | ---: | ---: | --- | --- |
| Combined baseline | 1 | 1 | Unconfirmed | Unknown; confirmed lower bound 0 |
| Independent parallel workers | 3 | 0 | Not attempted | 0 dispatched by this benchmark |

The baseline's initial ACK was `running/prepared`; later observations remained
`sending/unconfirmed`. Forty bounded waits and the one send call occupied a
483-second controller session, including startup and shutdown. No provider user
message or conversation identity was saved. The confirmed new-Chat count is
therefore a lower bound of zero, not proof of zero provider effects. The original
operation stays unknown and was not resent or retrospectively changed to
`not_sent`. After controller shutdown, zero owned background Chrome processes
remained. Raw prompts, DOM, account data and operation IDs remain private.

The exact owned page retained the inserted draft and a Send button on the Chat
root page. A read-only DOM capture succeeded; the screenshot attempt raced the
bounded controller shutdown and failed with `TargetClosedError`. An isolated
render of the extracted editor was inspected, but is not a screenshot of the
live provider page. Re-evaluating the current draft comparator against that
saved editor reproduced refusal: the expected input had 664 characters and
the canonical DOM had 663, with only its terminal line feed missing. Ordinary
isolated Chrome contenteditable insertion preserved terminal line feeds in a
separate check. This demonstrates a rejected draft shape after the provider's
editor processing; it does not recover the original worker exception or prove
which editor step removed the character.

The resulting fixes preserve exact text rather than trimming it:

- The guarded browser task distinguishes a refusal before its Send click from
  an observed manual dispatch gesture. Only the HTTPX interception path with
  no claimed generation request, blocked delayed requests, and a verified close
  of that exact owned page can become terminal `preflight_failed` / `not_sent`.
  This applies to an insertion that failed its immediate text check. Later draft
  changes may be user edits: those pages remain open. Manual gestures and
  unconfirmed cleanup also retain an unknown outcome.
  If a rejected page stays open, a page-scoped abort-only route remains until
  it closes. The previous abort guard is retained if its replacement cannot be
  confirmed; cleanup never restores that page's generation access. Replacement,
  removal of the old route and HTTPX client cleanup each have a five-second
  deadline, and the replacement handler does not capture the send credentials.
- `subchat_status` and `subchat_wait` include `send_worker`, a session-local
  state (`running`, `failed`, `cancelled`, `finished`, or `not_owned`). A failed
  worker has a fixed reason code; no provider exception text is returned.
  `subchat_activity.failed_send_workers` distinguishes failed workers from active
  ones. `not_owned` after restart does not imply that the provider never received
  the message. Durable `draft_rejected` and `send_worker_failed` checkpoints keep
  only a fixed stage and timestamp. Receipt recovery remains available even
  after the local worker failed.

Synthetic regressions cover the observed missing-LF shape, a delayed request
during cleanup, manual dispatch, later user edits, unconfirmed page closure, and late worker
failure followed by successful receipt recovery. The old implementation fails
the missing-LF classification and late-worker visibility regressions. These
tests verify the new failure handling, not provider acceptance of terminal LF.
An additional real-Chrome regression reproduced a POST escaping to the provider
mock after unconfirmed cleanup returned. It now stays blocked, including failed
or stalled guard replacement, stalled route removal and stalled client cleanup.
The same tests verify that the block does not affect another page after the
rejected page is closed.

No final Chat answer was obtained, so native web-search availability, tool traces,
source-reading evidence and output quality are unmeasured. The parallel arm,
generation overlap and attributable Chat-token or Codex-run usage counters are also unavailable. There
is no measured speedup or Codex-usage saving. A new paired run needs explicit
authorization and a fresh predefined input; these old identities must not be
reused as replacement sends.
