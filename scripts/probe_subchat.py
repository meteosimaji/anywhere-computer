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
from collections.abc import Awaitable, Callable
from pathlib import Path


def matching_reply(
    snapshot: object, thread_id: str, previous_user_id: str, prompt: str,
) -> dict[str, str] | None:
    """Match a new turn after a known baseline, never an idle cached result."""
    if not isinstance(snapshot, dict):
        return None
    thread = snapshot.get("thread")
    if not isinstance(thread, dict) or thread.get("id") != thread_id:
        return None
    if thread.get("kind") != "chatgpt" or thread.get("status") != {"type": "idle"}:
        return None
    turns = snapshot.get("turns")
    if not isinstance(turns, list):
        return None
    # read_thread returns newest first. An absent baseline may mean pagination
    # or stale data; neither is evidence that a matching turn is new.
    baseline = next((i for i, turn in enumerate(turns)
                     if isinstance(turn, dict) and turn.get("id") == previous_user_id), None)
    if baseline is None:
        return None
    matches: list[dict[str, str]] = []
    for turn in turns[:baseline]:
        if not isinstance(turn, dict) or turn.get("status") != "completed" or turn.get("error"):
            continue
        items = turn.get("items")
        if not isinstance(items, list) or len(items) != 2:
            continue
        user, answer = items
        if not isinstance(user, dict) or not isinstance(answer, dict):
            continue
        if user.get("type") != "userMessage" or answer.get("type") != "agentMessage":
            continue
        content = user.get("content")
        if not isinstance(content, list) or len(content) != 1:
            continue
        part = content[0]
        if (not isinstance(part, dict) or part.get("type") != "text"
                or part.get("text") != prompt or part.get("truncated") or answer.get("truncated")):
            continue
        user_id, answer_id, text = user.get("id"), answer.get("id"), answer.get("text")
        if (not isinstance(user_id, str) or not user_id
                or not isinstance(answer_id, str) or not answer_id
                or not isinstance(text, str) or not text):
            continue
        if user_id != turn.get("id") or user_id == previous_user_id:
            continue
        matches.append({"user_message_id": user_id, "answer_message_id": answer_id, "text": text})
    return matches[0] if len(matches) == 1 else None


async def wait_for_reply(
    read: Callable[[], Awaitable[object]], thread_id: str, previous_user_id: str,
    prompt: str, timeout: float, interval: float = 2,
) -> dict[str, object]:
    if timeout <= 0 or interval <= 0:
        raise ValueError("timeout and interval must be positive")
    attempts = 0
    try:
        async with asyncio.timeout(timeout):
            while True:
                attempts += 1
                reply = matching_reply(await read(), thread_id, previous_user_id, prompt)
                if reply is not None:
                    return {"state": "reply_observed", "read_attempts": attempts, **reply}
                await asyncio.sleep(interval)
    except TimeoutError:
        return {"state": "reply_unconfirmed", "read_attempts": attempts, "resend": False}


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


async def probe(
    server: Path, thread_id: str | None, previous_user_id: str | None = None,
    expected_prompt: str | None = None, wait_seconds: float = 60,
) -> dict[str, object]:
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
                if previous_user_id is not None and expected_prompt is not None:
                    async def read() -> object:
                        response = await session.call_tool(
                            "read_thread",
                            {"threadId": thread_id, "turnLimit": 10,
                             "maxOutputCharsPerItem": 20000},
                            meta={"codexThreadId": os.environ["CODEX_THREAD_ID"]},
                        )
                        if response.isError:
                            raise RuntimeError("Chat read tool returned an error")
                        if len(response.content) != 1 or response.content[0].type != "text":
                            raise RuntimeError("Unexpected Chat read result shape")
                        return json.loads(response.content[0].text)

                    receipt = await wait_for_reply(
                        read, thread_id, previous_user_id, expected_prompt, wait_seconds,
                    )
                    answer = receipt.pop("text", None)
                    if isinstance(answer, str):
                        receipt["answer_characters"] = len(answer)
                    result["reply"] = receipt
            return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", required=True, type=Path,
                        help="Installed codex-app-tools server.mjs absolute path")
    parser.add_argument("--read-thread", help="Explicitly selected test conversation ID")
    parser.add_argument("--after-user-id", help="Known user-message ID before the tested send")
    parser.add_argument("--expected-prompt-file", type=Path,
                        help="UTF-8 file with the exact already-submitted test prompt")
    parser.add_argument("--wait-seconds", type=float, default=60)
    args = parser.parse_args()
    if not args.server.is_absolute() or not args.server.is_file():
        parser.error("--server must be an existing absolute file path")
    if bool(args.after_user_id) != bool(args.expected_prompt_file):
        parser.error("--after-user-id and --expected-prompt-file must be provided together")
    if args.after_user_id and not args.read_thread:
        parser.error("reply verification requires --read-thread")
    if not 1 <= args.wait_seconds <= 300:
        parser.error("--wait-seconds must be between 1 and 300")
    prompt = (args.expected_prompt_file.read_text(encoding="utf-8")
              if args.expected_prompt_file else None)
    result = asyncio.run(asyncio.wait_for(
        probe(args.server, args.read_thread, args.after_user_id, prompt, args.wait_seconds),
        timeout=30 + args.wait_seconds,
    ))
    print(json.dumps(result, ensure_ascii=False))
    if result["state"] != "catalog_received" or result.get("read_tool_error"):
        raise SystemExit(1)
    reply = result.get("reply")
    if reply is not None and (not isinstance(reply, dict)
                              or reply.get("state") != "reply_observed"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
