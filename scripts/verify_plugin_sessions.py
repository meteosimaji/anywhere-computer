#!/usr/bin/env python3
"""Verify stateful plugin sessions through authenticated HTTP and a real Codex binary.

Owns an isolated CODEX_HOME, counter MCP fixture, OAuth store and loopback port.
No production grants, configuration or services are changed. Reports contain only
fixture outcomes, protocol method names and sanitized verification flags.
"""

import argparse
import asyncio
import json
import os
import sys
import tempfile
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import psutil

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
PNG = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8"
       "/x8AAwMCAO+a5XcAAAAASUVORK5CYII=")
TOOLS = frozenset({
    "codex_plugin_session_open", "codex_plugin_session_status", "codex_plugin_session_close",
    "codex_plugin_tools", "codex_plugin_call", "operations_get",
})


def counter_worker() -> None:
    count = 0
    process = psutil.Process()
    for line in sys.stdin:
        request = json.loads(line)
        if "id" not in request:
            continue
        method = request.get("method")
        if method == "initialize":
            result = {
                "protocolVersion": "2025-11-25", "capabilities": {"tools": {}},
                "serverInfo": {"name": "stateful-counter-fixture", "version": "1"},
            }
        elif method == "tools/list":
            result = {"tools": [{
                "name": "step", "description": "Increment an isolated in-memory test counter",
                "inputSchema": {"type": "object", "properties": {
                    "delta": {"type": "integer"}, "image": {"type": "boolean"},
                }, "additionalProperties": False},
            }]}
        elif method == "tools/call":
            arguments = request.get("params", {}).get("arguments", {})
            count += arguments.get("delta", 0)
            content = [{"type": "text", "text": str(count)}]
            if arguments.get("image"):
                content.append({"type": "image", "mimeType": "image/png", "data": PNG})
            result = {"content": content, "isError": False, "structuredContent": {
                "count": count, "fixture_pid": process.pid, "created": process.create_time(),
            }}
        else:
            result = {}
        print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)


@asynccontextmanager
async def client_for(url, token):
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    async with httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=65) as http:
        async with streamable_http_client(url, http_client=http) as (reader, writer, _):
            async with ClientSession(reader, writer) as client:
                await client.initialize()
                yield client


async def call(client, tool, arguments, *, operation_id=None):
    from anywhere_computer.mcp_server import OPERATION_META

    metadata = {OPERATION_META: operation_id} if operation_id else None
    result = await client.call_tool(tool, arguments, meta=metadata)
    assert not result.isError, result.structuredContent
    assert result.structuredContent["state"] == "completed", result.structuredContent
    return result


async def inspect(client, cwd, session_id=None):
    arguments = {"cwd": str(cwd), "server": "counter_fixture", "tool": "step"}
    if session_id is not None:
        arguments["session_id"] = session_id
    result = await call(client, "codex_plugin_tools", arguments)
    rows = result.structuredContent["data"]["servers"]
    assert len(rows) == 1 and len(rows[0]["tools"]) == 1
    return rows[0]["tools"][0]["call_arguments"]


def counter_data(result):
    return result.structuredContent["data"]["structured_content"]


async def verify(codex: Path) -> dict:
    from mcp.types import ImageContent

    from anywhere_computer import codex_plugins
    from anywhere_computer.authorization import AuthorizationStore, pkce_s256
    from anywhere_computer.authorized_http import AuthorizedDeviceMCP
    from anywhere_computer.engine import Engine
    from anywhere_computer.http_mcp import HTTPMCP

    report = {"transport": "official-mcp-sdk/authenticated-loopback-http"}
    trace = []
    identities = set()
    original_send = codex_plugins._Session._send

    async def recording_send(session, packet):
        await original_send(session, packet)
        method = packet.get("method")
        if isinstance(method, str):
            row = {"method": method}
            if method == "thread/start":
                row["ephemeral"] = packet.get("params", {}).get("ephemeral")
            trace.append(row)

    def remember(result):
        data = counter_data(result)
        identities.add((data["fixture_pid"], data["created"]))
        return data["count"]

    with tempfile.TemporaryDirectory(prefix="plugin-sessions-") as raw:
        root = Path(raw)
        home = root / "codex-home"
        home.mkdir()
        (home / "config.toml").write_text(
            "[mcp_servers.counter_fixture]\n"
            f"command = {json.dumps(sys.executable)}\n"
            f"args = [\"-I\", {json.dumps(str(Path(__file__).resolve()))}, \"--worker\"]\n"
            "startup_timeout_sec = 5\n", encoding="utf-8",
        )
        previous = {key: os.environ.get(key) for key in ("CODEX_HOME", "ANYWHERE_CODEX_EXECUTABLE")}
        os.environ.update(CODEX_HOME=str(home), ANYWHERE_CODEX_EXECUTABLE=str(codex))
        codex_plugins._Session._send = recording_send
        engine = Engine(root / "engine")
        resource, redirect = "https://fixture.example/mcp", "https://client.example/callback"
        authority = AuthorizationStore(root / "auth", resource=resource,
                                       known_tools=frozenset(engine.tools))
        authority.register_client("fixture", frozenset({redirect}))
        authority.enroll_device("owner", "device", TOOLS)

        def issue(tools):
            verifier = "x" * 43
            code = authority.approve(
                owner="owner", device="device", client="fixture", redirect=redirect,
                resource=resource, tools=tools, challenge=pkce_s256(verifier),
            )
            return authority.exchange_code(
                code=code, verifier=verifier, client="fixture",
                redirect=redirect, resource=resource,
            )

        first_token = issue(TOOLS)
        other_token = issue(TOOLS)
        legacy_token = issue(frozenset({
            "codex_plugin_tools", "codex_plugin_call", "operations_get",
        }))
        backend = AuthorizedDeviceMCP(authority, engine, owner="owner", device="device")
        adapter = HTTPMCP(backend.authenticate, backend.session)
        port = await adapter.start()
        url = f"http://127.0.0.1:{port}/mcp"
        try:
            async with asyncio.timeout(180):
                async with client_for(url, first_token.value) as client:
                    arguments = await inspect(client, root)
                    legacy = []
                    for _ in range(2):
                        result = await call(client, "codex_plugin_call", {
                            **arguments, "arguments": {"delta": 1},
                        })
                        legacy.append(remember(result))
                    assert legacy == [1, 1]
                    report["one_shot_values"] = legacy
                    opened = await call(client, "codex_plugin_session_open", {"cwd": str(root)})
                    session_id = opened.structuredContent["data"]["session_id"]
                    arguments = await inspect(client, root, session_id)
                    assert arguments["session_id"] == session_id
                    first = await call(client, "codex_plugin_call", {
                        **arguments, "arguments": {"delta": 1, "image": True},
                    })
                    assert remember(first) == 1
                    assert any(isinstance(item, ImageContent) and item.data == PNG
                               for item in first.content)
                    assert PNG not in json.dumps(first.structuredContent)
                    calls_before_disconnect = sum(row["method"] == "thread/start" for row in trace)
                assert not adapter.sessions
                refreshed = authority.refresh(
                    refresh_token=first_token.refresh_value, client="fixture", resource=resource,
                )
                async with client_for(url, refreshed.value) as client:
                    operation_id = uuid.uuid4().hex
                    second = await call(client, "codex_plugin_call", {
                        **arguments, "arguments": {"delta": 1},
                    }, operation_id=operation_id)
                    assert remember(second) == 2
                    repeated = await call(client, "codex_plugin_call", {
                        **arguments, "arguments": {"delta": 1},
                    }, operation_id=operation_id)
                    assert repeated.structuredContent == second.structuredContent
                    recovered = await call(client, "operations_get", {"operation_id": operation_id})
                    assert recovered.structuredContent["data"]["operation_id"] == operation_id
                    saved = recovered.structuredContent["data"]["data"]["structured_content"]
                    assert saved["count"] == 2
                    before = sum(row["method"] == "mcpServer/tool/call" for row in trace)
                    stale = await client.call_tool("codex_plugin_call", {
                        **arguments, "arguments": {"delta": 100}, "catalog_sha256": "0" * 64,
                    })
                    assert stale.isError
                    assert stale.structuredContent["data"]["error_code"] == "catalog_stale"
                    assert stale.structuredContent["data"]["dispatched"] is False
                    assert before == sum(row["method"] == "mcpServer/tool/call" for row in trace)
                    unchanged = await call(client, "codex_plugin_call", {
                        **arguments, "arguments": {"delta": 0},
                    })
                    assert remember(unchanged) == 2
                    starts = sum(row["method"] == "thread/start" for row in trace)
                    assert calls_before_disconnect == starts
                    report.update(stateful_values=[1, 2], http_reconnect_preserved_state=True,
                                  token_refresh_preserved_state=True, native_image=True,
                                  operation_replay_dispatched_once=True,
                                  stale_never_dispatched=True)
                async with client_for(url, other_token.value) as client:
                    for tool, params in [
                        ("codex_plugin_session_status", {"session_id": session_id}),
                        ("codex_plugin_session_close", {"session_id": session_id}),
                        ("codex_plugin_call", {**arguments, "arguments": {"delta": 100}}),
                    ]:
                        denied = await client.call_tool(tool, params)
                        assert denied.isError
                        assert denied.structuredContent["data"]["error_code"] == "session_not_found"
                    lookup = await client.call_tool(
                        "operations_get", {"operation_id": operation_id},
                    )
                    assert lookup.isError
                report["other_grant_denied"] = True
                async with client_for(url, legacy_token.value) as client:
                    names = {tool.name for tool in (await client.list_tools()).tools}
                    assert "codex_plugin_session_open" not in names
                    assert names == {"codex_plugin_tools", "codex_plugin_call", "operations_get"}
                report["existing_grants_not_expanded"] = True
                async with client_for(url, refreshed.value) as client:
                    stopped = await call(
                        client, "codex_plugin_session_close", {"session_id": session_id},
                    )
                    assert stopped.structuredContent["data"]["cleanup_confirmed"] is True
                    after_close = await client.call_tool("codex_plugin_call", {
                        **arguments, "arguments": {"delta": 100},
                    })
                    assert after_close.isError
                    assert after_close.structuredContent["data"]["error_code"] == "session_closed"
                report["closed_session_not_recreated"] = True
                grant = authority.verify(refreshed.value, resource=resource)
                authority.revoke(owner="owner", grant=grant.grant_id)
                assert await backend.authenticate(refreshed.value) is None
                report["revoked_grant_denied"] = True
        finally:
            await adapter.close()
            await engine.close()
            authority.close()
            codex_plugins._Session._send = original_send
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        deadline = asyncio.get_running_loop().time() + 3
        while True:
            live = []
            for pid, created in identities:
                try:
                    if abs(psutil.Process(pid).create_time() - created) < 0.01:
                        live.append(pid)
                except psutil.NoSuchProcess:
                    pass
            if not live:
                break
            assert asyncio.get_running_loop().time() < deadline, "Owned fixture child still running"
            await asyncio.sleep(0.05)
    methods = [row["method"] for row in trace]
    assert not {"turn/start", "turn/steer", "thread/resume"}.intersection(methods)
    assert all(row["ephemeral"] is True for row in trace if row["method"] == "thread/start")
    report.update(inference_requested=False, fixture_children_remaining=False, sent_methods=methods)
    return report


async def verify_registered(codex: Path, cwd: Path) -> dict:
    """Opt-in check against installed read-only docs; no content is retained."""
    from anywhere_computer import codex_plugins
    from anywhere_computer.engine import Engine
    from anywhere_computer.models import Request

    methods = []
    original_send = codex_plugins._Session._send
    previous = os.environ.get("ANYWHERE_CODEX_EXECUTABLE")
    os.environ["ANYWHERE_CODEX_EXECUTABLE"] = str(codex)

    async def record(session, packet):
        await original_send(session, packet)
        if isinstance(packet.get("method"), str):
            methods.append(packet["method"])
        if packet.get("method") == "thread/start":
            assert packet["params"]["ephemeral"] is True

    codex_plugins._Session._send = record
    try:
        with tempfile.TemporaryDirectory(prefix="installed-plugin-session-") as directory:
            engine = Engine(Path(directory))

            async def execute(tool, arguments):
                reply = await engine.execute(Request(
                    operation_id=uuid.uuid4().hex, tool=tool, arguments=arguments,
                ))
                assert reply.state == "completed", reply.error
                assert reply.data.get("is_error") is not True, reply.data
                return reply.data

            try:
                opened = await execute("codex_plugin_session_open", {"cwd": str(cwd)})
                inspected = await execute("codex_plugin_tools", {
                    "cwd": str(cwd), "session_id": opened["session_id"],
                    "server": "openaiDeveloperDocs", "tool": "list_openai_docs",
                })
                descriptor = inspected["servers"][0]["tools"][0]
                for _ in range(2):
                    result = await execute("codex_plugin_call", {
                        **descriptor["call_arguments"], "arguments": {"limit": 1},
                    })
                    assert result["content"]
                closed = await execute("codex_plugin_session_close", {
                    "session_id": opened["session_id"],
                })
                assert closed["cleanup_confirmed"] is True
            finally:
                await engine.close()
    finally:
        codex_plugins._Session._send = original_send
        if previous is None:
            os.environ.pop("ANYWHERE_CODEX_EXECUTABLE", None)
        else:
            os.environ["ANYWHERE_CODEX_EXECUTABLE"] = previous
    assert methods.count("thread/start") == 1
    assert methods.count("mcpServer/tool/call") == 2
    assert not {"turn/start", "turn/steer", "thread/resume"}.intersection(methods)
    return {"transport": "isolated-engine/installed-codex-app-server",
            "server": "openaiDeveloperDocs", "tool": "list_openai_docs",
            "successful_calls": 2, "thread_starts": 1, "cleanup_confirmed": True,
            "inference_requested": False, "sent_methods": methods}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--codex", type=Path)
    parser.add_argument("--installed-cwd", type=Path,
                        help="Also check installed read-only docs in this workspace")
    args = parser.parse_args()
    if args.worker:
        counter_worker()
    else:
        if args.codex is None or not args.codex.is_absolute() or not args.codex.is_file():
            parser.error("--codex must name an existing absolute Codex executable")
        report = asyncio.run(verify(args.codex))
        if args.installed_cwd is not None:
            report["installed_docs"] = asyncio.run(verify_registered(
                args.codex, args.installed_cwd.resolve(),
            ))
        print(json.dumps(report, ensure_ascii=False, indent=2))
