"""Real local browser navigation, session isolation and owner binding."""

import asyncio
import uuid

import pytest

from anywhere_computer import engine as engine_module
from anywhere_computer.browser_control import BrowserControl
from anywhere_computer.engine import Engine
from anywhere_computer.models import (
    BrowserClick,
    BrowserFill,
    BrowserNavigate,
    BrowserSession,
    Reply,
    Request,
)


@pytest.fixture
async def local_page():
    async def serve(reader, writer):
        request = await reader.readuntil(b"\r\n\r\n")
        cookie = next((line for line in request.split(b"\r\n")
                       if line.lower().startswith(b"cookie:")), b"")
        is_cookie_page = request.startswith(b"GET /cookie ")
        body = (b"<html><head><title>Local fixture</title></head><body>"
                b"<p>Browser verified 42</p><p>" + cookie
                + b"</p><input id='entry' oninput=\"document.querySelector('#result').textContent"
                b"=this.value\"><button id='go' onclick=\"document.querySelector('#result')"
                b".textContent+=' clicked'\">Go</button><p id='result'></p></body></html>")
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


@pytest.mark.parametrize("close_target", ["page", "browser"])
async def test_dead_browser_session_releases_capacity_without_touching_live_owners(
    close_target,
):
    pytest.importorskip("playwright.async_api")
    control = BrowserControl(channel="chrome")
    try:
        opened = [await control.open(owner=f"owner-{index}") for index in range(4)]
        ended = opened[0]
        entry = control.entries[ended["session_id"]]
        if close_target == "page":
            await entry.page.close()
        else:
            await entry.browser.close()
        with pytest.raises(ValueError, match="ended"):
            await control.observe(BrowserSession(session_id=ended["session_id"],
                                                 tab_id=ended["tab_id"]), owner="owner-0")

        replacement = await control.open(owner="owner-new")
        assert ended["session_id"] not in control.entries
        assert len(control.entries) == 4
        assert replacement["session_id"] in control.entries
        for index, session in enumerate(opened[1:], start=1):
            observed = await control.observe(BrowserSession(
                session_id=session["session_id"], tab_id=session["tab_id"]),
                                             owner=f"owner-{index}")
            assert observed["session_id"] == session["session_id"]
    finally:
        await control.close()


@pytest.mark.parametrize("cancel_at", ["new_context", "new_page"])
async def test_cancelled_browser_open_closes_unregistered_resources(monkeypatch, cancel_at):
    pytest.importorskip("playwright.async_api")
    reached = asyncio.Event()
    calls = []

    class Context:
        async def new_page(self):
            if cancel_at == "new_page":
                reached.set()
                await asyncio.Future()
            raise AssertionError("Unexpected page creation")

    class Browser:
        async def new_context(self, **_kwargs):
            if cancel_at == "new_context":
                reached.set()
                await asyncio.Future()
            return Context()

        async def close(self):
            calls.append("browser.close")

    class Chromium:
        async def launch(self, **_kwargs):
            return Browser()

    class Driver:
        chromium = Chromium()

        async def start(self):
            return self

        async def stop(self):
            calls.append("driver.stop")

    driver = Driver()
    monkeypatch.setattr("playwright.async_api.async_playwright", lambda: driver)
    control = BrowserControl()
    opening = asyncio.create_task(control.open(owner="owner-a"))
    await asyncio.wait_for(reached.wait(), 5)
    opening.cancel()
    with pytest.raises(asyncio.CancelledError):
        await opening
    assert calls == ["browser.close", "driver.stop"]
    assert control.entries == {}
    await control.close()
    assert calls == ["browser.close", "driver.stop"]


async def test_cancelled_playwright_start_stops_connection(monkeypatch):
    pytest.importorskip("playwright.async_api")
    reached = asyncio.Event()
    release = asyncio.Event()
    calls = []

    class Driver:
        async def stop(self):
            calls.append("driver.stop")

    class Manager:
        async def start(self):
            reached.set()
            await release.wait()
            return Driver()

    manager = Manager()
    monkeypatch.setattr("playwright.async_api.async_playwright", lambda: manager)
    control = BrowserControl()
    opening = asyncio.create_task(control.open(owner="owner-a"))
    await asyncio.wait_for(reached.wait(), 5)
    opening.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(opening, 5)
    assert calls == ["driver.stop"]
    assert control.entries == {}


async def test_failed_playwright_start_preserves_original_error_and_unlocks(monkeypatch):
    pytest.importorskip("playwright.async_api")

    class Manager:
        async def start(self):
            raise FileNotFoundError("Playwright driver missing")

        async def __aexit__(self, *_args):
            raise AttributeError("_output")

    monkeypatch.setattr("playwright.async_api.async_playwright", Manager)
    control = BrowserControl()
    with pytest.raises(FileNotFoundError, match="Playwright driver missing"):
        await control.open(owner="owner-a")
    assert not control._lock.locked()
    assert control.entries == {}


async def test_failed_playwright_start_closes_started_transport(monkeypatch):
    pytest.importorskip("playwright.async_api")
    calls = []

    class Transport:
        _output = object()

    class Connection:
        _transport = Transport()

    class Manager:
        _connection = Connection()

        async def start(self):
            raise RuntimeError("Protocol initialization failed")

        async def __aexit__(self, *_args):
            calls.append("manager.exit")

    monkeypatch.setattr("playwright.async_api.async_playwright", Manager)
    control = BrowserControl()
    with pytest.raises(RuntimeError, match="Protocol initialization failed"):
        await control.open(owner="owner-a")
    assert calls == ["manager.exit"]
    assert not control._lock.locked()


async def test_cancelled_playwright_start_has_bounded_wait_and_late_cleanup(monkeypatch):
    pytest.importorskip("playwright.async_api")
    monkeypatch.setattr("anywhere_computer.browser_control._CLEANUP_WAIT_SECONDS", 0.01)
    reached = asyncio.Event()
    release = asyncio.Event()
    stopped = asyncio.Event()

    class Driver:
        async def stop(self):
            stopped.set()

    class Manager:
        async def start(self):
            reached.set()
            await release.wait()
            return Driver()

    monkeypatch.setattr("playwright.async_api.async_playwright", Manager)
    control = BrowserControl()
    opening = asyncio.create_task(control.open(owner="owner-a"))
    await asyncio.wait_for(reached.wait(), 5)
    opening.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(opening, 1)
    assert not control._lock.locked()
    assert control.entries == {}
    release.set()
    await asyncio.wait_for(stopped.wait(), 5)


@pytest.mark.parametrize("second_cancel_at", ["browser_close", "driver_stop"])
async def test_repeated_cancellation_finishes_browser_open_cleanup(
    monkeypatch, second_cancel_at,
):
    pytest.importorskip("playwright.async_api")
    context_reached = asyncio.Event()
    cleanup_reached = asyncio.Event()
    release_cleanup = asyncio.Event()
    calls = []

    class Browser:
        async def new_context(self, **_kwargs):
            context_reached.set()
            await asyncio.Future()

        async def close(self):
            calls.append("browser.close.started")
            if second_cancel_at == "browser_close":
                cleanup_reached.set()
                await release_cleanup.wait()
            calls.append("browser.close.finished")

    class Chromium:
        async def launch(self, **_kwargs):
            return Browser()

    class Driver:
        chromium = Chromium()

        async def start(self):
            return self

        async def stop(self):
            calls.append("driver.stop.started")
            if second_cancel_at == "driver_stop":
                cleanup_reached.set()
                await release_cleanup.wait()
            calls.append("driver.stop.finished")

    driver = Driver()
    monkeypatch.setattr("playwright.async_api.async_playwright", lambda: driver)
    control = BrowserControl()
    opening = asyncio.create_task(control.open(owner="owner-a"))
    await asyncio.wait_for(context_reached.wait(), 5)
    opening.cancel()
    await asyncio.wait_for(cleanup_reached.wait(), 5)
    opening.cancel()
    release_cleanup.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(opening, 5)
    assert calls == ["browser.close.started", "browser.close.finished",
                     "driver.stop.started", "driver.stop.finished"]
    assert control.entries == {}


async def test_status_reports_closed_page_as_ended(tmp_path):
    pytest.importorskip("playwright.async_api")
    engine = Engine(tmp_path / "state")
    engine.browser.channel = "chrome"
    try:
        opened = await engine.browser.open(owner="owner-a")
        await engine.browser.entries[opened["session_id"]].page.close()
        details = engine.status(owner="owner-a")["update_blocker_details"]
        assert details == [{
            "resource": "browser_session", "id": opened["session_id"],
            "state": "ended", "stop_tool": "browser_close",
            "tab_id": opened["tab_id"], "stop_available": True,
        }]
    finally:
        await engine.close()


async def test_engine_browser_tools_are_owned_and_block_update(tmp_path, local_page, monkeypatch):
    pytest.importorskip("playwright.async_api")
    engine = Engine(tmp_path / "state")
    engine.browser.channel = "chrome"

    async def call(tool, arguments, peer, *, force_pending=False):
        operation_id = uuid.uuid4().hex
        request = Request(operation_id=operation_id, tool=tool, arguments=arguments)
        if force_pending:
            with monkeypatch.context() as patch:
                patch.setattr(engine_module, "OBSERVER_WAIT_SECONDS", 0.001)
                reply = await engine.execute(request, peer=peer)
        else:
            reply = await engine.execute(request, peer=peer)
        async with asyncio.timeout(90):
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
        opened = await call("browser_open", {}, "owner-a", force_pending=True)
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
        filled = await call("browser_fill", {**ids, "selector": "#entry",
                                             "value": "日本語 ✅"}, "owner-a")
        assert filled.state == "completed", filled.error
        assert "日本語 ✅" in filled.data["text"]
        clicked = await call("browser_click", {**ids, "selector": "#go"}, "owner-a")
        assert clicked.state == "completed", clicked.error
        assert "日本語 ✅ clicked" in clicked.data["text"]
        async def lost_snapshot(_entry):
            raise RuntimeError("observation lost after click")

        with monkeypatch.context() as patch:
            patch.setattr(engine.browser, "_snapshot", lost_snapshot)
            uncertain = await call("browser_click", {**ids, "selector": "#go"}, "owner-a")
        assert uncertain.state == "unknown"
        assert uncertain.data["error_code"] == "browser_action_outcome_unknown"
        observed = await call("browser_observe", ids, "owner-a")
        assert observed.state == "completed"
        assert "日本語 ✅ clicked clicked" in observed.data["text"]
        assert "clicked clicked clicked" not in observed.data["text"]
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


async def test_browser_click_fill_exact_target_owner_and_preflight(local_page):
    pytest.importorskip("playwright.async_api")
    control = BrowserControl(channel="chrome")
    try:
        opened = await control.open(owner="owner-a")
        ids = {"session_id": opened["session_id"], "tab_id": opened["tab_id"]}
        await control.navigate(BrowserNavigate(**ids, url=local_page), owner="owner-a")
        fill = BrowserFill(**ids, selector="#entry", value="日本語 ✅")
        with pytest.raises(ValueError, match="unavailable"):
            await control.fill(fill, owner="owner-b")
        with pytest.raises(ValueError, match="exactly one"):
            await control.click(BrowserClick(**ids, selector="p"), owner="owner-a")
        with pytest.raises(ValueError, match="editable"):
            await control.fill(BrowserFill(**ids, selector="#go", value="no"), owner="owner-a")
        filled = await control.fill(fill, owner="owner-a")
        assert "日本語 ✅" in filled["text"]
        assert filled["value_verified"] is True
        clicked = await control.click(BrowserClick(**ids, selector="#go"), owner="owner-a")
        assert "日本語 ✅ clicked" in clicked["text"]
        await control.stop(BrowserSession(**ids), owner="owner-a")
        with pytest.raises(ValueError, match="unavailable"):
            await control.click(BrowserClick(**ids, selector="#go"), owner="owner-a")
    finally:
        await control.close()
