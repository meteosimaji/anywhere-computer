"""Real local browser navigation, session isolation and owner binding."""

import asyncio
import uuid

import pytest

from anywhere_computer import engine as engine_module
from anywhere_computer.browser_control import BrowserControl
from anywhere_computer.engine import Engine
from anywhere_computer.models import BrowserNavigate, BrowserSession, Reply, Request


@pytest.fixture
async def local_page():
    async def serve(reader, writer):
        request = await reader.readuntil(b"\r\n\r\n")
        cookie = next((line for line in request.split(b"\r\n")
                       if line.lower().startswith(b"cookie:")), b"")
        is_cookie_page = request.startswith(b"GET /cookie ")
        body = (b"<html><head><title>Local fixture</title></head><body>"
                b"<p>Browser verified 42</p><p>" + cookie + b"</p></body></html>")
        headers = (b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n"
                   + (b"Set-Cookie: isolated=owner-a; Path=/\r\n" if is_cookie_page else b""))
        writer.write(headers
                     + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
                     + body)
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    try:
        yield f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/fixture"
    finally:
        server.close()
        await server.wait_closed()


async def test_isolated_browser_exact_owner_tab_and_stale_references(local_page):
    pytest.importorskip("playwright.async_api")
    control = BrowserControl(channel="chrome")
    try:
        opened = await control.open(owner="owner-a")
        session = BrowserSession(session_id=opened["session_id"], tab_id=opened["tab_id"])
        wrong_tab = BrowserSession(session_id=opened["session_id"], tab_id="f" * 32)
        with pytest.raises(ValueError, match="unavailable"):
            await control.observe(session, owner="owner-b")
        with pytest.raises(ValueError, match="unavailable"):
            await control.observe(wrong_tab, owner="owner-a")
        with pytest.raises(ValueError, match="HTTP or HTTPS"):
            await control.navigate(BrowserNavigate(**session.model_dump(), url="file:///etc/passwd"),
                                   owner="owner-a")
        navigated = await control.navigate(BrowserNavigate(**session.model_dump(), url=local_page),
                                           owner="owner-a")
        assert navigated["session_id"] == opened["session_id"]
        assert navigated["tab_id"] == opened["tab_id"]
        assert navigated["url"] == local_page
        assert navigated["title"] == "Local fixture"
        assert "Browser verified 42" in navigated["text"]
        assert navigated["http_status"] == 200
        assert (await control.observe(session, owner="owner-a"))["url"] == local_page
        assert (await control.stop(session, owner="owner-a"))["state"] == "closed"
        with pytest.raises(ValueError, match="unavailable"):
            await control.observe(session, owner="owner-a")
    finally:
        await control.close()


async def test_engine_browser_tools_are_owned_and_block_update(tmp_path, local_page, monkeypatch):
    pytest.importorskip("playwright.async_api")
    engine = Engine(tmp_path / "state")
    engine.browser.channel = "chrome"

    async def call(tool, arguments, peer):
        operation_id = uuid.uuid4().hex
        reply = await engine.execute(Request(operation_id=operation_id,
                                             tool=tool, arguments=arguments), peer=peer)
        async with asyncio.timeout(30):
            while reply.state == "running":
                recovered = await engine.execute(Request(
                    operation_id=uuid.uuid4().hex, tool="operations_get",
                    arguments={"operation_id": operation_id},
                ), peer=peer)
                if recovered.state == "running":
                    await asyncio.sleep(0.1)
                    continue
                assert recovered.state == "completed", recovered
                reply = Reply.model_validate(recovered.data)
                if reply.state == "running":
                    await asyncio.sleep(0.1)
        return reply

    try:
        with monkeypatch.context() as patch:
            patch.setattr(engine_module, "OBSERVER_WAIT_SECONDS", 0.001)
            opened = await call("browser_open", {}, "owner-a")
        assert opened.state == "completed", opened.error
        ids = {"session_id": opened.data["session_id"], "tab_id": opened.data["tab_id"]}
        assert engine.status(owner="owner-a")["active_resources"]["browser_sessions"] == 1
        assert engine.status(owner="owner-b")["active_resources"]["browser_sessions"] == 0
        assert engine.status(owner="owner-a")["update_blocked"] is True
        assert engine.status()["update_blocker_details"] == [{
            "resource": "browser_session", "id": ids["session_id"],
            "state": "running", "stop_tool": "browser_close",
            "tab_id": ids["tab_id"], "stop_available": False,
        }]
        denied = await call("browser_observe", ids, "owner-b")
        assert denied.state == "failed"
        assert local_page not in str(denied)
        moved = await call("browser_navigate", {**ids, "url": local_page}, "owner-a")
        assert moved.state == "completed", moved.error
        assert moved.data["url"] == local_page
        closed = await call("browser_close", ids, "owner-a")
        assert closed.state == "completed"
        assert engine.status(owner="owner-a")["active_resources"]["browser_sessions"] == 0
    finally:
        await engine.close()


async def test_browser_sessions_do_not_share_cookies(local_page):
    pytest.importorskip("playwright.async_api")
    control = BrowserControl(channel="chrome")
    try:
        first = await control.open(owner="owner-a")
        second = await control.open(owner="owner-a")
        first_ids = {"session_id": first["session_id"], "tab_id": first["tab_id"]}
        second_ids = {"session_id": second["session_id"], "tab_id": second["tab_id"]}
        await control.navigate(BrowserNavigate(**first_ids,
                          url=local_page.replace("/fixture", "/cookie")),
                               owner="owner-a")
        first_page = await control.navigate(BrowserNavigate(**first_ids, url=local_page),
                                            owner="owner-a")
        second_page = await control.navigate(BrowserNavigate(**second_ids, url=local_page),
                                             owner="owner-a")
        assert "isolated=owner-a" in first_page["text"]
        assert "isolated=owner-a" not in second_page["text"]
    finally:
        await control.close()
