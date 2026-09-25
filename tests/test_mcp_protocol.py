import asyncio
import io
import json
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from anywhere_computer.mcp_server import MCPSession, serve_stdio
from anywhere_computer.models import Reply


async def test_initialization_notifications_and_protocol_errors():
    async def catalog():
        return []

    async def execute(request):
        return Reply(operation_id=request.operation_id, state="completed")

    session = MCPSession(catalog, execute)
    assert (await session.handle([]))["error"]["code"] == -32600
    assert (await session.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}))["error"][
        "code"
    ] == -32600
    initialize = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "initialize",
        "params": {
            "protocolVersion": "2099-01-01",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1"},
        },
    }
    assert (await session.handle(initialize))["result"]["protocolVersion"] == "2025-11-25"
    assert await session.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert (await session.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/list"}))[
        "result"
    ] == {"tools": []}
    assert (await session.handle({"jsonrpc": "2.0", "id": 4, "method": "made/up"}))["error"][
        "code"
    ] == -32601


async def test_stdio_parse_failure_does_not_corrupt_following_ping():
    async def catalog():
        return []

    async def execute(request):
        return Reply(operation_id=request.operation_id, state="completed")

    source = io.BytesIO(b'not-json\n{"jsonrpc":"2.0","id":1,"method":"ping"}\n')
    destination = io.BytesIO()
    await serve_stdio(MCPSession(catalog, execute), source, destination)
    messages = [json.loads(line) for line in destination.getvalue().splitlines()]
    assert len(messages) == 2
    assert messages[0]["error"]["code"] == -32700
    assert messages[1]["result"] == {}


async def test_official_sdk_can_use_independent_stdio_server(tmp_path):
    # Actual subprocess and official client: no host credential store or live daemon required.
    program = """
import asyncio,sys
from pathlib import Path
from anywhere_computer.engine import Engine
from anywhere_computer.models import Reply
from anywhere_computer.mcp_server import MCPSession,serve_stdio
async def main():
    engine=Engine(Path(sys.argv[1]))
    async def catalog():
        return [{"name": t.name,"description": t.description,
                 "inputSchema": t.schema.model_json_schema(),
                 "outputSchema": Reply.model_json_schema()}
                for t in engine.tools.values()]
    try:
        await serve_stdio(MCPSession(catalog,engine.execute),sys.stdin.buffer,sys.stdout.buffer)
    finally:
        await engine.close()
asyncio.run(main())
"""
    parameters = StdioServerParameters(
        command=sys.executable, args=["-u", "-c", program, str(tmp_path / "agent")],
        env={"PATH": str(tmp_path / "empty-path"),
             "ANYWHERE_CODEX_EXECUTABLE": str(tmp_path / "missing-codex")},
    )
    async with asyncio.timeout(15):
        async with stdio_client(parameters) as (reader, writer):
            async with ClientSession(reader, writer) as client:
                initialized = await client.initialize()
                assert initialized.serverInfo.name == "anywhere-computer"
                tools = await client.list_tools()
                assert len(tools.tools) == 78
                assert "gui_native_press" in {tool.name for tool in tools.tools}
                # An unavailable optional integration must not disable core tools.
                unavailable = await client.call_tool("codex_skills_list", {"cwd": str(tmp_path)})
                assert unavailable.isError
                # Common skills work in the same real subprocess with no Codex executable.
                skill_root = tmp_path / "portable-skills"
                skill_dir = skill_root / "example"
                skill_dir.mkdir(parents=True)
                (skill_dir / "SKILL.md").write_text("Read helper.py", encoding="utf-8")
                (skill_dir / "helper.py").write_bytes("print('日本語 42')\n".encode())
                listed = await client.call_tool("skills_list", {"roots": [str(skill_root)]})
                row = listed.structuredContent["data"]["skills"][0]
                helper = await client.call_tool("skills_read", {
                    "roots": [str(skill_root)], "skill_id": row["skill_id"],
                    "expected_skill_sha256": row["skill_sha256"], "relative_path": "helper.py",
                })
                assert not helper.isError
                assert helper.structuredContent["data"]["text"] == "print('日本語 42')\n"
                path = str(tmp_path / "MCP 日本語.txt")
                written = await client.call_tool("files_write", {"path": path, "text": "stdio"})
                assert not written.isError
                read = await client.call_tool("files_read", {"path": path})
                assert read.structuredContent["data"]["text"] == "stdio"
                recovered = await client.call_tool("operations_get", {
                    "operation_id": read.structuredContent["operation_id"],
                })
                assert not recovered.isError
                assert recovered.structuredContent["data"]["data"]["text"] == "stdio"

                await client.send_ping()
                bad = await client.call_tool("files_read", {"path": path, "limit": -1})
                assert bad.isError
