"""Bounded, read-only context access through the native Codex app server."""

import asyncio
import json
import os
import re
import shutil
from pathlib import Path
from typing import Literal, cast

from pydantic import JsonValue

WIRE_LIMIT = 8 * 1024 * 1024
TEXT_LIMIT = 64 * 1024
MAX_MESSAGES = 1000
LIFETIME = 30.0
AllowedMethod = Literal["thread/list", "thread/turns/list", "skills/list"]


def _executable(explicit: Path | None) -> Path:
    if explicit is None and os.environ.get("ANYWHERE_CODEX_EXECUTABLE"):
        explicit = Path(os.environ["ANYWHERE_CODEX_EXECUTABLE"])
    if explicit is not None:
        if not explicit.is_absolute() or explicit.parent == Path.cwd():
            raise ValueError("Codex executable must be an absolute path outside the cwd")
        selected = explicit
    else:
        found = shutil.which("codex")
        if found is None:
            raise FileNotFoundError("Codex executable was not found")
        selected = Path(found)
        if not selected.is_absolute():
            raise ValueError("Codex executable must resolve from an absolute PATH entry")
    resolved = selected.resolve()
    if not resolved.is_file() or not resolved.is_absolute():
        raise ValueError("Codex executable is not a regular file")
    if resolved.parent == Path.cwd().resolve():
        raise ValueError("Codex executable may not be loaded from the cwd")
    return resolved


async def codex_request(
    method: AllowedMethod,
    params: dict[str, JsonValue],
    *,
    executable: Path | None = None,
) -> dict[str, JsonValue]:
    """Call one read-only app-server method and return its bounded result object."""
    if method not in {"thread/list", "thread/turns/list", "skills/list"}:
        raise ValueError("Codex method is not allowed")
    program = _executable(executable)
    process = await asyncio.create_subprocess_exec(
        str(program), "app-server", "--listen", "stdio://",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, cwd=str(Path.home()), limit=WIRE_LIMIT,
    )
    request_id = 0

    async def send(message: dict[str, JsonValue]) -> None:
        assert process.stdin is not None
        encoded = (json.dumps(message, ensure_ascii=False, allow_nan=False) + "\n").encode()
        if len(encoded) > WIRE_LIMIT:
            raise ValueError("Codex request exceeds the size limit")
        process.stdin.write(encoded)
        await process.stdin.drain()

    async def receive(expected: int) -> dict[str, JsonValue]:
        assert process.stdout is not None
        while True:
            line = await process.stdout.readline()
            if not line:
                raise ConnectionError("Codex app server closed the connection")
            if len(line) > WIRE_LIMIT:
                raise ValueError("Codex response exceeds the size limit")
            payload = json.loads(line)
            if not isinstance(payload, dict):
                continue
            if type(payload.get("id")) is not int or payload["id"] != expected:
                continue
            if "error" in payload:
                raise RuntimeError("Codex app server rejected the read request")
            result = payload.get("result")
            if not isinstance(result, dict):
                raise ValueError("Codex app server returned an invalid result")
            return cast(dict[str, JsonValue], result)

    async def call(name: str, arguments: dict[str, JsonValue]) -> dict[str, JsonValue]:
        nonlocal request_id
        request_id += 1
        await send({"id": request_id, "method": name, "params": arguments})
        return await receive(request_id)

    async def run() -> dict[str, JsonValue]:
        await call("initialize", {
            "clientInfo": {"name": "anywhere-computer-context", "version": "1"},
            "capabilities": {"experimentalApi": True},
        })
        await send({"method": "initialized", "params": {}})
        return await call(method, params)

    try:
        return await asyncio.wait_for(run(), timeout=LIFETIME)
    finally:
        if process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), timeout=1)
            except (TimeoutError, ProcessLookupError):
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()


def _bounded_text(value: object) -> str:
    if isinstance(value, str):
        return value
    return ""


def _cursor(value: str | None) -> str | None:
    if value is not None and (not value or len(value) > 2048):
        raise ValueError("Cursor is too long")
    return value


def _truncate_utf8(text: str, limit: int) -> tuple[str, bool]:
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text, False
    return encoded[:limit].decode("utf-8", errors="ignore"), True


def _message_text(item: dict[str, JsonValue]) -> str:
    direct = _bounded_text(item.get("text"))
    if direct:
        return direct
    content = item.get("content")
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for part in content:
        if isinstance(part, dict) and part.get("type") in {"text", "input_text", "output_text"}:
            text = _bounded_text(part.get("text"))
            if text:
                parts.append(text)
    return "".join(parts)


def _thread_metadata(raw: object) -> dict[str, JsonValue] | None:
    if not isinstance(raw, dict):
        return None
    identity = raw.get("id", raw.get("threadId"))
    if (
        not isinstance(identity, str)
        or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", identity) is None
    ):
        return None
    title = raw.get("title", raw.get("name"))
    result: dict[str, JsonValue] = {
        "id": identity,
        "title": title[:1000] if isinstance(title, str) and title else "Untitled",
    }
    for key in ("archived", "isPinned", "projectId", "createdAt", "updatedAt", "status"):
        value = raw.get(key)
        if isinstance(value, (str, int, float, bool)):
            result[key] = value[:1000] if isinstance(value, str) else cast(JsonValue, value)
    return result


async def list_codex_threads(
    limit: int = 10, cursor: str | None = None, *, executable: Path | None = None,
) -> dict[str, JsonValue]:
    if not 1 <= limit <= 30:
        raise ValueError("Thread list limit must be between 1 and 30")
    params: dict[str, JsonValue] = {"limit": limit, "useStateDbOnly": True}
    bounded_cursor = _cursor(cursor)
    if bounded_cursor is not None:
        params["cursor"] = bounded_cursor
    result = await codex_request("thread/list", params, executable=executable)
    raw_threads = result.get("data", result.get("threads", []))
    if not isinstance(raw_threads, list):
        raise ValueError("Codex thread list has an invalid shape")
    if len(raw_threads) > limit:
        raise ValueError("Codex returned more threads than requested")
    threads = [item for raw in raw_threads if (item := _thread_metadata(raw)) is not None]
    output: dict[str, JsonValue] = {"threads": cast(JsonValue, threads)}
    next_cursor = result.get("nextCursor", result.get("next_cursor"))
    if isinstance(next_cursor, str) and 0 < len(next_cursor) <= 2048:
        output["next_cursor"] = next_cursor
    return output


async def read_codex_thread(
    thread_id: str, limit: int = 3, cursor: str | None = None, *, executable: Path | None = None,
) -> dict[str, JsonValue]:
    if re.fullmatch(r"[A-Za-z0-9_-]{1,128}", thread_id) is None or not 1 <= limit <= 10:
        raise ValueError("Invalid thread read bounds")
    params: dict[str, JsonValue] = {
        "threadId": thread_id, "limit": limit, "itemsView": "full",
    }
    bounded_cursor = _cursor(cursor)
    if bounded_cursor is not None:
        params["cursor"] = bounded_cursor
    result = await codex_request("thread/turns/list", params, executable=executable)
    response_id = result.get("threadId", result.get("thread_id"))
    if response_id is not None and response_id != thread_id:
        raise ValueError("Codex returned a different thread ID")
    raw_turns = result.get("data", result.get("turns", []))
    if not isinstance(raw_turns, list):
        raise ValueError("Codex thread turns have an invalid shape")
    if len(raw_turns) > limit:
        raise ValueError("Codex returned more turns than requested")
    messages: list[JsonValue] = []
    total = 0
    truncated = False
    for turn in raw_turns:
        if not isinstance(turn, dict):
            continue
        items = turn.get("items", [])
        if not isinstance(items, list):
            continue
        for item in items:
            if (
                not isinstance(item, dict)
                or item.get("type") not in {"userMessage", "agentMessage"}
                or item.get("phase") == "analysis"
            ):
                continue
            text = _message_text(item)
            if not text:
                continue
            remaining = TEXT_LIMIT - total
            if remaining <= 0:
                truncated = True
                break
            text, text_truncated = _truncate_utf8(text, remaining)
            if text_truncated:
                truncated = True
            entry: dict[str, JsonValue] = {
                "role": "user" if item["type"] == "userMessage" else "assistant",
                "text": text,
            }
            message_id = item.get("id")
            if isinstance(message_id, str):
                entry["id"] = message_id[:128]
            messages.append(entry)
            total += len(text.encode("utf-8"))
            if len(messages) >= MAX_MESSAGES:
                truncated = True
            if truncated:
                break
        if truncated:
            break
    output: dict[str, JsonValue] = {
        "thread_id": thread_id, "messages": messages, "truncated": truncated,
    }
    next_cursor = result.get("nextCursor", result.get("next_cursor"))
    if isinstance(next_cursor, str) and 0 < len(next_cursor) <= 2048:
        output["next_cursor"] = next_cursor
    return output
