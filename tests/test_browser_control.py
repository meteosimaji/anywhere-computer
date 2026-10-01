"""Real local browser navigation, session isolation and owner binding."""

import asyncio
import base64
import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable

import pytest

from anywhere_computer import browser_control as browser_control_module
from anywhere_computer import engine as engine_module
from anywhere_computer.browser_control import (
    _CLEANUP_WAIT_SECONDS,
    BrowserActionUnknown,
    BrowserControl,
    BrowserNavigationUnknown,
    BrowserStartupUnavailable,
    _network_url,
)
from anywhere_computer.engine import Engine
from anywhere_computer.models import (
    BrowserClick,
    BrowserConsole,
    BrowserDownload,
    BrowserDrag,
    BrowserFileUpload,
    BrowserFill,
    BrowserHover,
    BrowserKey,
    BrowserNavigate,
    BrowserNetwork,
    BrowserObserve,
    BrowserResearch,
    BrowserScroll,
    BrowserSelect,
    BrowserSession,
    BrowserSource,
    BrowserTarget,
    Reply,
    Request,
)


async def _close_browser_control(control: BrowserControl) -> None:
    """Wait for owned cleanup even after its shorter caller receipt expires."""
    entries = list(control.entries.values())
    try:
        await control.close()
    except BrowserActionUnknown:
        assert entries
        assert all(entry.tabs.closing and entry.tabs.close_task is not None
                   for entry in entries)
    finally:
        # Await the real retained task: errors and unfinished cleanup still fail.
        for entry in entries:
            task = entry.tabs.close_task
            assert task is not None
            await asyncio.wait_for(asyncio.shield(task), 4 * _CLEANUP_WAIT_SECONDS)
            assert not entry.browser.is_connected()
        assert not control.entries


async def _tracked_fixture_client(
    handler: Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]],
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
    clients: set[asyncio.StreamWriter],
) -> None:
    clients.add(writer)
    try:
        await handler(reader, writer)
    except asyncio.IncompleteReadError:
        # Chrome can open a connection without sending complete request headers.
        pass
    finally:
        clients.discard(writer)
        writer.close()


async def _close_fixture_server(
    server: asyncio.Server, clients: set[asyncio.StreamWriter],
) -> None:
    server.close()
    for writer in tuple(clients):
        writer.close()
    await asyncio.wait_for(server.wait_closed(), 10)


async def test_fixture_server_closes_idle_accepted_client():
    clients: set[asyncio.StreamWriter] = set()
    started = asyncio.Event()

    async def read_headers(reader: asyncio.StreamReader, _writer: asyncio.StreamWriter) -> None:
        started.set()
        await reader.readuntil(b"\r\n\r\n")

    server = await asyncio.start_server(
        lambda reader, writer: _tracked_fixture_client(read_headers, reader, writer, clients),
        "127.0.0.1", 0)
    _reader, writer = await asyncio.open_connection(
        "127.0.0.1", server.sockets[0].getsockname()[1])
    try:
        await asyncio.wait_for(started.wait(), 1)
        assert clients  # The handler is blocked on an incomplete request.
        await asyncio.wait_for(_close_fixture_server(server, clients), 1)
        assert not clients
    finally:
        writer.close()
        await writer.wait_closed()


@pytest.mark.parametrize(
    ("platform", "requested_channel", "expected_channel"),
    [("win32", "auto", "msedge"), ("darwin", "auto", "chrome"),
     ("win32", "chrome", "chrome")],
)
def test_isolated_browser_channel_uses_installed_windows_default(
    monkeypatch, platform, requested_channel, expected_channel,
):
    monkeypatch.setattr(browser_control_module.sys, "platform", platform)
    assert BrowserControl(channel=requested_channel).channel == expected_channel


def test_network_url_omits_secrets_and_preserves_ipv6_authority():
    result = _network_url("https://user:password@[::1]:8443/page?token=private&mode=fast#part")
    assert result["route"] == "https://[::1]:8443/page"
    assert result["query_keys"] == ["token", "mode"]
    assert "private" not in str(result) and "password" not in str(result)


async def test_missing_isolated_browser_reports_pre_dispatch_failure(tmp_path, monkeypatch):
    pytest.importorskip("playwright.async_api")
    channels = []
    stopped = []

    class Chromium:
        async def launch(self, **kwargs):
            channels.append(kwargs["channel"])
            raise OSError("Executable is not installed at a private path")

    class Driver:
        chromium = Chromium()

        async def start(self):
            return self

        async def stop(self):
            stopped.append(True)

    monkeypatch.setattr(browser_control_module.sys, "platform", "win32")
    monkeypatch.setattr("playwright.async_api.async_playwright", Driver)
    engine = Engine(tmp_path / "state")
    try:
        reply = await engine.execute(Request(
            operation_id=uuid.uuid4().hex, tool="browser_open", arguments={},
        ), peer="owner-a")
        assert reply.state == "failed"
        assert reply.data["error_code"] == "browser_startup_unavailable"
        assert reply.data["dispatched"] is False
        assert "private path" not in str(reply)
        assert channels == ["msedge"]
        assert stopped == [True]
        assert engine.browser.entries == {}
    finally:
        await engine.close()


async def test_missing_playwright_driver_reports_pre_dispatch_failure(tmp_path, monkeypatch):
    pytest.importorskip("playwright.async_api")

    class Manager:
        async def start(self):
            raise FileNotFoundError("private Playwright driver path")

    monkeypatch.setattr("playwright.async_api.async_playwright", Manager)
    engine = Engine(tmp_path / "state")
    try:
        reply = await engine.execute(Request(
            operation_id=uuid.uuid4().hex, tool="browser_open", arguments={},
        ), peer="owner-a")
        assert reply.state == "failed"
        assert reply.data["error_code"] == "browser_startup_unavailable"
        assert reply.data["dispatched"] is False
        assert "private Playwright driver path" not in str(reply)
        assert engine.browser.entries == {}
    finally:
        await engine.close()


@pytest.fixture
async def local_page():
    clients: set[asyncio.StreamWriter] = set()

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

    server = await asyncio.start_server(
        lambda reader, writer: _tracked_fixture_client(serve, reader, writer, clients),
        "127.0.0.1", 0)
    try:
        yield f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/fixture"
    finally:
        await _close_fixture_server(server, clients)


@pytest.fixture
async def file_site():
    clients: set[asyncio.StreamWriter] = set()
    payload = b"browser download verified 42\n"

    async def serve(reader, writer):
        request = await reader.readuntil(b"\r\n\r\n")
        if request.startswith(b"GET /payload "):
            body = payload
            headers = (b"HTTP/1.1 200 OK\r\nContent-Type: application/octet-stream\r\n"
                       b"Content-Disposition: attachment; filename=sample.bin\r\n")
        else:
            body = (b"<html><head><title>Source fixture</title>"
                    b"<meta property='og:site_name' content='Example publisher'>"
                    b"<meta property='article:published_time' content='2026-09-28'>"
                    b"<link rel='canonical' href='/canonical'></head><body>"
                    b"<h1>Verified page heading</h1><label for='file'>Choose file</label>"
                    b"<input id='file' type='file' onchange=\"document.querySelector('#name')"
                    b".textContent=this.files[0].name\"><p id='name'></p>"
                    b"<a href='/payload'>Get file</a></body></html>")
            headers = b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n"
        writer.write(headers + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
                     + body)
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(
        lambda reader, writer: _tracked_fixture_client(serve, reader, writer, clients),
        "127.0.0.1", 0)
    try:
        yield f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/", payload
    finally:
        await _close_fixture_server(server, clients)


@pytest.fixture
async def navigation_site():
    clients: set[asyncio.StreamWriter] = set()
    requests = []

    async def serve(reader, writer):
        try:
            request = await reader.readuntil(b"\r\n\r\n")
        except asyncio.IncompleteReadError:
            writer.close()
            return
        path = request.split(b" ", 2)[1].decode()
        if path != "/favicon.ico":
            requests.append(path)
        page = b"Second document" if path == "/next" else b"First document"
        body = (b"<html><head><title>Navigation fixture</title></head><body><p id='content'>"
                + page + b"</p><a id='next' href='/next'>Next</a>"
                + b"<button id='spa' onclick=\"history.pushState({}, '', '/spa');"
                + b"document.querySelector('#content').textContent='SPA document'\">SPA</button>"
                + b"</body></html>")
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n"
                     + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
                     + body)
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(
        lambda reader, writer: _tracked_fixture_client(serve, reader, writer, clients),
        "127.0.0.1", 0)
    try:
        yield f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}", requests
    finally:
        await _close_fixture_server(server, clients)


async def test_browser_navigation_continuity_across_documents_and_spa(navigation_site):
    pytest.importorskip("playwright.async_api")
    base, requests = navigation_site
    control = BrowserControl(channel="chrome")
    try:
        opened = await control.open(owner="owner-a")
        ids = {"session_id": opened["session_id"], "tab_id": opened["tab_id"]}
        first = await control.navigate(BrowserNavigate(**ids, url=base + "/first"),
                                       owner="owner-a")
        assert first["last_navigation"] == {
            "requested_url": base + "/first", "outcome": "confirmed",
            "observed_url": base + "/first",
        }
        second = await control.click(BrowserClick(**ids, selector="#next"), owner="owner-a")
        assert second["url"] == base + "/next"
        assert "Second document" in second["text"]
        spa = await control.click(BrowserClick(**ids, selector="#spa"), owner="owner-a")
        assert spa["url"] == base + "/spa"
        assert "SPA document" in spa["text"]
        observed = await control.observe(BrowserSession(**ids), owner="owner-a")
        assert observed["url"] == base + "/spa"
        assert observed["last_navigation"] == {
            "requested_url": base + "/first", "outcome": "confirmed",
            "observed_url": base + "/spa",
        }
        assert requests == ["/first", "/next"]
    finally:
        await _close_browser_control(control)


async def test_ambiguous_navigation_reconciles_observation_without_replay(
    navigation_site, monkeypatch,
):
    pytest.importorskip("playwright.async_api")
    base, requests = navigation_site
    control = BrowserControl(channel="chrome")
    try:
        opened = await control.open(owner="owner-a")
        ids = {"session_id": opened["session_id"], "tab_id": opened["tab_id"]}
        entry = control.entries[ids["session_id"]]
        original_goto = entry.page.goto

        async def lost_completion(*args, **kwargs):
            await original_goto(*args, **kwargs)
            raise TimeoutError("completion acknowledgement lost")

        with monkeypatch.context() as patch:
            patch.setattr(entry.page, "goto", lost_completion)
            with pytest.raises(BrowserNavigationUnknown, match="observe the same tab"):
                await control.navigate(BrowserNavigate(**ids, url=base + "/next"),
                                       owner="owner-a")
        assert requests == ["/next"]
        with pytest.raises(ValueError, match="unavailable"):
            await control.observe(BrowserSession(**ids), owner="owner-b")
        observed = await control.observe(BrowserSession(**ids), owner="owner-a")
        assert observed["url"] == base + "/next"
        assert "Second document" in observed["text"]
        assert observed["last_navigation"] == {
            "requested_url": base + "/next", "outcome": "unconfirmed",
            "observed_url": base + "/next",
        }
        assert requests == ["/next"]
    finally:
        await _close_browser_control(control)


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
        await _close_browser_control(control)


@pytest.mark.parametrize("close_target", ["page", "browser"])
@pytest.mark.parametrize("close_delay", [0, 5.1], ids=["normal", "slow-browser-close"])
async def test_dead_browser_session_releases_capacity_without_touching_live_owners(
    close_target, close_delay, monkeypatch,
):
    pytest.importorskip("playwright.async_api")
    control = BrowserControl(channel="chrome")
    live_entry = None
    browser_close = None
    try:
        opened = [await control.open(owner=f"owner-{index}") for index in range(4)]
        live_entry = control.entries[opened[2]["session_id"]]
        browser_close = live_entry.browser.close
        live_close_calls = []

        async def delayed_close():
            live_close_calls.append(True)
            await asyncio.sleep(close_delay)
            await browser_close()

        monkeypatch.setattr(live_entry.browser, "close", delayed_close)
        ended = opened[0]
        entry = control.entries[ended["session_id"]]
        if close_target == "page":
            # Make a real owned cleanup outlive its five-second receipt deadline.
            # The session must retain capacity until its driver actually stops.
            ended_browser_close = entry.browser.close

            async def delayed_ended_close():
                await asyncio.sleep(close_delay)
                await ended_browser_close()

            monkeypatch.setattr(entry.browser, "close", delayed_ended_close)
            await entry.page.close()
        else:
            await entry.browser.close()
        with pytest.raises(ValueError, match="ended"):
            await control.observe(BrowserSession(session_id=ended["session_id"],
                                                 tab_id=ended["tab_id"]), owner="owner-0")

        try:
            replacement = await control.open(owner="owner-new")
        except ValueError as error:
            assert str(error) == "Browser session capacity reached"
            assert control.entries.get(ended["session_id"]) is entry
            assert entry.tabs.closing and entry.tabs.close_task is not None
            assert len(control.entries) == 4 and not live_close_calls
            # The failed open did not launch anything. Await the same retained
            # task without cancelling it, then retry only after closure is proven.
            await asyncio.wait_for(asyncio.shield(entry.tabs.close_task),
                                   4 * _CLEANUP_WAIT_SECONDS)
            assert ended["session_id"] not in control.entries
            assert not entry.browser.is_connected()
            replacement = await control.open(owner="owner-new")
        assert not live_close_calls
        assert ended["session_id"] not in control.entries
        assert len(control.entries) == 4
        assert replacement["session_id"] in control.entries
        for index, session in enumerate(opened[1:], start=1):
            observed = await control.observe(BrowserSession(
                session_id=session["session_id"], tab_id=session["tab_id"]),
                                             owner=f"owner-{index}")
            assert observed["session_id"] == session["session_id"]
    finally:
        # The delay detects accidental live-owner cleanup during capacity recovery.
        # Restore it before intentional fixture teardown so it does not consume
        # the runtime's real browser-close deadline on slower Windows runners.
        if live_entry is not None and browser_close is not None:
            monkeypatch.setattr(live_entry.browser, "close", browser_close)
        await _close_browser_control(control)


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


async def test_failed_playwright_start_hides_internal_error_and_unlocks(monkeypatch):
    pytest.importorskip("playwright.async_api")

    class Manager:
        async def start(self):
            raise FileNotFoundError("Playwright driver missing")

        async def __aexit__(self, *_args):
            raise AttributeError("_output")

    monkeypatch.setattr("playwright.async_api.async_playwright", Manager)
    control = BrowserControl()
    with pytest.raises(BrowserStartupUnavailable, match="runtime could not start"):
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
    with pytest.raises(BrowserStartupUnavailable, match="runtime could not start"):
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
        filled = await call("browser_fill", {**ids, "role": "textbox",
                                             "snapshot_id": moved.data["snapshot_id"],
                                             "value": "日本語 ✅"}, "owner-a")
        assert filled.state == "completed", filled.error
        assert "日本語 ✅" in filled.data["text"]
        clicked = await call("browser_click", {**ids, "role": "button", "name": "Go",
                                               "snapshot_id": filled.data["snapshot_id"]},
                             "owner-a")
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
        await _close_browser_control(control)


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
        await _close_browser_control(control)


async def test_semantic_browser_targets_use_observed_snapshot_and_exact_role(local_page):
    pytest.importorskip("playwright.async_api")
    control = BrowserControl(channel="chrome")
    try:
        opened = await control.open(owner="owner-a")
        ids = {"session_id": opened["session_id"], "tab_id": opened["tab_id"]}
        observed = await control.navigate(BrowserNavigate(**ids, url=local_page),
                                          owner="owner-a")
        assert 'button "Go"' in observed["semantic_tree"]
        assert "textbox" in observed["semantic_tree"]
        with pytest.raises(ValueError, match="exactly one"):
            BrowserClick(**ids, selector="#go", role="button")
        with pytest.raises(ValueError, match="snapshot_id"):
            BrowserClick(**ids, role="button", name="Go")

        filled = await control.fill(BrowserFill(**ids, role="textbox",
            snapshot_id=observed["snapshot_id"], value="semantic input"), owner="owner-a")
        assert filled["value_verified"] is True
        with pytest.raises(ValueError, match="stale"):
            await control.click(BrowserClick(**ids, role="button", name="Go",
                snapshot_id=observed["snapshot_id"]), owner="owner-a")
        clicked = await control.click(BrowserClick(**ids, role="button", name="Go",
            snapshot_id=filled["snapshot_id"]), owner="owner-a")
        assert "semantic input clicked" in clicked["text"]

        entry = control.entries[ids["session_id"]]
        await entry.page.evaluate("""() => document.querySelector('#entry').
            insertAdjacentHTML('beforebegin', '<label for=entry>Message</label>')""")
        labeled = await control.observe(BrowserSession(**ids), owner="owner-a")
        by_label = await control.fill(BrowserFill(**ids, label="Message",
            snapshot_id=labeled["snapshot_id"], value="by label"), owner="owner-a")
        assert by_label["value_verified"] is True
        assert "by label" in by_label["text"]
    finally:
        await _close_browser_control(control)


async def test_html_labels_and_rendered_viewport_reach_native_mcp_image(local_page):
    pytest.importorskip("playwright.async_api")
    from mcp.types import CallToolResult, ImageContent

    from anywhere_computer.mcp_server import _reply_result

    control = BrowserControl(channel="chrome")
    try:
        opened = await control.open(owner="owner-a")
        ids = {"session_id": opened["session_id"], "tab_id": opened["tab_id"]}
        await control.navigate(BrowserNavigate(**ids, url=local_page), owner="owner-a")
        entry = control.entries[ids["session_id"]]
        await entry.page.evaluate("""() => {
            document.body.insertAdjacentHTML('beforeend', `
                <label for="email">Contact<br>email</label><input id="email" type="email">
                <span id="search-name">Search<br>the site</span>
                <input id="search" aria-labelledby="search-name" placeholder="Find things">
                <input id="direct" aria-label="Direct&#10;label">
                <input id="secret" type="password" value="private-test-value">
                <button>Send<br>now</button>
            `);
        }""")
        observed = await control.observe(BrowserObserve(**ids, include_image=True),
                                         owner="owner-a")
        controls = observed["form_controls"]
        assert isinstance(controls, list)
        assert {row["id"]: row["label"] for row in controls if "id" in row}["email"] == (
            "Contact email")
        assert {row["id"]: row["label_source"] for row in controls if "id" in row}[
            "email"] == "html-label"
        assert {row["id"]: row["label"] for row in controls if "id" in row}["search"] == (
            "Search the site")
        assert {row["id"]: row["label"] for row in controls if "id" in row}["direct"] == (
            "Direct label")
        email = next(row for row in controls if row.get("id") == "email")
        assert email["in_viewport"] is True
        assert email["box"]["width"] > 0
        assert 0 <= email["box"]["x"] < 1280
        assert 0 <= email["box"]["y"] < 720
        assert "private-test-value" not in json.dumps(controls)
        assert observed["visual"]["kind"] == "rendered_viewport"
        assert observed["visual"]["coordinate_unit"] == "css_px"
        assert observed["visual"]["capture_mode"] == "sequential"
        assert observed["visual"]["width"] == 1280
        assert observed["visual"]["height"] == 720
        picture = observed["content"][0]
        assert picture["mimeType"] == "image/jpeg"
        assert base64.b64decode(picture["data"]).startswith(b"\xff\xd8\xff")
        reply = Reply(operation_id="f" * 32, state="completed", data=observed)
        wire = _reply_result("browser_observe", reply)
        validated = CallToolResult.model_validate(wire)
        assert isinstance(validated.content[1], ImageContent)
        assert picture["data"] not in validated.content[0].text
        assert picture["data"] not in json.dumps(wire["structuredContent"])
        assert wire["structuredContent"]["data"]["content"][0]["bytes"] == (
            observed["visual"]["bytes"])
    finally:
        await _close_browser_control(control)


@pytest.mark.parametrize("close_delay", [0, 5.1], ids=["normal", "slow-browser-close"])
async def test_current_dom_and_network_metadata_are_bounded_and_owner_scoped(
    local_page, monkeypatch, close_delay,
):
    pytest.importorskip("playwright.async_api")
    control = BrowserControl(channel="chrome")
    try:
        opened = await control.open(owner="owner-a")
        ids = {"session_id": opened["session_id"], "tab_id": opened["tab_id"]}
        entry = control.entries[ids["session_id"]]
        browser_close = entry.browser.close

        async def delayed_close():
            await asyncio.sleep(close_delay)
            await browser_close()

        monkeypatch.setattr(entry.browser, "close", delayed_close)
        await control.navigate(BrowserNavigate(**ids, url=local_page +
            "?token=private-value&search=example"), owner="owner-a")
        source = await control.source(BrowserSource(**ids), owner="owner-a")
        assert source["source_kind"] == "current_dom_outer_html"
        assert "<button" in source["html"]
        assert source["truncated"] is False
        button = await control.source(BrowserSource(**ids, selector="#go"), owner="owner-a")
        assert button["html"].startswith("<button")
        with pytest.raises(ValueError, match="exactly one"):
            await control.source(BrowserSource(**ids, selector="p"), owner="owner-a")

        network = await control.network(BrowserNetwork(**ids), owner="owner-a")
        response = next(row for row in network["events"] if row["event"] == "response")
        assert response["status"] == 200
        assert response["route"].endswith("/fixture")
        assert response["query_keys"] == ["token", "search"]
        assert "private-value" not in json.dumps(network)
        assert network["next_id"] == response["id"]
        empty = await control.network(BrowserNetwork(**ids, after_id=network["latest_id"]),
                                      owner="owner-a")
        assert empty["events"] == []
        with pytest.raises(ValueError, match="unavailable"):
            await control.network(BrowserNetwork(**ids), owner="owner-b")
    finally:
        await _close_browser_control(control)


async def test_key_and_drag_recheck_observation_and_target(local_page):
    pytest.importorskip("playwright.async_api")
    control = BrowserControl(channel="chrome")
    try:
        opened = await control.open(owner="owner-a")
        ids = {"session_id": opened["session_id"], "tab_id": opened["tab_id"]}
        observed = await control.navigate(BrowserNavigate(**ids, url=local_page), owner="owner-a")
        entry = control.entries[ids["session_id"]]
        await entry.page.evaluate("""() => {
            const input = document.querySelector('#entry');
            input.addEventListener('keydown', event => {
                if (event.key === 'Enter') {
                    document.querySelector('#result').textContent = 'entered';
                }
            });
            const source = document.createElement('div');
            source.id = 'source';
            source.draggable = true;
            source.textContent = 'Move';
            source.addEventListener('dragstart', event => {
                event.dataTransfer.setData('text/plain', 'moved');
            });
            const target = document.createElement('div');
            target.id = 'target';
            target.textContent = 'Drop';
            target.addEventListener('dragover', event => event.preventDefault());
            target.addEventListener('drop', event => {
                event.preventDefault();
                target.textContent = event.dataTransfer.getData('text/plain');
            });
            document.body.append(source, target);
        }""")
        keyed = await control.key(BrowserKey(**ids, role="textbox", key="Enter",
                                             snapshot_id=observed["snapshot_id"]), owner="owner-a")
        assert "entered" in keyed["text"]
        with pytest.raises(ValueError, match="stale"):
            await control.drag(BrowserDrag(**ids, snapshot_id=observed["snapshot_id"],
                source=BrowserTarget(selector="#source"),
                target=BrowserTarget(selector="#target")), owner="owner-a")
        dragged = await control.drag(BrowserDrag(**ids, snapshot_id=keyed["snapshot_id"],
            source=BrowserTarget(selector="#source"),
            target=BrowserTarget(selector="#target")), owner="owner-a")
        assert await entry.page.locator("#target").inner_text() == "moved"
        assert dragged["snapshot_id"] != keyed["snapshot_id"]
        with pytest.raises(ValueError, match="unavailable"):
            await control.key(BrowserKey(**ids, selector="#entry", key="Tab"), owner="owner-b")
    finally:
        await _close_browser_control(control)


async def test_console_hover_select_and_scroll_use_owned_observation(local_page):
    pytest.importorskip("playwright.async_api")
    control = BrowserControl(channel="chrome")
    try:
        opened = await control.open(owner="owner-a")
        ids = {"session_id": opened["session_id"], "tab_id": opened["tab_id"]}
        await control.navigate(BrowserNavigate(**ids, url=local_page), owner="owner-a")
        entry = control.entries[ids["session_id"]]
        async with entry.page.expect_event("pageerror"):
            await entry.page.evaluate("""() => {
                console.warn('fixture warning');
                setTimeout(() => { throw new Error('fixture page error'); }, 0);
            }""")
        console = await control.console(BrowserConsole(**ids), owner="owner-a")
        assert any(row["text"] == "fixture warning" and row["type"] == "warning"
                   for row in console["events"])
        assert any("fixture page error" in row["text"] and row["event"] == "page_error"
                   for row in console["events"])
        class RedactedMessage:
            text = "diagnostic message"
            type = "info"
            location = {"url": "https://user:password@example.test/app?token=secret#part",
                        "lineNumber": 3, "columnNumber": 7}

        control._record_console(entry, RedactedMessage())
        redacted = await control.console(BrowserConsole(
            **ids, after_id=console["latest_id"]), owner="owner-a")
        assert redacted["events"][0]["source"]["route"] == "https://example.test/app"
        assert redacted["events"][0]["source"]["query_keys"] == ["token"]
        assert "secret" not in json.dumps(redacted)
        console = redacted
        assert (await control.console(BrowserConsole(
            **ids, after_id=console["latest_id"]), owner="owner-a"))["events"] == []
        with pytest.raises(ValueError, match="unavailable"):
            await control.console(BrowserConsole(**ids), owner="owner-b")

        await entry.page.evaluate("""() => {
            const button = document.querySelector('#go');
            button.addEventListener('mouseover', () => {
                document.querySelector('#result').textContent = 'hovered';
            });
            const label = document.createElement('label');
            label.textContent = 'Plan';
            label.htmlFor = 'plan';
            const select = document.createElement('select');
            select.id = 'plan';
            select.innerHTML = '<option value="basic">Basic</option>'
                + '<option value="pro">Pro</option>';
            select.addEventListener('change', () => {
                document.querySelector('#result').textContent = select.value;
            });
            const pane = document.createElement('div');
            pane.id = 'scrollpane';
            pane.style.cssText = 'height:60px;width:180px;overflow:auto';
            pane.innerHTML = '<div style="height:600px">Scrollable</div>';
            document.body.append(label, select, pane);
        }""")
        observed = await control.observe(BrowserSession(**ids), owner="owner-a")
        hovered = await control.hover(BrowserHover(
            **ids, target=BrowserTarget(role="button", name="Go"),
            snapshot_id=observed["snapshot_id"]), owner="owner-a")
        assert "hovered" in hovered["text"]
        with pytest.raises(ValueError, match="stale"):
            await control.select(BrowserSelect(
                **ids, target=BrowserTarget(label="Plan"),
                snapshot_id=observed["snapshot_id"], label="Pro"), owner="owner-a")
        await entry.page.evaluate("""() => {
            document.querySelector('#plan').insertAdjacentHTML(
                'beforeend', '<option value="other">Pro</option>');
        }""")
        with pytest.raises(ValueError, match="exactly one"):
            await control.select(BrowserSelect(
                **ids, target=BrowserTarget(label="Plan"),
                snapshot_id=hovered["snapshot_id"], label="Pro"), owner="owner-a")
        assert await entry.page.locator("#plan").input_value() == "basic"
        await entry.page.locator("#plan option[value=other]").evaluate("el => el.remove()")
        selected = await control.select(BrowserSelect(
            **ids, target=BrowserTarget(label="Plan"),
            snapshot_id=hovered["snapshot_id"], label="Pro"), owner="owner-a")
        assert selected["selection_verified"] is True
        assert selected["selected_option"] == {"value": "pro", "label": "Pro"}
        assert "pro" in selected["text"]
        scrolled = await control.scroll(BrowserScroll(
            **ids, target=BrowserTarget(selector="#scrollpane"),
            snapshot_id=selected["snapshot_id"], delta_y=180), owner="owner-a")
        assert scrolled["scroll"]["changed"] is True
        assert scrolled["scroll"]["after"]["y"] > 0
        assert scrolled["snapshot_id"] != selected["snapshot_id"]
    finally:
        await _close_browser_control(control)


async def test_file_input_and_download_are_bounded_and_do_not_replace(
    tmp_path, file_site,
):
    pytest.importorskip("playwright.async_api")
    url, payload = file_site
    source = tmp_path / "upload.txt"
    source.write_bytes(b"upload verified")
    destination = tmp_path / "download.bin"
    control = BrowserControl(channel="chrome")
    try:
        opened = await control.open(owner="owner-a")
        ids = {"session_id": opened["session_id"], "tab_id": opened["tab_id"]}
        observed = await control.navigate(BrowserNavigate(**ids, url=url), owner="owner-a")
        research = await control.research(BrowserResearch(**ids), owner="owner-a")
        assert research["publisher_claim"] == "Example publisher"
        assert research["published_claim"] == "2026-09-28"
        assert research["canonical"]["route"].endswith("/canonical")
        assert research["links"][0]["destination"]["route"].endswith("/payload")
        selected = await control.file_upload(BrowserFileUpload(
            **ids, target=BrowserTarget(label="Choose file"),
            snapshot_id=observed["snapshot_id"], path=str(source),
        ), owner="owner-a")
        assert selected["selected_file"] == {
            "name": "upload.txt", "bytes": len(source.read_bytes()),
            "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        }
        assert "upload.txt" in selected["text"]
        downloaded = await control.download(BrowserDownload(
            **ids, target=BrowserTarget(role="link", name="Get file"),
            snapshot_id=selected["snapshot_id"], path=str(destination),
        ), owner="owner-a")
        assert destination.read_bytes() == payload
        assert downloaded["download"]["sha256"] == hashlib.sha256(payload).hexdigest()
        assert downloaded["download"]["bytes"] == len(payload)
        with pytest.raises(ValueError, match="unused path"):
            await control.download(BrowserDownload(
                **ids, target=BrowserTarget(selector="a"),
                snapshot_id=downloaded["snapshot_id"], path=str(destination),
            ), owner="owner-a")
        with pytest.raises(ValueError, match="unavailable"):
            await control.file_upload(BrowserFileUpload(
                **ids, target=BrowserTarget(selector="#file"),
                snapshot_id=downloaded["snapshot_id"], path=str(source),
            ), owner="owner-b")
    finally:
        await _close_browser_control(control)


async def test_semantic_click_waits_for_observed_target_after_rerender(local_page):
    pytest.importorskip("playwright.async_api")
    control = BrowserControl(channel="chrome")
    try:
        opened = await control.open(owner="owner-a")
        ids = {"session_id": opened["session_id"], "tab_id": opened["tab_id"]}
        observed = await control.navigate(BrowserNavigate(**ids, url=local_page), owner="owner-a")
        assert 'button "Go"' in observed["semantic_tree"]
        entry = control.entries[ids["session_id"]]
        await entry.page.evaluate("""() => {
            const button = document.querySelector('#go');
            button.style.visibility = 'hidden';
            setTimeout(() => {
                const replacement = button.cloneNode(true);
                replacement.style.visibility = 'visible';
                button.replaceWith(replacement);
            }, 150);
        }""")
        clicked = await control.click(BrowserClick(**ids, role="button", name="Go",
            snapshot_id=observed["snapshot_id"]), owner="owner-a")
        assert clicked["text"].count("clicked") == 1
    finally:
        await _close_browser_control(control)


async def test_frame_scope_is_owned_observed_and_invalidated_on_same_url_reload():
    control = BrowserControl(channel="chrome")
    try:
        opened = await control.open(owner="frame-owner")
        ids = {"session_id": opened["session_id"], "tab_id": opened["tab_id"]}
        page = control.entries[ids["session_id"]].page
        parent = ('<title>Parent</title><button>Save</button>'
                  '<iframe name="editor" src="http://child.test/form"></iframe>'
                  '<iframe name="second" src="http://child.test/form"></iframe>')
        child = ('<title>Child</title><label>Name<input id="entry"></label>'
                 '<button onclick="document.querySelector(\'p\').textContent='
                 'document.querySelector(\'input\').value">Save</button><p>unchanged</p>')

        async def serve(route):
            await route.fulfill(content_type="text/html", body=(
                parent if route.request.url.startswith("http://parent.test") else child
            ))

        await page.route("http://**/*", serve)
        await control.navigate(BrowserNavigate(**ids, url="http://parent.test/"),
                               owner="frame-owner")
        for name in ("editor", "second"):
            await page.frame_locator(f'iframe[name="{name}"]').locator("p").wait_for()
        main = await control.observe(BrowserObserve(**ids), owner="frame-owner")
        frame_id = next(row["frame_id"] for row in main["frames"] if row["name"] == "editor")
        scoped = {**ids, "frame_id": frame_id}
        with pytest.raises(ValueError, match="stale"):
            await control.click(BrowserClick(**scoped, role="button", name="Save",
                                snapshot_id=main["snapshot_id"]), owner="frame-owner")
        observed = await control.observe(BrowserObserve(**scoped), owner="frame-owner")
        assert observed["title"] == "Child"
        assert observed["frame_id"] == frame_id
        assert observed["form_controls"][0]["label"] == "Name"
        with pytest.raises(ValueError, match="unavailable"):
            await control.observe(BrowserObserve(**scoped), owner="another-owner")
        filled = await control.fill(BrowserFill(**scoped, label="Name", value="frame-specific",
                                    snapshot_id=observed["snapshot_id"]), owner="frame-owner")
        assert filled["value_verified"] is True
        clicked = await control.click(BrowserClick(**scoped, role="button", name="Save",
                                      snapshot_id=filled["snapshot_id"]), owner="frame-owner")
        assert "frame-specific" in clicked["text"]
        assert await page.frame(name="second").locator("p").inner_text() == "unchanged"
        source = await control.source(BrowserSource(**scoped, selector="p"), owner="frame-owner")
        assert source["html"] == "<p>frame-specific</p>"
        assert (await control.research(BrowserResearch(**scoped),
                                      owner="frame-owner"))["title"] == "Child"
        await page.frame(name="editor").goto("http://child.test/form")
        with pytest.raises(ValueError, match="stale"):
            await control.click(BrowserClick(**scoped, role="button", name="Save",
                                snapshot_id=clicked["snapshot_id"]), owner="frame-owner")
        assert await page.frame(name="editor").locator("p").inner_text() == "unchanged"
        await page.locator('iframe[name="editor"]').evaluate("element => element.remove()")
        with pytest.raises(ValueError, match="frame unavailable"):
            await control.observe(BrowserObserve(**scoped), owner="frame-owner")
    finally:
        await _close_browser_control(control)


async def test_shadow_labels_match_semantic_actions_and_frame_images_keep_tab_coordinates():
    control = BrowserControl(channel="chrome")
    try:
        opened = await control.open(owner="shadow-owner")
        ids = {"session_id": opened["session_id"], "tab_id": opened["tab_id"]}
        page = control.entries[ids["session_id"]].page
        await page.set_content('<div id="host"></div>')
        await page.evaluate("""() => {
            const root = document.querySelector('#host').attachShadow({mode: 'open'});
            root.innerHTML = '<span id="label">Shadow name</span>'
                + '<input aria-labelledby="label"><button>Save shadow</button>';
            root.querySelector('button').onclick = () => {
                document.body.dataset.saved = root.querySelector('input').value;
            };
        }""")
        observed = await control.observe(BrowserObserve(**ids), owner="shadow-owner")
        controls = observed["form_controls"]
        assert any(row["label"] == "Shadow name" and row["in_shadow_dom"] for row in controls)
        filled = await control.fill(BrowserFill(**ids, label="Shadow name", value="visible",
                                    snapshot_id=observed["snapshot_id"]), owner="shadow-owner")
        assert filled["value_verified"] is True
        await control.click(BrowserClick(**ids, role="button", name="Save shadow",
                            snapshot_id=filled["snapshot_id"]), owner="shadow-owner")
        assert await page.evaluate("document.body.dataset.saved") == "visible"
        await page.evaluate("""() => {
            const frame = document.createElement('iframe');
            frame.srcdoc = '<label>Frame label<input></label>';
            document.body.append(frame);
        }""")
        await page.frames[1].locator("input").wait_for()
        main = await control.observe(BrowserObserve(**ids), owner="shadow-owner")
        child = await control.observe(BrowserObserve(
            **ids, frame_id=main["frames"][0]["frame_id"], include_image=True,
        ), owner="shadow-owner")
        assert child["form_controls"][0]["label"] == "Frame label"
        assert child["visual"]["scope"] == "tab_viewport"
        assert child["form_control_coordinate_space"] == "frame_viewport_css_pixels"
        assert base64.b64decode(child["content"][0]["data"]).startswith(b"\xff\xd8")
    finally:
        await _close_browser_control(control)


async def test_snapshot_rejects_main_reload_even_when_url_is_unchanged(local_page):
    control = BrowserControl(channel="chrome")
    try:
        opened = await control.open(owner="reload-owner")
        ids = {"session_id": opened["session_id"], "tab_id": opened["tab_id"]}
        observed = await control.navigate(BrowserNavigate(**ids, url=local_page),
                                          owner="reload-owner")
        page = control.entries[ids["session_id"]].page
        await page.reload()
        with pytest.raises(ValueError, match="stale"):
            await control.fill(BrowserFill(**ids, role="textbox", value="must not be entered",
                               snapshot_id=observed["snapshot_id"]), owner="reload-owner")
        assert await page.locator("#entry").input_value() == ""
    finally:
        await _close_browser_control(control)
