# Targeted plugin discovery and execution

The current installed Codex executable generated the protocol types used for this
review. McpServerStatus exposes runtimeStatus and authStatus. The bridge previously
discarded these fields and had only server-page pagination. It now returns bounded
summaries, query filtering and exact server/tool inspection through the existing
codex_plugin_tools tool, preserving the existing authorization scope. An exact server
inspection scans at most ten server pages; a search scans the requested single page.
No standalone MCP transport or native GUI adapter was added.

The real local catalog contained seven nonrecursive servers in this inspection;
that inventory is not a claim that every listed tool is callable. Ready-to-call
means connected with schemas, not successful execution. The isolated fixture and
one registered OpenAI documentation tool both executed successfully without any
model turn-start method. Only method names and success flags were retained:

```json
{
  "server": "openaiDeveloperDocs",
  "tool": "list_openai_docs",
  "is_error": false,
  "has_text": true,
  "inference_requested": false,
  "methods": [
    "initialize",
    "thread/start",
    "mcpServerStatus/list",
    "thread/unsubscribe",
    "initialize",
    "thread/start",
    "mcpServerStatus/list",
    "mcpServer/tool/call",
    "thread/unsubscribe"
  ]
}
```

Validation: 619 passed, 5 skipped; Ruff and mypy passed. This includes three
pre-existing probe regression tests from uncommitted workspace work, which were
not authored or modified in this change. Fresh portable verification checked
2254 manifest files, file roundtrip and a regex subprocess.

Deployment: portable/plugin-inspect-20260910/Anywhere Computer. Source/deployed
runtime ef9031fe594a8af5cf271df507e4a3c3d44a599ac96db35588211e5f5f810c54.
The first upgrade rolled back; the second succeeded. The known first-upgrade
failure remains undiagnosed. Final native registration/running and local/public
metadata probes passed. Existing permanent consent was retained.

ChatGPT settings -> plugin -> Update completed with an actions-reloaded receipt.
The visible input schema now includes server, tool, query and summary. This is
separate evidence from the local catalog, and no additional scopes were granted.
