"""Bounded, ephemeral access to Codex MCP plugin tools."""

import asyncio
import hashlib
import json
import re
from pathlib import Path
from typing import cast

from pydantic import JsonValue

from .codex_context import WIRE_LIMIT, _executable

STARTUP_TIMEOUT = 30.0
CALL_TIMEOUT = 120.0
TEXT_LIMIT = 64 * 1024
MAX_PAGES = 10
MAX_CURSOR = 2048
MAX_CATALOG = 30
_SAFE_NAME = re.compile(r"^[A-Za-z0-9_.:/-]{1,200}$")
_DENIED_TOOL_PREFIXES = ("codex_plugin_", "devices_", "connection_setup_", "mcp__codex_app__")


class PluginPreflightError(ValueError):
    """A classified failure before any plugin tool dispatch."""

    def __init__(self, code: str, action: str) -> None:
        self.code = code
        self.action = action
        super().__init__(f"{code}: {action}")


def _availability(row: dict[str, JsonValue], has_tools: bool) -> str:
    runtime = row.get("runtimeStatus")
    if runtime == "authenticationRequired" or row.get("authStatus") == "notLoggedIn":
        return "authentication_required"
    if runtime in {"failed", "cancelled", "disabled"}:
        return "unavailable"
    if runtime in {"notStarted", "starting"}:
        return "runtime_not_ready"
    if runtime == "connected" and has_tools:
        return "ready_to_call"
    return "unverified"


class PluginCallOutcomeUnknown(RuntimeError):
    """The plugin call may have dispatched, but its outcome was not confirmed."""


def _cwd(value: str) -> str:
    path = Path(value)
    if not path.is_absolute() or not path.is_dir():
        raise ValueError("Plugin cwd must be an existing absolute directory")
    return str(path.resolve())


def _cursor(value: str | None) -> str | None:
    if value is not None and (not value or len(value) > MAX_CURSOR):
        raise ValueError("Plugin catalog cursor is invalid")
    return value


def _deny(server: str, tool: str = "") -> None:
    lowered = server.casefold()
    if "anywhere-computer" in lowered or "anywhere_computer" in lowered:
        raise ValueError("Anywhere Computer's own server cannot be routed through this bridge")
    if tool.casefold().startswith(_DENIED_TOOL_PREFIXES):
        raise ValueError("Recursive or local tool routing is forbidden")


def _json_object(value: object, message: str) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        raise ValueError(message)
    return cast(dict[str, JsonValue], value)


def _bounded_text(value: object, remaining: int) -> tuple[str, bool]:
    if not isinstance(value, str):
        return "", False
    raw = value.encode("utf-8")
    if len(raw) <= remaining:
        return value, False
    return raw[:remaining].decode("utf-8", errors="ignore"), True


def _descriptor_digest(server: str, cwd: str, tool: dict[str, JsonValue]) -> str:
    payload = json.dumps({
        "server": server, "cwd": cwd, "name": tool["name"],
        "description": tool.get("description", ""),
        "inputSchema": tool.get("inputSchema", {}),
        "annotations": tool.get("annotations", {}),
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def _strip_meta(value: JsonValue) -> JsonValue:
    if isinstance(value, dict):
        return {key: _strip_meta(item) for key, item in value.items() if key != "_meta"}
    if isinstance(value, list):
        return [_strip_meta(item) for item in value]
    return value


class _Session:
    _ALLOWED = {
        "initialize", "thread/start", "mcpServerStatus/list", "mcpServer/tool/call",
        "thread/unsubscribe",
    }

    def __init__(self, executable: Path) -> None:
        self.executable = executable
        self.process: asyncio.subprocess.Process | None = None
        self.next_id = 0

    async def __aenter__(self) -> "_Session":
        self.process = await asyncio.create_subprocess_exec(
            str(self.executable), "app-server", "--listen", "stdio://",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, cwd=str(Path.home()), limit=WIRE_LIMIT,
        )
        try:
            await asyncio.wait_for(self._initialize(), timeout=STARTUP_TIMEOUT)
            return self
        except BaseException:
            await self._close()
            raise

    async def __aexit__(self, *_: object) -> None:
        await self._close()

    async def _close(self) -> None:
        if self.process is None or self.process.returncode is not None:
            return
        try:
            self.process.terminate()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(self.process.wait(), timeout=1)
        except (TimeoutError, ProcessLookupError):
            try:
                self.process.kill()
            except ProcessLookupError:
                pass
            await self.process.wait()

    async def _send(self, packet: dict[str, JsonValue]) -> None:
        if self.process is None or self.process.stdin is None:
            raise ConnectionError("Plugin app server is not running")
        line = (json.dumps(packet, ensure_ascii=False, allow_nan=False) + "\n").encode()
        if len(line) > WIRE_LIMIT:
            raise ValueError("Plugin request exceeds the size limit")
        self.process.stdin.write(line)
        await self.process.stdin.drain()

    async def _receive(self, expected: int) -> dict[str, JsonValue]:
        if self.process is None or self.process.stdout is None:
            raise ConnectionError("Plugin app server is not running")
        while True:
            line = await self.process.stdout.readline()
            if not line:
                raise ConnectionError("Plugin app server closed the connection")
            if len(line) > WIRE_LIMIT:
                raise ValueError("Plugin response exceeds the size limit")
            message = json.loads(line)
            if not isinstance(message, dict):
                continue
            if isinstance(message.get("method"), str) and "id" in message:
                await self._send({
                    "id": message["id"],
                    "error": {
                        "code": -32601, "message": "Interactive plugin requests are disabled",
                    },
                })
                continue
            if type(message.get("id")) is not int or message["id"] != expected:
                continue
            if "error" in message:
                raise RuntimeError("Codex app server rejected the plugin request")
            return _json_object(message.get("result"), "Invalid plugin response")

    async def request(self, method: str, params: dict[str, JsonValue]) -> dict[str, JsonValue]:
        self.next_id += 1
        if method not in self._ALLOWED:
            raise ValueError("Codex app-server method is not allowed")
        if method == "thread/start" and params.get("ephemeral") is not True:
            raise ValueError("Codex plugin contexts must be ephemeral")
        await self._send({
            "id": self.next_id, "method": method, "params": params,
        })
        return await self._receive(self.next_id)

    async def notify(self, method: str, params: dict[str, JsonValue]) -> None:
        if method != "initialized":
            raise ValueError("Codex notification is not allowed")
        await self._send({"method": method, "params": params})

    async def _initialize(self) -> None:
        await self.request("initialize", {
            "clientInfo": {"name": "anywhere-computer-plugin", "version": "1"},
            "capabilities": {"experimentalApi": True},
        })
        await self.notify("initialized", {})


async def _start_and_catalog(
    session: _Session, cwd: str, *, limit: int, cursor: str | None = None,
    max_pages: int = MAX_PAGES,
) -> tuple[list[dict[str, JsonValue]], str | None, str]:
    started = await session.request("thread/start", {"ephemeral": True, "cwd": cwd})
    thread_id = started.get("threadId", started.get("thread_id"))
    if isinstance(started.get("thread"), dict):
        thread = cast(dict[str, JsonValue], started["thread"])
        thread_id = thread.get("id", thread.get("threadId"))
    if not isinstance(thread_id, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", thread_id) is None:
        raise ValueError("Codex returned an invalid ephemeral thread ID")
    servers: list[dict[str, JsonValue]] = []
    seen_servers: set[str] = set()
    page_cursor = cursor
    seen_cursors: set[str] = set()
    for _ in range(max_pages):
        params: dict[str, JsonValue] = {
            "threadId": thread_id, "detail": "toolsAndAuthOnly", "limit": limit,
        }
        if page_cursor is not None:
            params["cursor"] = page_cursor
        result = await session.request("mcpServerStatus/list", params)
        rows = result.get("data", result.get("servers", []))
        if isinstance(rows, dict):
            rows = [dict(value, server=key) for key, value in rows.items()
                    if isinstance(value, dict)]
        if not isinstance(rows, list) or len(rows) > limit:
            raise ValueError("Invalid or oversized plugin server catalog")
        for row in rows:
            if not isinstance(row, dict):
                continue
            server = row.get("name", row.get("server", row.get("id")))
            if not isinstance(server, str) or not _SAFE_NAME.fullmatch(server):
                continue
            try:
                _deny(server)
            except ValueError:
                continue
            if server in seen_servers:
                raise ValueError("Plugin catalog contains duplicate servers")
            seen_servers.add(server)
            raw_tools = row.get("tools", [])
            if isinstance(raw_tools, dict):
                raw_tools = [dict(value, name=key) for key, value in raw_tools.items()
                             if isinstance(value, dict)]
            if not isinstance(raw_tools, list) or len(raw_tools) > 1000:
                continue
            tools: list[JsonValue] = []
            seen_tools: set[str] = set()
            for raw_tool in raw_tools:
                if not isinstance(raw_tool, dict):
                    continue
                name = raw_tool.get("name")
                if not isinstance(name, str) or not _SAFE_NAME.fullmatch(name):
                    continue
                try:
                    _deny(server, name)
                except ValueError:
                    continue
                if name in seen_tools:
                    raise ValueError("Plugin catalog contains duplicate tools")
                seen_tools.add(name)
                if not isinstance(raw_tool.get("inputSchema"), dict):
                    continue
                description = raw_tool.get("description", "")
                if not isinstance(description, str):
                    continue
                annotations = raw_tool.get("annotations", {})
                if not isinstance(annotations, dict):
                    annotations = {}
                clean_annotations: dict[str, JsonValue] = {}
                for key, value in annotations.items():
                    if not isinstance(key, str) or len(key) > 100:
                        continue
                    if isinstance(value, bool):
                        clean_annotations[key] = value
                    elif isinstance(value, str) and len(value) <= 1000:
                        clean_annotations[key] = value
                clean: dict[str, JsonValue] = {
                    "name": name, "description": description,
                    "inputSchema": raw_tool["inputSchema"], "annotations": clean_annotations,
                    "server": server,
                }
                clean["catalog_sha256"] = _descriptor_digest(server, cwd, clean)
                if len(description) > 1000:
                    clean["description"] = description[:1000]
                    clean["description_truncated"] = True
                tools.append(clean)
            servers.append({
                "server": server, "tools": tools,
                "availability": _availability(row, bool(tools)),
                "runtime_status": row.get("runtimeStatus")
                if row.get("runtimeStatus") in {
                    "notStarted", "starting", "connected", "authenticationRequired",
                    "failed", "cancelled", "disabled",
                } else None,
                "auth_status": row.get("authStatus")
                if row.get("authStatus") in {
                    "unknown", "unsupported", "notLoggedIn", "bearerToken", "oAuth",
                } else "unknown",
                "execution_verified": False,
            })
            if len(json.dumps(servers, ensure_ascii=False).encode()) > 2 * 1024 * 1024:
                raise ValueError("Plugin catalog exceeds the size limit")
        next_value = result.get("nextCursor", result.get("next_cursor"))
        if next_value is None:
            page_cursor = None
            break
        if (
            not isinstance(next_value, str) or not next_value or len(next_value) > MAX_CURSOR
            or next_value in seen_cursors
        ):
            raise ValueError("Plugin catalog pagination is invalid")
        seen_cursors.add(next_value)
        page_cursor = next_value
    return servers, page_cursor, thread_id


async def list_codex_plugin_tools(
    cwd: str, limit: int = MAX_CATALOG, cursor: str | None = None,
    *, server: str | None = None, tool: str | None = None,
    query: str | None = None, summary: bool = False,
) -> dict[str, JsonValue]:
    if not 1 <= limit <= MAX_CATALOG:
        raise ValueError("Plugin catalog limit must be between 1 and 30")
    if tool is not None and server is None:
        raise PluginPreflightError("invalid_selection", "Specify server with tool")
    for name in (server, tool):
        if name is not None and not _SAFE_NAME.fullmatch(name):
            raise PluginPreflightError("invalid_selection", "Use an exact catalog name")
    if server is not None:
        _deny(server, tool or "")
    clean_cwd = _cwd(cwd)
    bounded_cursor = _cursor(cursor)
    async with _Session(_executable(None)) as session:
        servers, next_cursor, thread_id = await asyncio.wait_for(
            _start_and_catalog(session, clean_cwd, limit=limit, cursor=bounded_cursor,
                               max_pages=MAX_PAGES if server else 1),
            timeout=STARTUP_TIMEOUT,
        )
        try:
            await asyncio.wait_for(
                session.request("thread/unsubscribe", {"threadId": thread_id}), 1,
            )
        except (TimeoutError, ConnectionError, OSError, ValueError, RuntimeError):
            pass
    selected_servers: list[dict[str, JsonValue]] = []
    for row in servers:
        if server is not None and row["server"] != server:
            continue
        descriptors = cast(list[dict[str, JsonValue]], row["tools"])
        selected = [item for item in descriptors if
                    (tool is None or item["name"] == tool) and
                    (query is None or query.casefold() in
                     f"{row['server']} {item['name']} {item['description']}".casefold())]
        if (query is not None and not selected
                and query.casefold() not in str(row["server"]).casefold()):
            continue
        clean_row = dict(row)
        clean_row["tool_count"] = len(selected)
        clean_row["inspect_arguments"] = {"cwd": clean_cwd, "server": row["server"]}
        if summary:
            clean_row["tools"] = []
            clean_row["tool_names_preview"] = [item["name"] for item in selected[:10]]
        else:
            clean_row["tools"] = cast(JsonValue, selected)
        selected_servers.append(clean_row)
    result: dict[str, JsonValue] = {
        "cwd": clean_cwd, "servers": cast(JsonValue, selected_servers),
        "inference_requested": False,
        "next_action": "Use codex_plugin_tools with server and optional tool for exact schemas; "
        "copy server, tool name, cwd and catalog_sha256 into codex_plugin_call. "
        "Registration and schemas do not prove successful execution.",
    }
    if next_cursor is not None:
        result["next_cursor"] = next_cursor
    return result


async def call_codex_plugin_tool(
    cwd: str, server: str, tool: str, arguments: dict[str, JsonValue], catalog_sha256: str,
) -> dict[str, JsonValue]:
    if not _SAFE_NAME.fullmatch(server) or not _SAFE_NAME.fullmatch(tool):
        raise ValueError("Plugin server or tool name is invalid")
    if not re.fullmatch(r"[a-f0-9]{64}", catalog_sha256):
        raise ValueError("Plugin catalog digest is invalid")
    _deny(server, tool)
    clean_cwd = _cwd(cwd)
    async with _Session(_executable(None)) as session:
        servers, remaining_cursor, thread_id = await asyncio.wait_for(
            _start_and_catalog(session, clean_cwd, limit=MAX_CATALOG, max_pages=MAX_PAGES),
            timeout=STARTUP_TIMEOUT,
        )
        selected: dict[str, JsonValue] | None = None
        for row in servers:
            if row.get("server") != server:
                continue
            raw_tools = row.get("tools", [])
            if not isinstance(raw_tools, list):
                continue
            for candidate in raw_tools:
                if isinstance(candidate, dict) and candidate.get("name") == tool:
                    if selected is not None:
                        raise ValueError("Plugin catalog contains duplicate tool names")
                    selected = candidate
        selected_server = next((row for row in servers if row.get("server") == server), None)
        if selected_server is not None and selected_server.get("availability") in {
            "authentication_required", "runtime_not_ready", "unavailable",
        }:
            raise PluginPreflightError(
                str(selected_server["availability"]),
                "Inspect this server and repair its local authentication/runtime before retrying",
            )
        if selected is None and remaining_cursor is not None:
            raise PluginPreflightError(
                "catalog_incomplete",
                "Server scan reached its page limit; tool absence is not established. "
                "Inspect subsequent catalog pages; no plugin call was dispatched",
            )
        if selected is None:
            raise PluginPreflightError(
                "tool_not_found",
                "Inspect the exact server/tool; it is absent from the bounded catalog",
            )
        if selected.get("catalog_sha256") != catalog_sha256:
            raise PluginPreflightError(
                "catalog_stale", "Inspect the tool again and use its new digest",
            )
        try:
            try:
                result = await asyncio.wait_for(session.request("mcpServer/tool/call", {
                    "threadId": thread_id, "server": server, "tool": tool,
                    "arguments": arguments,
                }), timeout=CALL_TIMEOUT)
            except (
                TimeoutError, ConnectionError, OSError, ValueError, RuntimeError,
                json.JSONDecodeError
            ) as exc:
                raise PluginCallOutcomeUnknown("Plugin call outcome was not confirmed") from exc
        finally:
            try:
                await asyncio.wait_for(
                    session.request("thread/unsubscribe", {"threadId": thread_id}),
                    timeout=1,
                )
            except (TimeoutError, ConnectionError, OSError, ValueError, RuntimeError):
                pass
        try:
            return _tool_result(result)
        except (ValueError, TypeError, RuntimeError) as error:
            raise PluginCallOutcomeUnknown("Plugin returned a malformed result") from error


def _tool_result(result: dict[str, JsonValue]) -> dict[str, JsonValue]:
    content = result.get("content", [])
    is_error = result.get("isError", False)
    if not isinstance(content, list) or not isinstance(is_error, bool):
        raise ValueError("Invalid plugin result")
    clean_content: list[JsonValue] = []
    total = 0
    truncated = False
    unsupported = 0
    for item in content:
        if not isinstance(item, dict) or not isinstance(item.get("type"), str):
            raise ValueError("Invalid plugin content")
        if item["type"] != "text":
            unsupported += 1
            truncated = True
            continue
        if not isinstance(item.get("text"), str):
            raise ValueError("Invalid plugin text")
        text, cut = _bounded_text(item["text"], TEXT_LIMIT - total)
        if text:
            clean_content.append({"type": "text", "text": text})
            total += len(text.encode("utf-8"))
        truncated = truncated or cut
        if cut:
            break
    output: dict[str, JsonValue] = {
        "content": clean_content, "is_error": is_error, "truncated": truncated,
    }
    if unsupported:
        output["unsupported_content_items"] = unsupported
    structured = result.get("structuredContent")
    if structured is not None:
        if not isinstance(structured, dict):
            raise ValueError("Invalid plugin structured data")
        raw = json.dumps(structured, ensure_ascii=False, allow_nan=False).encode()
        if len(raw) <= TEXT_LIMIT:
            output["structured_content"] = _strip_meta(structured)
        else:
            output["truncated"] = True
    return output
