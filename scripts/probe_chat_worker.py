#!/usr/bin/env python3
"""Read-only probe of the owning Codex app's ChatGPT tool transport.

Run inside a Codex-provided environment. This does not discover other app
sockets, invent caller identities, start inference, or send chat messages.
"""

import argparse
import asyncio
import json
import os
import stat
from pathlib import Path


def connection_state(environment: dict[str, str]) -> str:
    endpoint = environment.get("CODEX_APP_TOOLS_PIPE_PATH", "").strip()
    if not endpoint:
        return "endpoint_not_provided"
    if not environment.get("CODEX_THREAD_ID", "").strip():
        return "caller_not_provided"
    if os.name != "nt":
        try:
            info = Path(endpoint).stat()
        except FileNotFoundError:
            return "endpoint_missing"
        if not stat.S_ISSOCK(info.st_mode):
            return "endpoint_not_socket"
    return "ready_to_probe"


async def probe(server: Path, thread_id: str | None) -> dict[str, object]:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    state = connection_state(dict(os.environ))
    if state != "ready_to_probe":
        return {"state": state, "dispatched": False, "inference_requested": False}
    launcher = server.parent / "scripts" / "launch_codex_app_tools_mcp"
    child_environment = {
        key: value for key, value in os.environ.items()
        if key in {
            "PATH", "HOME", "USERPROFILE", "LOCALAPPDATA", "XDG_CACHE_HOME",
            "CODEX_APP_TOOLS_PIPE_PATH", "CODEX_MCP_NODE_PATH",
            "CODEX_BROWSER_USE_NODE_PATH", "CODEX_ELECTRON_RESOURCES_PATH",
            "CODEX_CLI_PATH",
        }
    }
    parameters = StdioServerParameters(
        command=str(launcher), args=[str(server)], env=child_environment,
    )
    async with stdio_client(parameters) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            catalog = await session.list_tools()
            names = {tool.name for tool in catalog.tools}
            result: dict[str, object] = {
                "state": "catalog_received", "inference_requested": False,
                "conversation_tools": sorted(names & {
                    "list_threads", "read_thread", "send_message_to_thread", "create_thread",
                }),
                "send_tested": False,
                "ordinary_chat_creation_tested": False,
                "creation_schema": next((tool.inputSchema for tool in catalog.tools
                                         if tool.name == "create_thread"), None),
            }
            if thread_id is not None:
                if "read_thread" not in names:
                    raise RuntimeError("read_thread is absent from the live catalog")
                response = await session.call_tool(
                    "read_thread",
                    {"threadId": thread_id, "turnLimit": 1, "maxOutputCharsPerItem": 100},
                    meta={"codexThreadId": os.environ["CODEX_THREAD_ID"]},
                )
                # Do not print private conversation text as probe diagnostics.
                result["read_tool_error"] = bool(response.isError)
                result["read_content_blocks"] = len(response.content)
            return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", required=True, type=Path,
                        help="Installed codex-app-tools server.mjs absolute path")
    parser.add_argument("--read-thread", help="Explicitly selected test conversation ID")
    args = parser.parse_args()
    if not args.server.is_absolute() or not args.server.is_file():
        parser.error("--server must be an existing absolute file path")
    result = asyncio.run(asyncio.wait_for(probe(args.server, args.read_thread), timeout=30))
    print(json.dumps(result, ensure_ascii=False))
    if result["state"] != "catalog_received" or result.get("read_tool_error"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
