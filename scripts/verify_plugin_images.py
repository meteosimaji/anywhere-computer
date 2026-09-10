#!/usr/bin/env python3
"""Exercise real Codex MCP dispatch and native images through the official MCP SDK.

The default run owns an isolated CODEX_HOME and a tiny image-only MCP fixture.
--installed additionally calls the user's read-only docs and Calendar palette tools.
Only protocol method names, outcome states and image hashes are recorded.
"""

import argparse
import asyncio
import base64
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
PNG = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8"
       "/x8AAwMCAO+a5XcAAAAASUVORK5CYII=")


def image_worker() -> None:
    for line in sys.stdin:
        request = json.loads(line)
        if "id" not in request:
            continue
        method = request.get("method")
        if method == "initialize":
            result = {
                "protocolVersion": "2025-11-25", "capabilities": {"tools": {}},
                "serverInfo": {"name": "image-fixture", "version": "1"},
            }
        elif method == "tools/list":
            result = {"tools": [{
                "name": "screenshot", "description": "Return a synthetic one-pixel PNG",
                "inputSchema": {"type": "object", "additionalProperties": False},
                "annotations": {"readOnlyHint": True},
            }]}
        elif method == "tools/call":
            result = {"content": [
                {"type": "text", "text": "image-fixture-ok"},
                {"type": "image", "mimeType": "image/png", "data": PNG},
            ], "isError": False}
        else:
            result = {}
        print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)


async def bridge_worker(state: Path, trace: Path) -> None:
    from anywhere_computer import codex_plugins
    from anywhere_computer.engine import Engine
    from anywhere_computer.mcp_server import MCPSession, serve_stdio

    original_send = codex_plugins._Session._send

    async def recording_send(session, packet):
        await original_send(session, packet)
        method = packet.get("method")
        if isinstance(method, str):
            record = {"method": method}
            if method == "thread/start":
                record["ephemeral"] = packet.get("params", {}).get("ephemeral")
            with trace.open("a", encoding="utf-8") as output:
                output.write(json.dumps(record) + "\n")

    codex_plugins._Session._send = recording_send
    engine = Engine(state)

    async def catalog():
        return engine.catalog(frozenset({
            "codex_plugin_tools", "codex_plugin_call", "operations_get",
        }))

    try:
        await serve_stdio(MCPSession(catalog, engine.execute), sys.stdin.buffer, sys.stdout.buffer)
    finally:
        await engine.close()
        codex_plugins._Session._send = original_send


async def verify_client(
    client, cwd: str, server: str, tool: str, arguments: dict, *, allow_tool_error: bool = False,
):
    inspected = await client.call_tool("codex_plugin_tools", {
        "cwd": cwd, "server": server, "tool": tool,
    })
    assert not inspected.isError, inspected.structuredContent
    rows = inspected.structuredContent["data"]["servers"]
    descriptors = [item for row in rows for item in row["tools"] if item["name"] == tool]
    assert len(descriptors) == 1, "Expected exactly one selected descriptor"
    call_arguments = descriptors[0]["call_arguments"]
    result = await client.call_tool("codex_plugin_call", {
        **call_arguments, "arguments": arguments,
    })
    if result.isError and not allow_tool_error:
        raise AssertionError(json.dumps(result.structuredContent, ensure_ascii=False))
    assert result.structuredContent["state"] == "completed"
    recovered = await client.call_tool("operations_get", {
        "operation_id": result.structuredContent["operation_id"],
    })
    assert not recovered.isError
    assert recovered.structuredContent["data"]["state"] == "completed"
    return result, recovered, call_arguments


def check_trace(path: Path) -> dict:
    records = [json.loads(line) for line in path.read_text().splitlines()]
    methods = [record["method"] for record in records]
    assert "mcpServer/tool/call" in methods
    assert not {"turn/start", "turn/steer", "thread/resume"}.intersection(methods)
    starts = [record for record in records if record["method"] == "thread/start"]
    assert starts and all(record["ephemeral"] is True for record in starts)
    return {"sent_methods": methods, "inference_requested": False}


async def verify(codex: Path, cwd: Path, installed: bool) -> dict:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from mcp.types import ImageContent

    report = {"transport": "official-mcp-sdk/stdio", "cases": []}
    with tempfile.TemporaryDirectory(prefix="plugin-images-") as temporary:
        root = Path(temporary)
        home = root / "codex-home"
        home.mkdir()
        worker = Path(__file__).resolve()
        (home / "config.toml").write_text(
            "[mcp_servers.image_fixture]\n"
            f"command = {json.dumps(sys.executable)}\n"
            f"args = [\"-I\", {json.dumps(str(worker))}, \"--worker\", \"image\"]\n"
            "startup_timeout_sec = 5\n", encoding="utf-8",
        )
        scenarios = ["fixture", "installed"] if installed else ["fixture"]
        for scenario in scenarios:
            trace = root / f"{scenario}-trace.jsonl"
            env = dict(os.environ, ANYWHERE_CODEX_EXECUTABLE=str(codex))
            if scenario == "fixture":
                env["CODEX_HOME"] = str(home)
            parameters = StdioServerParameters(
                command=sys.executable,
                args=["-I", str(worker), "--worker", "bridge", "--state", str(root / scenario),
                      "--trace", str(trace)], env=env,
            )
            async with stdio_client(parameters) as (reader, writer):
                async with ClientSession(reader, writer) as client:
                    await client.initialize()
                    if scenario == "fixture":
                        result, recovered, arguments = await verify_client(
                            client, str(root), "image_fixture", "screenshot", {},
                        )
                        images = [item for item in result.content if isinstance(item, ImageContent)]
                        assert len(images) == 1 and images[0].data == PNG
                        assert PNG not in result.content[0].text
                        assert PNG not in json.dumps(result.structuredContent)
                        saved = recovered.structuredContent["data"]["data"]["content"]
                        assert any(item.get("data") == PNG for item in saved)
                        before = check_trace(trace)["sent_methods"].count("mcpServer/tool/call")
                        stale = await client.call_tool("codex_plugin_call", {
                            **arguments, "arguments": {}, "catalog_sha256": "0" * 64,
                        })
                        assert stale.isError
                        failure = stale.structuredContent["data"]
                        assert failure["error_code"] == "catalog_stale"
                        assert failure["dispatched"] is False
                        assert (
                            failure["details"]["current_catalog_sha256"]
                            == arguments["catalog_sha256"]
                        )
                        after = check_trace(trace)["sent_methods"].count("mcpServer/tool/call")
                        assert before == after == 1
                        report["cases"].append({
                            "case": "native_image_and_stale_guard", "passed": True,
                            "image_sha256": hashlib.sha256(base64.b64decode(PNG)).hexdigest(),
                            "image_retained_in_ledger": True,
                        })
                    else:
                        for server, tool, arguments in [
                            ("openaiDeveloperDocs", "list_openai_docs", {"limit": 1}),
                            ("codex_apps", "google_calendar.get_colors", {}),
                        ]:
                            result, _, _ = await verify_client(
                                client, str(cwd), server, tool, arguments, allow_tool_error=True,
                            )
                            data = result.structuredContent["data"]
                            structured = data.get("structured_content", {})
                            report["cases"].append({
                                "server": server, "tool": tool,
                                "bridge_round_trip_passed": True,
                                "tool_succeeded": not result.isError,
                                "error_code": structured.get("error_code"),
                                "error_reason": structured.get("error_data", {}).get("reason"),
                                "operation_id": result.structuredContent["operation_id"],
                            })
            report[scenario] = check_trace(trace)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", choices=["image", "bridge"], help=argparse.SUPPRESS)
    parser.add_argument("--state", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--trace", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--codex", type=Path)
    parser.add_argument("--cwd", type=Path, default=ROOT)
    parser.add_argument("--installed", action="store_true")
    arguments = parser.parse_args()
    if arguments.worker == "image":
        image_worker()
    elif arguments.worker == "bridge":
        asyncio.run(bridge_worker(arguments.state, arguments.trace))
    else:
        if arguments.codex is None or not arguments.codex.is_absolute():
            parser.error("--codex must name an absolute installed Codex executable")
        report = asyncio.run(verify(
            arguments.codex, arguments.cwd.resolve(), arguments.installed,
        ))
        print(json.dumps(report, ensure_ascii=False, indent=2))
        if any(case.get("tool_succeeded") is False for case in report["cases"]):
            raise SystemExit(1)
