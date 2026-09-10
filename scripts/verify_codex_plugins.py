#!/usr/bin/env python3
"""Isolated smoke test for the direct Codex MCP-plugin bridge.

The fixture owns its temporary CODEX_HOME and MCP server.  It never reads the
user's Codex configuration and only exercises the two public bridge functions.
"""

import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

import psutil

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


WORKER = '''
import json, os, sys, psutil
log = sys.argv[1]
pid = os.getpid()
with open(log, "a") as out:
    out.write(json.dumps({"pid": pid, "create_time": psutil.Process(pid).create_time()}) + "\\n")
for line in sys.stdin:
    try: req = json.loads(line)
    except Exception: continue
    if not isinstance(req, dict): continue
    with open(log, "a") as out:
        out.write(json.dumps(req.get("method"), separators=(",", ":")) + "\\n")
    if "id" not in req: continue
    method = req.get("method")
    if method == "initialize":
        result = {
            "protocolVersion": "2025-06-18",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "probe-echo", "version": "1"},
        }
    elif method == "tools/list":
        result = {
            "tools": [{
                "name": "echo", "description": "fixture echo",
                "inputSchema": {
                    "type": "object", "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            }],
        }
    elif method == "tools/call":
        text = ((req.get("params") or {}).get("arguments") or {}).get("text", "")
        result = {"content":[{"type":"text","text":str(text)}],"isError":False}
    else:
        result = {}
    print(json.dumps({"jsonrpc":"2.0","id":req["id"],"result":result}), flush=True)
'''


async def main() -> None:
    import anywhere_computer.codex_plugins as bridge  # noqa: PLC0415
    from anywhere_computer.codex_plugins import (  # noqa: PLC0415
        call_codex_plugin_tool,
        list_codex_plugin_tools,
    )

    # Use the bridge's resolver: launchd may supply an explicit executable while
    # its PATH deliberately omits the user's interactive-shell commands.
    codex = bridge._executable(None)
    if shutil.which(str(codex)) is None:
        raise RuntimeError("configured Codex executable is not executable")
    with tempfile.TemporaryDirectory(prefix="codex-plugin-smoke-") as raw:
        root = Path(raw)
        home = root / "codex-home"
        home.mkdir()
        worker = root / "echo_worker.py"
        worker.write_text(WORKER, encoding="utf-8")
        log = root / "mcp-methods.log"
        config = home / "config.toml"
        # JSON string quoting is also valid TOML basic-string quoting, including
        # paths containing spaces, quotes, or backslashes on Windows.
        config.write_text(
            "[mcp_servers.echo]\n"
            f"command = {json.dumps(sys.executable)}\n"
            f"args = [{json.dumps('-I')}, {json.dumps(str(worker))}, {json.dumps(str(log))}]\n"
            "startup_timeout_sec = 5\n"
            "",
            encoding="utf-8",
        )
        old_home = os.environ.get("CODEX_HOME")
        os.environ["CODEX_HOME"] = str(home)
        try:
            calls: list[tuple[str, dict[str, object]]] = []
            original_request = bridge._Session.request

            async def recording_request(session: object, method: str, params: dict[str, object]):
                calls.append((method, params))
                return await original_request(session, method, params)

            bridge._Session.request = recording_request
            catalog = await list_codex_plugin_tools(str(root), limit=30)
            rows = catalog.get("servers")
            if not isinstance(rows, list) or len(rows) != 1:
                raise AssertionError("fixture catalog did not contain one server")
            row = rows[0]
            if not isinstance(row, dict) or row.get("server") != "echo":
                raise AssertionError("fixture server name mismatch")
            tools = row.get("tools")
            if not isinstance(tools, list) or len(tools) != 1:
                raise AssertionError("fixture catalog did not contain one tool")
            tool = tools[0]
            if not isinstance(tool, dict) or tool.get("name") != "echo":
                raise AssertionError("fixture tool name mismatch")
            digest = tool.get("catalog_sha256")
            if not isinstance(digest, str):
                raise AssertionError("bridge did not return catalog digest")
            result = await call_codex_plugin_tool(
                str(root), "echo", "echo", {"text": "fixture-ok"}, digest,
            )
            content = result.get("content")
            if not isinstance(content, list) or content[0].get("text") != "fixture-ok":
                raise AssertionError("fixture tool result mismatch")
            records = [json.loads(line) for line in log.read_text().splitlines()]
            methods = [row for row in records if isinstance(row, str)]
            app_methods = [method for method, _ in calls]
            if any(method in {"turn/start", "turn/steer"} for method in app_methods):
                raise AssertionError("model turn method was requested")
            starts = [params for method, params in calls if method == "thread/start"]
            if not starts or any(params.get("ephemeral") is not True for params in starts):
                raise AssertionError("all bridge threads must be ephemeral")
            if any(method in {"turn/start", "turn/steer"} for method in methods):
                raise AssertionError("model turn method reached MCP fixture")
            identities = []
            for identity in records:
                if not isinstance(identity, dict):
                    continue
                identities.append((int(identity["pid"]), float(identity["create_time"])))
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                live = []
                for pid, create_time in identities:
                    try:
                        process = psutil.Process(pid)
                        if abs(process.create_time() - create_time) < 0.01:
                            live.append(pid)
                    except psutil.NoSuchProcess:
                        pass
                if not live:
                    break
                await asyncio.sleep(0.05)
            else:
                raise AssertionError(f"fixture MCP children remained alive: {live}")
            print(json.dumps({
                "ok": True,
                "server_count": len(rows),
                "tool_count": len(tools),
                "result_text": "fixture-ok",
                "inference_requested": False,
                "app_server_methods": app_methods,
                "mcp_methods": methods,
                "fixture_child_remaining": False,
            }, separators=(",", ":")))
        finally:
            if "original_request" in locals():
                bridge._Session.request = original_request
            if old_home is None:
                os.environ.pop("CODEX_HOME", None)
            else:
                os.environ["CODEX_HOME"] = old_home


if __name__ == "__main__":
    asyncio.run(main())
