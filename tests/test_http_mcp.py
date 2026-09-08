import asyncio
import json

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from anywhere_computer.engine import Engine
from anywhere_computer.http_mcp import HTTPMCP
from anywhere_computer.mcp_server import MCPSession

HEADERS = {
    "Authorization": "Bearer test-owner-token",
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}
INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-11-25",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "1"},
    },
}


@pytest.fixture
async def http_agent(tmp_path):
    engine = Engine(tmp_path / "state")
    identities = {"test-owner-token": "owner", "other-token": "other"}

    async def authenticate(token):
        return identities.get(token)

    async def catalog():
        return engine.catalog()

    adapter = HTTPMCP(authenticate, lambda owner: MCPSession(catalog, engine.execute))
    port = await adapter.start()
    try:
        yield adapter, engine, identities, port
    finally:
        await adapter.close()
        await engine.close()


async def test_official_sdk_streamable_http_can_execute_and_delete_session(http_agent, tmp_path):
    adapter, engine, _, port = http_agent
    async with asyncio.timeout(15):
        async with httpx.AsyncClient(headers=HEADERS) as http:
            async with streamable_http_client(f"http://127.0.0.1:{port}/mcp", http_client=http) as (
                reader,
                writer,
                session_id,
            ):
                async with ClientSession(reader, writer) as client:
                    initialized = await client.initialize()
                    assert initialized.serverInfo.name == "anywhere-computer"
                    assert session_id() in adapter.sessions
                    assert len((await client.list_tools()).tools) == len(engine.tools)
                    path = str(tmp_path / "HTTP 日本語.txt")
                    written = await client.call_tool("files_write", {"path": path, "text": "接続"})
                    assert not written.isError
                    read = await client.call_tool("files_read", {"path": path})
                    assert read.structuredContent["data"]["text"] == "接続"
                    await client.send_ping()
        assert not adapter.sessions


async def test_http_identity_origin_expiry_and_revocation(http_agent):
    adapter, _, identities, port = http_agent
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", headers=HEADERS) as http:
        assert (
            await http.post("/mcp", json=INITIALIZE, headers={"Origin": "https://evil.test"})
        ).status_code == 403
        assert (
            await http.post("/mcp", json=INITIALIZE, headers={"Host": "evil.test"})
        ).status_code == 403
        unauthorized = await http.post(
            "/mcp", json=INITIALIZE, headers={"Authorization": "Bearer wrong"}
        )
        assert (
            unauthorized.status_code == 401 and unauthorized.headers["www-authenticate"] == "Bearer"
        )
        response = await http.post("/mcp", json=INITIALIZE)
        session = response.headers["mcp-session-id"]
        http.headers["MCP-Session-Id"] = session
        notification = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        accepted = await http.post("/mcp", json=notification)
        assert accepted.status_code == 202 and accepted.content == b""
        assert (await http.get("/mcp")).status_code == 405
        ping = {"jsonrpc": "2.0", "id": 2, "method": "ping"}
        stolen = await http.post("/mcp", json=ping, headers={"Authorization": "Bearer other-token"})
        assert stolen.status_code == 404
        identities.pop("test-owner-token")
        assert (await http.post("/mcp", json=ping)).status_code == 401
        identities["test-owner-token"] = "owner"
        adapter.sessions[session].touched -= 1801
        assert (await http.post("/mcp", json=ping)).status_code == 404
        assert not adapter.sessions


async def test_http_bounds_and_protocol_errors(http_agent):
    adapter, _, _, port = http_agent
    adapter.max_sessions = 1
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", headers=HEADERS) as http:
        assert (
            await http.post("/mcp", json=INITIALIZE, headers={"Accept": "application/json"})
        ).status_code == 406
        assert (
            await http.post("/mcp", json=INITIALIZE, headers={"Content-Type": "text/plain"})
        ).status_code == 415
        assert (
            await http.post("/mcp", json=INITIALIZE, headers={"MCP-Protocol-Version": "invalid"})
        ).status_code == 400
        assert (await http.post("/mcp", content='{"id":NaN}')).status_code == 400
        assert (await http.post("/mcp", json=[])).status_code == 400
        assert (
            await http.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        ).status_code == 400
        assert (await http.post("/mcp", json=INITIALIZE)).status_code == 200
        assert (await http.post("/mcp", json=INITIALIZE)).status_code == 503
        assert len(adapter.sessions) == 1


@pytest.mark.parametrize(
    "framing",
    [
        "Content-Length: 0\r\nContent-Length: 1\r\n",
        "Content-Length: 0\r\nTransfer-Encoding: chunked\r\n",
        "Content-Length: 99999999\r\n",
    ],
)
async def test_http_rejects_ambiguous_or_oversized_frames(http_agent, framing):
    _, _, _, port = http_agent
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(
            (f"POST /mcp HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n" + framing + "\r\n").encode()
        )
        await writer.drain()
        response = await asyncio.wait_for(reader.read(), 3)
        assert response.startswith((b"HTTP/1.1 400", b"HTTP/1.1 413"))
    finally:
        writer.close()
        await writer.wait_closed()


async def test_revocation_is_checked_after_slow_body(http_agent):
    adapter, _, identities, port = http_agent
    body = json.dumps(INITIALIZE).encode()
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        head = f"POST /mcp HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
        head += "".join(f"{name}: {value}\r\n" for name, value in HEADERS.items())
        head += f"Content-Length: {len(body)}\r\n\r\n"
        writer.write(head.encode() + body[:5])
        await writer.drain()
        identities.clear()
        writer.write(body[5:])
        await writer.drain()
        assert (await asyncio.wait_for(reader.read(), 3)).startswith(b"HTTP/1.1 401")
        assert not adapter.sessions
    finally:
        writer.close()
        await writer.wait_closed()


async def test_disconnected_http_observer_does_not_cancel_dispatched_work(http_agent, tmp_path):
    adapter, engine, _, port = http_agent
    from anywhere_computer.models import Empty

    started, finish, completed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    target = tmp_path / "completed-after-disconnect.txt"

    async def delayed(args: Empty):
        started.set()
        await finish.wait()
        target.write_text("once", encoding="utf-8")
        completed.set()
        return {"written": True}

    engine.register("test_delayed_write", "Disposable test operation", Empty, delayed)
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", headers=HEADERS) as http:
        response = await http.post("/mcp", json=INITIALIZE)
        session = response.headers["mcp-session-id"]
        await http.post(
            "/mcp",
            headers={"MCP-Session-Id": session},
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
    _, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        packet = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 10,
                "method": "tools/call",
                "params": {"name": "test_delayed_write", "arguments": {}},
            }
        ).encode()
        headers = {**HEADERS, "MCP-Session-Id": session, "Content-Length": str(len(packet))}
        head = f"POST /mcp HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
        head += "".join(f"{name}: {value}\r\n" for name, value in headers.items())
        writer.write(head.encode() + b"\r\n" + packet)
        await writer.drain()
        await asyncio.wait_for(started.wait(), 3)
        writer.close()
        await writer.wait_closed()
        finish.set()
        await asyncio.wait_for(completed.wait(), 3)
        await adapter.close()
        assert target.read_text(encoding="utf-8") == "once"
        operations = engine.ledger.recent(10)
        assert len(operations) == 1 and operations[0]["state"] == "completed"
    finally:
        finish.set()
        writer.close()
        await writer.wait_closed()
