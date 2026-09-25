"""Bounded, ephemeral access to Codex MCP plugin tools."""

import asyncio
import hashlib
import json
import os
import re
from pathlib import Path
from typing import cast

from pydantic import JsonValue

from .codex_context import WIRE_LIMIT, _executable
from .execution_environment import with_tool_path
from .mcp_results import normalize_tool_result as _tool_result
from .plugin_diagnostics import (
    STDERR_CHUNK,
    PluginDiagnostics,
    PluginRPCError,
    failure_diagnostic,
)

STARTUP_TIMEOUT = 30.0
CALL_TIMEOUT = 120.0
MAX_PAGES = 10
MAX_CURSOR = 2048
MAX_CATALOG = 30
_SAFE_NAME = re.compile(r"^[A-Za-z0-9_.:/-]{1,200}$")
_DENIED_TOOL_PREFIXES = ("codex_plugin_", "devices_", "connection_setup_", "mcp__codex_app__")
STATEFUL_SUBCHAT_TOOLS = frozenset({
    "subchat_send", "subchat_message", "subchat_recover", "subchat_wait",
    "subchat_queue_watch",
})


class PluginPreflightError(ValueError):
    """A classified failure before any plugin tool dispatch."""

    def __init__(
        self, code: str, action: str, *,
        details: dict[str, JsonValue] | None = None,
    ) -> None:
        self.code = code
        self.action = action
        self.details = dict(details or {})
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

    def __init__(self, message: str, *, details: dict[str, JsonValue] | None = None) -> None:
        self.details = dict(details or {})
        super().__init__(message)


def _computer_use_route(server: str, tool: str) -> bool:
    names = re.split(r"[.:/]|__", f"{server}.{tool}".casefold().replace("-", "_"))
    return bool({"cua_repl", "unified_computer_use"}.intersection(names))


def _computer_use_compatibility() -> dict[str, JsonValue]:
    return {
        "state": "unsupported_execution_context",
        "screen_read": "unverified", "native_actions": "unsupported_execution_context",
        "browser_actions": "unsupported_execution_context",
        "turn_context": "not_provided", "inference_requested": False,
        "next_action": "Use Computer Use in its owning Codex client. This direct bridge "
        "has no verified non-inference API to activate its required execution context.",
    }


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
        raise PluginPreflightError(
            "recursive_route_forbidden",
            "Anywhere Computer's own server cannot be routed through this bridge",
        )
    normalized = tool.casefold().replace("-", "_")
    # Codex Apps uses provider.tool; MCP clients can expose mcp__provider__tool.
    # Match a complete provider component, not every dotted third-party name.
    namespaces = re.split(r"[.:/]|__", normalized)
    if "anywhere_computer" in namespaces[:-1] or normalized.startswith(_DENIED_TOOL_PREFIXES):
        raise PluginPreflightError(
            "recursive_route_forbidden", "Recursive or local tool routing is forbidden",
        )


def _json_object(value: object, message: str) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        raise ValueError(message)
    return cast(dict[str, JsonValue], value)




def _descriptor_digest(
    server: str, cwd: str, tool: dict[str, JsonValue], *,
    auth_status: JsonValue,
) -> str:
    payload = json.dumps({
        "server": server, "cwd": cwd, "name": tool["name"],
        "description": tool.get("description", ""),
        "inputSchema": tool.get("inputSchema", {}),
        "annotations": tool.get("annotations", {}),
        "auth_status": auth_status,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode()).hexdigest()




def _validate_inspection(limit: int, server: str | None, tool: str | None) -> None:
    if not 1 <= limit <= MAX_CATALOG:
        raise ValueError("Plugin catalog limit must be between 1 and 30")
    if tool is not None and server is None:
        raise PluginPreflightError("invalid_selection", "Specify server with tool")
    for name in (server, tool):
        if name is not None and not _SAFE_NAME.fullmatch(name):
            raise PluginPreflightError("invalid_selection", "Use an exact catalog name")
    if server is not None:
        _deny(server, tool or "")


def _validate_call(server: str, tool: str, catalog_sha256: str) -> None:
    if not _SAFE_NAME.fullmatch(server) or not _SAFE_NAME.fullmatch(tool):
        raise ValueError("Plugin server or tool name is invalid")
    if not re.fullmatch(r"[a-f0-9]{64}", catalog_sha256):
        raise ValueError("Plugin catalog digest is invalid")
    _deny(server, tool)
    if _computer_use_route(server, tool):
        raise PluginPreflightError(
            "unsupported_execution_context", str(_computer_use_compatibility()["next_action"]),
            details={"failure_stage": "before_dispatch",
                     "compatibility": _computer_use_compatibility()},
        )


class _Session:
    _ALLOWED = {
        "initialize", "thread/start", "mcpServerStatus/list", "mcpServer/tool/call",
        "thread/unsubscribe",
    }

    def __init__(self, executable: Path) -> None:
        self.executable = executable
        self.process: asyncio.subprocess.Process | None = None
        self.next_id = 0
        self.diagnostics = PluginDiagnostics()
        self._stderr_task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> "_Session":
        self.process = await asyncio.create_subprocess_exec(
            str(self.executable), "app-server", "--listen", "stdio://",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, cwd=str(Path.home()), limit=WIRE_LIMIT,
            env=with_tool_path(os.environ),
        )
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        try:
            await asyncio.wait_for(self._initialize(), timeout=STARTUP_TIMEOUT)
            return self
        except BaseException:
            await self._close()
            raise

    async def __aexit__(self, *_: object) -> None:
        await self._close()

    async def _close(self) -> None:
        try:
            await self._close_process()
        finally:
            if self._stderr_task is not None:
                try:
                    await asyncio.wait_for(asyncio.shield(self._stderr_task), timeout=0.1)
                except TimeoutError:
                    self._stderr_task.cancel()
                    await asyncio.gather(self._stderr_task, return_exceptions=True)
                self._stderr_task = None

    async def _drain_stderr(self) -> None:
        assert self.process is not None and self.process.stderr is not None
        try:
            while chunk := await self.process.stderr.read(STDERR_CHUNK):
                self.diagnostics.feed(chunk)
        except (OSError, ValueError):
            self.diagnostics.capture_failed = True

    async def _close_process(self) -> None:
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
                raise PluginRPCError(message["error"])
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


async def _start_thread(session: _Session, cwd: str) -> str:
    started = await session.request("thread/start", {"ephemeral": True, "cwd": cwd})
    thread_id = started.get("threadId", started.get("thread_id"))
    if isinstance(started.get("thread"), dict):
        thread = cast(dict[str, JsonValue], started["thread"])
        thread_id = thread.get("id", thread.get("threadId"))
    if not isinstance(thread_id, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", thread_id) is None:
        raise ValueError("Codex returned an invalid ephemeral thread ID")
    return thread_id


async def _start_and_catalog(
    session: _Session, cwd: str, *, limit: int, cursor: str | None = None,
    max_pages: int = MAX_PAGES, thread_id: str | None = None,
    target_server: str | None = None, target_tool: str | None = None,
    progress: dict[str, JsonValue] | None = None,
) -> tuple[list[dict[str, JsonValue]], str | None, str]:
    if thread_id is None:
        thread_id = await _start_thread(session, cwd)
    servers: list[dict[str, JsonValue]] = []
    seen_servers: set[str] = set()
    page_cursor = cursor
    seen_cursors: set[str] = set()
    for page in range(max_pages):
        params: dict[str, JsonValue] = {
            "threadId": thread_id, "detail": "toolsAndAuthOnly", "limit": limit,
        }
        if page_cursor is not None:
            params["cursor"] = page_cursor
        if progress is not None:
            progress["catalog_page"] = page + 1
            progress["catalog_rpc_state"] = "waiting"
        result = await session.request("mcpServerStatus/list", params)
        if progress is not None:
            progress["catalog_rpc_state"] = "received"
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
            if target_server is not None and server != target_server:
                continue
            raw_tools = row.get("tools", [])
            if isinstance(raw_tools, dict):
                raw_tools = [dict(value, name=key) for key, value in raw_tools.items()
                             if isinstance(value, dict)]
            if not isinstance(raw_tools, list):
                raise ValueError("Invalid plugin tool catalog")
            complete = target_tool is not None or len(raw_tools) <= 1000
            received_count = len(raw_tools)
            if target_tool is not None:
                raw_tools = [item for item in raw_tools
                             if isinstance(item, dict) and item.get("name") == target_tool]
            else:
                raw_tools = raw_tools[:1000]
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
                clean["catalog_sha256"] = _descriptor_digest(
                    server, cwd, clean,
                    auth_status=row.get("authStatus"),
                )
                clean["call_arguments"] = {
                    "cwd": cwd, "server": server, "tool": name,
                    "catalog_sha256": clean["catalog_sha256"],
                }
                if _computer_use_route(server, name):
                    clean["compatibility"] = _computer_use_compatibility()
                    clean["availability"] = "unsupported_execution_context"
                else:
                    clean["availability"] = _availability(row, True)
                tools.append(clean)
            server_availability = _availability(row, bool(tools))
            if (server_availability == "ready_to_call" and tools
                    and all(isinstance(item, dict) and item.get("availability")
                            == "unsupported_execution_context"
                            for item in tools)):
                server_availability = "unsupported_execution_context"
            servers.append({
                "server": server, "tools": tools,
                "catalog_complete": complete, "received_tool_count": received_count,
                "omitted_tool_count": max(0, received_count - 1000) if not complete else 0,
                "availability": server_availability,
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


async def _inspect_tools(
    context: "PluginContext", limit: int = MAX_CATALOG, cursor: str | None = None,
    *, server: str | None = None, tool: str | None = None,
    query: str | None = None, summary: bool = False,
) -> dict[str, JsonValue]:
    _validate_inspection(limit, server, tool)
    clean_cwd = context.cwd
    bounded_cursor = _cursor(cursor)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + STARTUP_TIMEOUT
    empty_tools_since: float | None = None
    while True:
        context._catalog_poll += 1
        context._catalog_progress = {"catalog_poll": context._catalog_poll}
        servers, next_cursor, _ = await asyncio.wait_for(
            _start_and_catalog(
                context.session, clean_cwd, limit=limit, cursor=bounded_cursor,
                max_pages=MAX_PAGES if server else 1, thread_id=context.thread_id,
                target_server=server, target_tool=tool,
                progress=context._catalog_progress,
            ), timeout=max(0.001, deadline - loop.time()),
        )
        selected_server = next((row for row in servers if row.get('server') == server), None)
        starting = selected_server is not None and selected_server.get(
            'availability') == 'runtime_not_ready'
        empty_tools = (selected_server is not None
                       and selected_server.get('runtime_status') == 'connected'
                       and selected_server.get('received_tool_count') == 0)
        now = loop.time()
        if empty_tools and empty_tools_since is None:
            empty_tools_since = now
        if (not starting and not empty_tools) or now >= deadline or (
                empty_tools and empty_tools_since is not None
                and now - empty_tools_since >= 5):
            break
        # Keep the same Codex process and ephemeral thread while its MCP server
        # starts. A connected server may also publish its tool catalog shortly
        # after its runtime status changes.
        await asyncio.sleep(min(0.25, max(0, deadline - loop.time())))
    selected_servers: list[dict[str, JsonValue]] = []
    for row in servers:
        if server is not None and row["server"] != server:
            continue
        descriptors = cast(list[dict[str, JsonValue]], row["tools"])
        selected = [item for item in descriptors if
                    (tool is None or item["name"] == tool) and
                    (query is None or query.casefold() in
                     (f"{row['server']} {item['name']} {item['description']} "
                      f"{'computer use' if 'compatibility' in item else ''}").casefold())]
        if (query is not None and not selected
                and query.casefold() not in str(row["server"]).casefold()):
            continue
        clean_row = dict(row)
        clean_row["tool_count"] = len(selected)
        clean_row["inspect_arguments"] = {"cwd": clean_cwd, "server": row["server"]}
        if any("compatibility" in item for item in selected):
            clean_row["computer_use_compatibility"] = _computer_use_compatibility()
        if summary:
            clean_row["tools"] = []
            clean_row["tool_names_preview"] = [item["name"] for item in selected[:10]]
        else:
            rendered: list[JsonValue] = []
            for item in selected:
                preview = dict(item)
                description = cast(str, item["description"])
                if tool is None and len(description) > 1000:
                    preview["description"] = description[:1000]
                    preview["description_truncated"] = True
                rendered.append(preview)
            clean_row["tools"] = rendered
        selected_servers.append(clean_row)
    result: dict[str, JsonValue] = {
        "cwd": clean_cwd, "servers": cast(JsonValue, selected_servers),
        "inference_requested": False,
        "next_action": "Use codex_plugin_tools with server and optional tool for exact schemas; "
        "copy the selected tool\'s call_arguments into codex_plugin_call and add arguments. "
        "Registration and schemas do not prove successful execution.",
    }
    diagnostics = getattr(context.session, "diagnostics", None)
    if isinstance(diagnostics, PluginDiagnostics):
        result["runtime_diagnostics"] = diagnostics.snapshot()
    if next_cursor is not None:
        result["next_cursor"] = next_cursor
    return result


async def _call_tool(
    context: "PluginContext", server: str, tool: str,
    arguments: dict[str, JsonValue], catalog_sha256: str,
) -> dict[str, JsonValue]:
    _validate_call(server, tool, catalog_sha256)
    clean_cwd = context.cwd
    session, thread_id = context.session, context.thread_id
    loop = asyncio.get_running_loop()
    deadline = loop.time() + STARTUP_TIMEOUT
    missing_tool_since: float | None = None
    while True:
        context._catalog_poll += 1
        context._catalog_progress = {"catalog_poll": context._catalog_poll}
        servers, remaining_cursor, _ = await asyncio.wait_for(
            _start_and_catalog(
                session, clean_cwd, limit=MAX_CATALOG, max_pages=MAX_PAGES,
                thread_id=thread_id, target_server=server, target_tool=tool,
                progress=context._catalog_progress,
            ), timeout=max(0.001, deadline - loop.time()),
        )
        selected_server = next((row for row in servers if row.get("server") == server), None)
        starting = (selected_server is not None and
                    selected_server.get("availability") == "runtime_not_ready")
        missing_connected_tool = (
            selected_server is not None and
            selected_server.get("runtime_status") == "connected" and
            selected_server.get("availability") == "unverified" and
            not selected_server.get("tools") and remaining_cursor is None
        )
        now = loop.time()
        if missing_connected_tool and missing_tool_since is None:
            missing_tool_since = now
        if (not starting and not missing_connected_tool) or now >= deadline or (
                missing_connected_tool and missing_tool_since is not None and
                now - missing_tool_since >= 5):
            break
        # Observe startup in this process only. Never replay a dispatched tool or
        # retry a failed catalog RPC, which can leave an unread response behind.
        await asyncio.sleep(min(0.25, max(0, deadline - loop.time())))
        if loop.time() >= deadline:
            break
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
    if (missing_connected_tool and selected_server is not None and
            selected_server.get("received_tool_count") == 0):
        raise PluginPreflightError(
            "runtime_not_ready",
            "The connected plugin has not published its tools; inspect this server again",
        )
    if selected_server is not None and selected_server.get("availability") in {
        "authentication_required", "runtime_not_ready", "unavailable",
    }:
        raise PluginPreflightError(
            str(selected_server["availability"]),
            "Inspect this server and repair its local authentication/runtime "
            "before retrying",
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
            details={
                "cwd": clean_cwd, "server": server, "tool": tool,
                "received_catalog_sha256": catalog_sha256,
                "current_catalog_sha256": selected["catalog_sha256"],
            },
        )
    try:
        result = await asyncio.wait_for(session.request("mcpServer/tool/call", {
            "threadId": thread_id, "server": server, "tool": tool,
            "arguments": arguments,
        }), timeout=CALL_TIMEOUT)
    except (
        TimeoutError, ConnectionError, OSError, ValueError, RuntimeError,
        json.JSONDecodeError
    ) as exc:
        raise PluginCallOutcomeUnknown(
            "Plugin call outcome was not confirmed",
            details=context.failure_details(exc, "after_dispatch"),
        ) from exc
    try:
        return _tool_result(result)
    except (ValueError, TypeError, RuntimeError) as error:
        raise PluginCallOutcomeUnknown(
            "Plugin returned a malformed result",
            details=context.failure_details(error, "after_dispatch"),
        ) from error


class PluginContext:
    """One owned App Server process and ephemeral thread; never starts a model turn."""

    def __init__(self, cwd: str) -> None:
        self.cwd = _cwd(cwd)
        self._session: _Session | None = None
        self.thread_id: str | None = None
        self._catalog_poll = 0
        self._catalog_progress: dict[str, JsonValue] = {}

    @property
    def session(self) -> _Session:
        if self._session is None:
            raise ConnectionError("Plugin context is not open")
        return self._session

    @property
    def alive(self) -> bool:
        process = self._session.process if self._session is not None else None
        return process is not None and process.returncode is None

    async def open(self) -> None:
        if self._session is not None:
            raise RuntimeError("Plugin context is already open")
        try:
            self._session = _Session(_executable(None))
            async with asyncio.timeout(STARTUP_TIMEOUT):
                await self._session.__aenter__()
                self.thread_id = await _start_thread(self._session, self.cwd)
        except (OSError, ValueError, RuntimeError, TimeoutError) as error:
            details = self.failure_details(error, "startup")
            await self.close()
            raise PluginPreflightError(
                "plugin_start_failed", "Check the installed Codex runtime and local MCP setup",
                details=details,
            ) from error
        except BaseException:
            await self.close()
            raise

    async def close(self) -> None:
        if self._session is None:
            return
        try:
            if self.thread_id is not None:
                try:
                    await asyncio.wait_for(self._session.request(
                        "thread/unsubscribe", {"threadId": self.thread_id},
                    ), timeout=1)
                except (TimeoutError, ConnectionError, OSError, ValueError, RuntimeError):
                    pass
        finally:
            await self._session.__aexit__()
            self._session = None
            self.thread_id = None

    async def __aenter__(self) -> "PluginContext":
        await self.open()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def inspect(
        self, *, limit: int = MAX_CATALOG, cursor: str | None = None,
        server: str | None = None, tool: str | None = None,
        query: str | None = None, summary: bool = False,
    ) -> dict[str, JsonValue]:
        try:
            return await _inspect_tools(
                self, limit, cursor, server=server, tool=tool, query=query, summary=summary,
            )
        except PluginPreflightError:
            raise
        except (OSError, ValueError, RuntimeError, TimeoutError) as error:
            raise PluginPreflightError(
                "plugin_catalog_failed", "Inspect local MCP runtime diagnostics before retrying",
                details=self.failure_details(error, "catalog"),
            ) from error

    def failure_details(self, error: BaseException, stage: str) -> dict[str, JsonValue]:
        details = failure_diagnostic(error, stage)
        if stage == "catalog" and self._catalog_progress:
            details["catalog_progress"] = dict(self._catalog_progress)
        if self._session is not None:
            # Test doubles may implement only the transport interface.
            diagnostics = getattr(self._session, "diagnostics", None)
            if isinstance(diagnostics, PluginDiagnostics):
                details["runtime_diagnostics"] = diagnostics.snapshot()
        return details

    async def call(
        self, server: str, tool: str, arguments: dict[str, JsonValue], catalog_sha256: str,
    ) -> dict[str, JsonValue]:
        _validate_call(server, tool, catalog_sha256)
        try:
            return await _call_tool(self, server, tool, arguments, catalog_sha256)
        except (PluginPreflightError, PluginCallOutcomeUnknown):
            raise
        except (OSError, ValueError, RuntimeError, TimeoutError) as error:
            raise PluginPreflightError(
                "plugin_catalog_failed", "Inspect local MCP runtime diagnostics before retrying",
                details=self.failure_details(error, "catalog"),
            ) from error


async def list_codex_plugin_tools(
    cwd: str, limit: int = MAX_CATALOG, cursor: str | None = None,
    *, server: str | None = None, tool: str | None = None,
    query: str | None = None, summary: bool = False,
) -> dict[str, JsonValue]:
    _validate_inspection(limit, server, tool)
    _cursor(cursor)
    async with PluginContext(cwd) as context:
        return await context.inspect(
            limit=limit, cursor=cursor, server=server, tool=tool, query=query, summary=summary,
        )


async def call_codex_plugin_tool(
    cwd: str, server: str, tool: str, arguments: dict[str, JsonValue], catalog_sha256: str,
) -> dict[str, JsonValue]:
    # Reject recursive routes before starting any installed runtime.
    _validate_call(server, tool, catalog_sha256)
    if tool in STATEFUL_SUBCHAT_TOOLS:
        raise PluginPreflightError(
            "plugin_session_required",
            "Open codex_plugin_session_open, then inspect and call this Subchat tool "
            "with the returned session_id. Keep that session open through answer recovery.",
        )
    async with PluginContext(cwd) as context:
        return await context.call(server, tool, arguments, catalog_sha256)
