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

## Codex context and direct MCP bridge (02:53 UTC build)

- Installed version: `0.1.0-alpha.1+codex.20260909025327`.
- Added six engine tools: selected Codex thread list/read, enabled skill list/read,
  and compatible MCP plugin catalog/call. Engine 47; local connector 53 including routing/setup.
- No runtime library dependency added. Uses the installed native Codex app-server;
  read-only context requests and direct MCP calls never send `turn/start`.
- Full pytest: **535 passed, 5 skipped, 73.78 seconds**. Ruff, strict mypy on all
  58 runtime modules, Windows-target mypy, Plugin Creator validation and skill validation passed.
- The installed version-specific uv environment matched all 58 Python source files and
  packaged web assets. Runtime identity:
  `3ee069a8002d3744a5b88a1270bcc3450f11f34538f374596d3177c1b0eb18e6`.
- The installed runtime successfully executed thread listing and enabled skill listing.
  A separate native read of the selected current thread returned 16 display messages from
  one turn with a continuation cursor; no message body was printed into the verification log.
- Native skill discovery found 405 enabled catalog entries. Reading the selected installed
  `anywhere-computer:computer-work` skill succeeded; unrelated skill bodies were not read.
- `uv run --offline python scripts/verify_codex_plugins.py` passed using a temporary
  Codex home and dummy MCP. Actual app-server method trace:
  initialize, thread/start, mcpServerStatus/list, thread/unsubscribe,
  initialize, thread/start, mcpServerStatus/list, mcpServer/tool/call, thread/unsubscribe.
  All started threads were ephemeral; no model turn was requested. All fixture worker
  PID/create-time identities had exited after completion.
- Real host discovery returned seven non-recursive server rows and 644 tool schemas.
  Two rows had zero available tools; discovery is not proof that every tool is usable.
- A real direct call to `openaiDeveloperDocs.fetch_openai_doc` obtained the public
  ChatGPT connection guide: 6,554 UTF-8 bytes, `is_error=false`, `truncated=false`,
  SHA-256 `11694ac732e312c06b92c8a29bc9cd4796bcbe075f890b5a3b50fe284fdca4ca`.
  No third-party write action was performed by this acceptance check.
- Tests cover unchanged grants, read/file profiles excluding the execution bridge,
  duplicate/schema/annotation drift, recursion, unsupported model methods,
  malformed/lost tool responses and durable unknown results with no duplicate dispatch.
- Current native-app acceptance remains incomplete: Computer Use denied access to
  both `com.openai.codex` and `com.openai.chat`. The authenticated ChatGPT web UI did
  expose Developer mode on the Pro account, currently off; account settings were not changed.
  These results do not establish a completed call from ordinary ChatGPT Chat.
- The existing long-running task's earlier plugin connection exposed 40 engine tools.
  Its real file create/read/hash-checked replace and terminal marker execution succeeded,
  but that older session is not evidence of pickup of the new six tools.

See [Codex context](../CODEX-CONTEXT.md) for supported behavior and limits. The repository
remains private; this update does not publish an official-directory listing or supply
one-command installation or production internet hosting.

## Installed transport versus existing conversation transport

2026-09-09: The current long-running conversation's existing MCP connection reported
40 engine tools and runtime ID `824bd098c25504cb5c73b50f478c6f3770b6f796cc7e96b4f691466d4ae4fd2a`.
Its registered device list contained only `local`. This does not verify a Windows connection.

Independently launched the installed `0.1.0-alpha.1+codex.20260909030721` plugin using
its actual `.mcp.json` command, arguments and cwd, with a disposable ANYWHERE_STATE_DIR
and offline uv. The official MCP Python client completed initialize, tools/list and
computer_status. It returned 53 connector tools, including all six Codex context/plugin
bridge tools, and runtime ID `6efefa2ea387858b1431fe842c7d2b4064463c720397d4a8e272f2477cfbc211`.
The dedicated agent stopped, endpoint disappeared and its native Keychain entry was
removed and checked absent. No existing conversation was resumed or replaced.

An existing conversation's tool catalog is not proof that the newly installed manifest
has been loaded. This test verifies the installed launch path, not ChatGPT UI acceptance
or automatic refresh of the original conversation's MCP connection.
