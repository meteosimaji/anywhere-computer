"""Disposable Chrome acceptance for owned tabs, popups and explicit page dialogs."""

import asyncio
import json
import time
import uuid

import pytest
from pydantic import ValidationError

from anywhere_computer import engine as engine_module
from anywhere_computer.browser_control import BrowserActionUnknown, BrowserControl
from anywhere_computer.engine import Engine
from anywhere_computer.models import (
    BrowserClick,
    BrowserDialogHandle,
    BrowserNavigate,
    BrowserObserve,
    BrowserSession,
    BrowserSessionId,
    BrowserSource,
    Reply,
    Request,
)


@pytest.fixture
async def dialog_site():
    async def serve(reader, writer):
        try:
            request = await reader.readuntil(b"\r\n\r\n")
        except asyncio.IncompleteReadError:
            writer.close()
            return
        cookie = next((line for line in request.split(b"\r\n")
                       if line.lower().startswith(b"cookie:")), b"")
        if request.startswith(b"GET /child-alert "):
            body = b"<html><body><script>alert('Popup early')</script>child ready</body></html>"
        else:
            body = b"""<html><head><title>Owned tabs fixture</title></head><body>
        <p id='cookie'>COOKIE</p><p id='result'>unchanged</p>
        <button id='popup' onclick="window.open('/child', '_blank')">Popup</button>
        <button id='popup-alert' onclick="window.open('/child-alert', '_blank')">
          Popup alert</button>
        <button id='confirm' onclick="document.querySelector('#result').textContent=
          confirm('Confirm fixture?') ? 'accepted once' : 'dismissed once'">Confirm</button>
        <button id='prompt' onclick="document.querySelector('#result').textContent=
          prompt('Prompt fixture?', 'default text')">Prompt</button>
        <button id='alert' onclick="alert('Alert fixture');
          document.querySelector('#result').textContent='alert continued'">Alert</button>
        </body></html>""".replace(b"COOKIE", cookie)
        headers = b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n"
        if request.startswith(b"GET /cookie "):
            headers += b"Set-Cookie: tab_shared=only-this-session; Path=/\r\n"
        writer.write(headers + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
                     + body)
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    try:
        yield f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
    finally:
        server.close()
        await server.wait_closed()


def tab_args(opened):
    return {"session_id": opened["session_id"], "tab_id": opened["tab_id"]}


async def test_tabs_popups_share_only_the_owned_context_and_close_exact_tab(dialog_site):
    pytest.importorskip("playwright.async_api")
    control = BrowserControl(channel="chrome")
    try:
        primary = tab_args(await control.open(owner="owner-a"))
        session = BrowserSessionId(session_id=primary["session_id"])
        await control.navigate(BrowserNavigate(**primary, url=dialog_site + "/cookie"),
                               owner="owner-a")
        with pytest.raises(ValueError, match="unavailable"):
            await control.tab_open(session, owner="owner-b")
        secondary = tab_args(await control.tab_open(session, owner="owner-a"))
        observed = await control.navigate(BrowserNavigate(**secondary, url=dialog_site),
                                          owner="owner-a")
        assert "tab_shared=only-this-session" in observed["text"]
        assert primary["tab_id"] != secondary["tab_id"]
        await control.click(BrowserClick(**primary, selector="#popup"), owner="owner-a")
        tabs = await control.tabs(session, owner="owner-a")
        popup = next(row for row in tabs["tabs"] if row["tab_id"] not in {
            primary["tab_id"], secondary["tab_id"],
        })
        popup_ids = {"session_id": primary["session_id"], "tab_id": popup["tab_id"]}
        observed = await control.observe(BrowserObserve(**popup_ids), owner="owner-a")
        assert "tab_shared=only-this-session" in observed["text"]
        other = tab_args(await control.open(owner="owner-a"))
        isolated = await control.navigate(BrowserNavigate(**other, url=dialog_site),
                                          owner="owner-a")
        assert "tab_shared=" not in isolated["text"]
        with pytest.raises(ValueError, match="unavailable"):
            await control.observe(BrowserObserve(session_id=other["session_id"],
                                                 tab_id=popup["tab_id"]), owner="owner-a")
        with pytest.raises(ValueError, match="unavailable"):
            await control.tabs(session, owner="owner-b")
        closed = await control.tab_close(BrowserSession(**primary), owner="owner-a")
        assert closed["session_closed"] is False
        with pytest.raises(ValueError, match="unavailable"):
            await control.observe(BrowserObserve(**primary), owner="owner-a")
        assert len((await control.tabs(session, owner="owner-a"))["tabs"]) == 2
        await control.tab_close(BrowserSession(**secondary), owner="owner-a")
        final = await control.tab_close(BrowserSession(**popup_ids), owner="owner-a")
        assert final["session_closed"] is True
        assert primary["session_id"] not in control.entries
        assert other["session_id"] in control.entries
    finally:
        await control.close()


@pytest.mark.parametrize(("button", "action", "prompt_text", "expected"), [
    ("confirm", "dismiss", None, "dismissed once"),
    ("confirm", "accept", None, "accepted once"),
    ("prompt", "accept", "typed fixture value", "typed fixture value"),
    ("alert", "accept", None, "alert continued"),
])
async def test_dialog_requires_observed_id_and_explicit_response(
    dialog_site, button, action, prompt_text, expected,
):
    pytest.importorskip("playwright.async_api")
    control = BrowserControl(channel="chrome")
    try:
        ids = tab_args(await control.open(owner="owner-a"))
        initial = await control.navigate(BrowserNavigate(**ids, url=dialog_site), owner="owner-a")
        started = time.monotonic()
        with pytest.raises(BrowserActionUnknown):
            await asyncio.wait_for(control.click(BrowserClick(**ids, selector="#" + button),
                                                 owner="owner-a"), 4)
        assert time.monotonic() - started < 4
        assert not control.entries[ids["session_id"]].lock.locked()
        pending = await control.dialogs(BrowserSession(**ids), owner="owner-a")
        dialog = pending["dialog"]
        assert dialog["type"] == button
        assert dialog["state"] == "pending"
        assert dialog["source_kind"] == "untrusted_page_dialog"
        snapshot = await asyncio.wait_for(control.observe(BrowserObserve(**ids, include_image=True),
                                                           owner="owner-a"), 1)
        assert snapshot["state"] == "dialog_open"
        assert snapshot["snapshot_id"] is None
        assert snapshot["dialog"] == dialog
        with pytest.raises(ValueError):
            await asyncio.wait_for(control.source(BrowserSource(**ids), owner="owner-a"), 1)
        with pytest.raises(ValueError, match="unavailable"):
            await control.dialogs(BrowserSession(**ids), owner="owner-b")
        response = BrowserDialogHandle(**ids, dialog_id=dialog["dialog_id"],
                                       action=action, prompt_text=prompt_text)
        with pytest.raises(ValueError, match="unavailable"):
            await control.dialog_handle(response, owner="owner-b")
        with pytest.raises(ValueError, match="unavailable"):
            await control.dialog_handle(BrowserDialogHandle(**ids, dialog_id="f" * 32,
                                                             action=action), owner="owner-a")
        if button != "prompt":
            with pytest.raises(ValueError, match="observed prompt"):
                await control.dialog_handle(BrowserDialogHandle(
                    **ids, dialog_id=dialog["dialog_id"], action="accept", prompt_text="wrong",
                ), owner="owner-a")
        assert (await control.dialogs(BrowserSession(**ids), owner="owner-a"))["dialog"] == dialog
        result = await asyncio.wait_for(control.dialog_handle(response, owner="owner-a"), 4)
        assert result["response_receipt"] == "confirmed"
        assert expected in result["observation"]["text"]
        with pytest.raises(ValueError, match="unavailable"):
            await control.dialog_handle(response, owner="owner-a")
        assert (await control.dialogs(BrowserSession(**ids), owner="owner-a"))["dialog"] is None
        with pytest.raises(ValueError, match="stale"):
            await control.click(BrowserClick(**ids, selector="#alert",
                                             snapshot_id=initial["snapshot_id"]), owner="owner-a")
    finally:
        await control.close()


async def test_popup_capacity_is_bounded_and_unknown_open_is_reconciled(dialog_site, monkeypatch):
    pytest.importorskip("playwright.async_api")
    control = BrowserControl(channel="chrome")
    try:
        ids = tab_args(await control.open(owner="owner-a"))
        session = BrowserSessionId(session_id=ids["session_id"])
        entry = control.entries[ids["session_id"]]
        new_page = entry.context.new_page

        async def lost_receipt():
            await new_page()
            raise TimeoutError("synthetic lost creation receipt")

        with monkeypatch.context() as patch:
            patch.setattr(entry.context, "new_page", lost_receipt)
            with pytest.raises(BrowserActionUnknown):
                await control.tab_open(session, owner="owner-a")
        rows = (await control.tabs(session, owner="owner-a"))["tabs"]
        assert len(rows) == 2
        for _ in range(6):
            await control.tab_open(session, owner="owner-a")
        with pytest.raises(ValueError, match="capacity"):
            await control.tab_open(session, owner="owner-a")
        await control.navigate(BrowserNavigate(**ids, url=dialog_site), owner="owner-a")
        await control.click(BrowserClick(**ids, selector="#popup"), owner="owner-a")
        await asyncio.gather(*entry.tabs.cleanup_tasks)
        listed = await control.tabs(session, owner="owner-a")
        assert len(listed["tabs"]) == 8
        assert listed["rejected_popups"] == 1
        assert len(entry.context.pages) == 8
        await control.stop(BrowserSession(**ids), owner="owner-a")
        assert not entry.browser.is_connected()
        assert not control.entries
    finally:
        await control.close()


def test_dialog_contract_rejects_prompt_on_dismiss():
    with pytest.raises(ValidationError, match="prompt_text requires"):
        BrowserDialogHandle(session_id="a" * 32, tab_id="b" * 32, dialog_id="c" * 32,
                            action="dismiss", prompt_text="invalid")


@pytest.mark.parametrize("force_running", [False, True])
async def test_engine_dialog_unknown_is_not_replayed_and_tabs_are_owner_bound(
    tmp_path, dialog_site, monkeypatch, force_running,
):
    pytest.importorskip("playwright.async_api")
    engine = Engine(tmp_path / "state")
    engine.browser.channel = "chrome"
    running = []

    async def execute(tool, arguments, *, operation_id=None, peer="owner-a"):
        request = Request(operation_id=operation_id or uuid.uuid4().hex,
                          tool=tool, arguments=arguments)
        with monkeypatch.context() as patch:
            if force_running:
                patch.setattr(engine_module, "OBSERVER_WAIT_SECONDS", 0)
            reply = await engine.execute(request, peer=peer)
        async with asyncio.timeout(90):
            while reply.state == "running":
                running.append(request.operation_id)
                recovered = await engine.execute(Request(operation_id=uuid.uuid4().hex,
                    tool="operations_get", arguments={"operation_id": request.operation_id}),
                    peer=peer)
                if recovered.state == "running":
                    await asyncio.sleep(0.02)
                    continue
                assert recovered.state == "completed", recovered
                reply = Reply.model_validate(recovered.data)
                if reply.state == "running":
                    await asyncio.sleep(0.02)
        return reply

    try:
        opened = await execute("browser_open", {})
        if force_running:
            assert running
        ids = tab_args(opened.data)
        await execute("browser_navigate", {**ids, "url": dialog_site})
        request_id = uuid.uuid4().hex
        first = await execute("browser_click", {**ids, "selector": "#confirm"},
                              operation_id=request_id)
        assert first.state == "unknown"
        assert first.data["error_code"] == "browser_action_outcome_unknown"
        replay = await execute("browser_click", {**ids, "selector": "#confirm"},
                               operation_id=request_id)
        assert replay.data == first.data
        pending = await execute("browser_dialogs", ids)
        assert pending.data["dialog"]["state"] == "pending"
        response = await execute("browser_dialog_handle", {
            **ids, "dialog_id": pending.data["dialog"]["dialog_id"], "action": "dismiss",
        })
        assert response.data["response_receipt"] == "confirmed"
        assert "dismissed once" in response.data["observation"]["text"]
        failed = await execute("browser_tabs", {"session_id": ids["session_id"]}, peer="owner-b")
        assert failed.state == "failed"
        assert "tabs" not in failed.data
        tabs = await execute("browser_tabs", {"session_id": ids["session_id"]})
        assert len(tabs.data["tabs"]) == 1
    finally:
        await engine.close()


async def test_lost_dialog_receipt_is_claimed_once_and_observation_recovers_closed_modal(
    dialog_site, monkeypatch,
):
    pytest.importorskip("playwright.async_api")
    control = BrowserControl(channel="chrome")
    try:
        ids = tab_args(await control.open(owner="owner-a"))
        await control.navigate(BrowserNavigate(**ids, url=dialog_site), owner="owner-a")
        with pytest.raises(BrowserActionUnknown):
            await control.click(BrowserClick(**ids, selector="#confirm"), owner="owner-a")
        entry = control.entries[ids["session_id"]]
        dialog = entry.pending_dialog
        accept = dialog.dialog.accept
        dispatches = []

        async def lost_receipt(prompt_text=None):
            dispatches.append(True)
            await accept(prompt_text)
            raise TimeoutError("synthetic lost dialog receipt")

        response = BrowserDialogHandle(**ids, dialog_id=dialog.dialog_id, action="accept")
        monkeypatch.setattr(dialog.dialog, "accept", lost_receipt)
        with pytest.raises(BrowserActionUnknown):
            await control.dialog_handle(response, owner="owner-a")
        with pytest.raises(ValueError, match="already claimed"):
            await control.dialog_handle(response, owner="owner-a")
        assert dispatches == [True]
        observed = await control.observe(BrowserObserve(**ids), owner="owner-a")
        assert "accepted once" in observed["text"]
        assert observed["last_dialog_response"] == {
            "dialog_id": dialog.dialog_id, "action": "accept", "outcome": "unconfirmed",
            "observed_dialog_closed": True,
        }
        assert (await control.dialogs(BrowserSession(**ids), owner="owner-a"))["dialog"] is None
        with pytest.raises(ValueError, match="unavailable"):
            await control.dialog_handle(response, owner="owner-a")
        assert dispatches == [True]
    finally:
        await control.close()


async def test_dialog_racing_snapshot_unblocks_read_and_final_close_does_not_accept(dialog_site):
    pytest.importorskip("playwright.async_api")
    control = BrowserControl(channel="chrome")
    try:
        ids = tab_args(await control.open(owner="owner-a"))
        await control.navigate(BrowserNavigate(**ids, url=dialog_site), owner="owner-a")
        entry = control.entries[ids["session_id"]]
        await entry.page.evaluate("setTimeout(() => alert('Timer fixture'), 10)")
        observed = await asyncio.wait_for(control.observe(BrowserObserve(**ids, include_image=True),
                                                          owner="owner-a"), 4)
        if observed.get("state") != "dialog_open":
            await asyncio.wait_for(entry.dialog_opened.wait(), 2)
            observed = await control.observe(BrowserObserve(**ids), owner="owner-a")
        assert observed["state"] == "dialog_open"
        assert observed["dialog"]["message"] == "Timer fixture"
        assert not entry.lock.locked()
        closed = await asyncio.wait_for(
            control.tab_close(BrowserSession(**ids), owner="owner-a"), 4,
        )
        assert closed["session_closed"] is True
        assert not entry.browser.is_connected()
        assert not control.entries
    finally:
        await control.close()


async def test_early_popup_dialog_is_observable_without_accepting_it(dialog_site):
    pytest.importorskip("playwright.async_api")
    control = BrowserControl(channel="chrome")
    try:
        ids = tab_args(await control.open(owner="owner-a"))
        await control.navigate(BrowserNavigate(**ids, url=dialog_site), owner="owner-a")
        try:
            await asyncio.wait_for(control.click(BrowserClick(**ids, selector="#popup-alert"),
                                                  owner="owner-a"), 4)
        except BrowserActionUnknown:
            pass  # The dialog may appear before the click acknowledgement.
        parent = await asyncio.wait_for(control.observe(BrowserObserve(**ids), owner="owner-a"), 1)
        assert parent["state"] == "dialog_open"
        assert parent["dialog"] is None
        assert any(tab["dialog"] for tab in parent["tabs"])
        session = control.entries[ids["session_id"]]
        popup = next(tab for tab in session.tabs.entries.values() if tab.tab_id != ids["tab_id"])
        await asyncio.wait_for(popup.dialog_opened.wait(), 2)
        popup_ids = {"session_id": popup.session_id, "tab_id": popup.tab_id}
        observed = await control.observe(BrowserObserve(**popup_ids), owner="owner-a")
        assert observed["dialog"]["message"] == "Popup early"
        handled = await control.dialog_handle(BrowserDialogHandle(
            **popup_ids, dialog_id=observed["dialog"]["dialog_id"], action="dismiss",
        ), owner="owner-a")
        assert "child ready" in handled["observation"]["text"]
    finally:
        await control.close()


async def test_confirmed_dialog_receipt_survives_unavailable_post_observation(
    dialog_site, monkeypatch,
):
    pytest.importorskip("playwright.async_api")
    control = BrowserControl(channel="chrome")
    try:
        ids = tab_args(await control.open(owner="owner-a"))
        await control.navigate(BrowserNavigate(**ids, url=dialog_site), owner="owner-a")
        with pytest.raises(BrowserActionUnknown):
            await control.click(BrowserClick(**ids, selector="#confirm"), owner="owner-a")
        dialog = (await control.dialogs(BrowserSession(**ids), owner="owner-a"))["dialog"]

        async def broken_snapshot(*args, **kwargs):
            raise RuntimeError("synthetic lost observation")

        with monkeypatch.context() as patch:
            patch.setattr(control, "_snapshot", broken_snapshot)
            result = await control.dialog_handle(BrowserDialogHandle(
                **ids, dialog_id=dialog["dialog_id"], action="dismiss",
            ), owner="owner-a")
        assert result["response_receipt"] == "confirmed"
        assert result["observation"] == {"state": "unavailable", "next_tool": "browser_observe"}
        observed = await control.observe(BrowserObserve(**ids), owner="owner-a")
        assert "dismissed once" in observed["text"]
        assert observed["last_dialog_response"]["outcome"] == "confirmed"
    finally:
        await control.close()


async def test_final_tab_cleanup_remains_an_update_blocker_until_driver_stops(
    tmp_path, monkeypatch,
):
    pytest.importorskip("playwright.async_api")
    engine = Engine(tmp_path / "state")
    engine.browser.channel = "chrome"
    entered = asyncio.Event()
    release = asyncio.Event()
    closing = None
    try:
        ids = tab_args(await engine.browser.open(owner="owner-a"))
        entry = engine.browser.entries[ids["session_id"]]
        original_stop = entry.playwright.stop
        phases = []
        started = time.monotonic()

        def trace(stage):
            phases.append({'stage': stage, 'at_ms': round((time.monotonic() - started) * 1000)})

        def observed_close(original, name):
            async def observed(*args, **kwargs):
                trace(name + '_start')
                try:
                    return await original(*args, **kwargs)
                finally:
                    trace(name + '_end')
            return observed

        async def held_stop():
            trace('driver_stop_start')
            entered.set()
            await release.wait()
            await original_stop()

        with monkeypatch.context() as patch:
            patch.setattr(entry.page, 'close', observed_close(entry.page.close, 'page_close'))
            patch.setattr(entry.browser, 'close',
                          observed_close(entry.browser.close, 'browser_close'))
            patch.setattr(entry.playwright, "stop", held_stop)
            closing = asyncio.create_task(engine.browser.tab_close(BrowserSession(**ids),
                                                                   owner="owner-a"))
            try:
                await asyncio.wait_for(entered.wait(), 3)
            except TimeoutError as error:
                error.add_note('Controlled final-tab cleanup phases: ' + json.dumps(phases))
                raise
            status = engine.status(owner="owner-a")
            assert status["active_sessions"] == 1
            assert status["update_blocker_details"][0]["stop_available"] is False
            listed = await engine.browser.tabs(BrowserSessionId(session_id=ids["session_id"]),
                                                owner="owner-a")
            assert listed["state"] == "closing" and listed["cleanup_in_progress"]
            with pytest.raises(ValueError, match="unavailable"):
                await engine.browser.tab_open(BrowserSessionId(session_id=ids["session_id"]),
                                              owner="owner-a")
            release.set()
            assert (await asyncio.wait_for(closing, 3))["session_closed"] is True
        assert engine.status(owner="owner-a")["active_sessions"] == 0
    finally:
        release.set()
        if closing is not None:
            await asyncio.gather(closing, return_exceptions=True)
        await engine.close()


@pytest.mark.parametrize("close_delay", [0, 3.2], ids=["normal", "slow-browser-close"])
async def test_failed_excess_popup_cleanup_closes_only_its_owned_session(
    dialog_site, monkeypatch, close_delay,
):
    pytest.importorskip("playwright.async_api")
    from playwright.async_api import Page

    from anywhere_computer.browser_control import _CLEANUP_WAIT_SECONDS, _PAGE_WAIT_SECONDS

    control = BrowserControl(channel="chrome")
    excess_attempted = asyncio.Event()
    entered = asyncio.Event()
    release = asyncio.Event()
    try:
        ids = tab_args(await control.open(owner="owner-a"))
        other = tab_args(await control.open(owner="owner-b"))
        session = BrowserSessionId(session_id=ids["session_id"])
        entry = control.entries[ids["session_id"]]
        for _ in range(7):
            await control.tab_open(session, owner="owner-a")
        await control.navigate(BrowserNavigate(**ids, url=dialog_site), owner="owner-a")
        page_close = Page.close
        browser_close = entry.browser.close
        failures = []

        async def fail_excess(page, *args, **kwargs):
            if page.context is entry.context and all(
                page is not owned.page for owned in entry.tabs.entries.values()
            ):
                failures.append(True)
                excess_attempted.set()
                raise TimeoutError("synthetic excess page cleanup failure")
            return await page_close(page, *args, **kwargs)

        async def held_browser_close():
            entered.set()
            await release.wait()
            await asyncio.sleep(close_delay)
            await browser_close()

        with monkeypatch.context() as patch:
            patch.setattr(Page, "close", fail_excess)
            patch.setattr(entry.browser, "close", held_browser_close)
            # Closing this context can interrupt the triggering click's snapshot.
            click = asyncio.create_task(control.click(BrowserClick(**ids, selector="#popup"),
                                                       owner="owner-a"))
            # First observe the real popup cleanup attempt. Click/actionability and
            # popup creation have their own bounded browser action budget; they
            # are not part of the three-second close-dispatch assertion.
            await asyncio.wait_for(excess_attempted.wait(), _PAGE_WAIT_SECONDS)
            await asyncio.wait_for(entered.wait(), 3)
            assert failures == [True]
            assert ids["session_id"] in control.entries
            listed = await control.tabs(session, owner="owner-a")
            assert listed["state"] == "closing" and listed["cleanup_in_progress"]
            with pytest.raises(ValueError, match="unavailable"):
                await control.tab_open(session, owner="owner-a")
            assert (await control.observe(BrowserObserve(**other), owner="owner-b"))["tab_id"] == (
                other["tab_id"])
            release.set()
            assert entry.tabs.close_task is not None
            # A retained close may outlive the caller receipt deadline. Observe
            # the actual browser and driver cleanup before checking ownership.
            await asyncio.wait_for(asyncio.shield(entry.tabs.close_task),
                                   4 * _CLEANUP_WAIT_SECONDS)
            results = await asyncio.gather(*entry.tabs.cleanup_tasks, return_exceptions=True)
            for result in results:
                if (isinstance(result, BaseException)
                        and not isinstance(result, BrowserActionUnknown)):
                    raise result
            await asyncio.gather(click, return_exceptions=True)
        assert ids["session_id"] not in control.entries
        assert not entry.browser.is_connected()
        assert other["session_id"] in control.entries
        assert not entry.tabs.rejected_pages
    finally:
        release.set()
        await control.close()


async def test_cleanup_timeout_is_unknown_and_retains_retryable_owner_blocker(monkeypatch):
    pytest.importorskip("playwright.async_api")
    import anywhere_computer.browser_control as browser_module

    control = BrowserControl(channel="chrome")
    try:
        ids = tab_args(await control.open(owner="owner-a"))
        entry = control.entries[ids["session_id"]]
        calls = []

        async def never_close():
            calls.append(True)
            await asyncio.Future()

        with monkeypatch.context() as patch:
            patch.setattr(browser_module, "_CLEANUP_WAIT_SECONDS", 0.03)
            patch.setattr(entry.browser, "close", never_close)
            with pytest.raises(BrowserActionUnknown, match="cleanup unconfirmed"):
                await asyncio.wait_for(control.stop(BrowserSession(**ids), owner="owner-a"), 1)
            assert ids["session_id"] in control.entries
            await asyncio.gather(entry.tabs.close_task, return_exceptions=True)
            assert calls == [True]
            assert entry.tabs.close_task.done()
            listed = await control.tabs(BrowserSessionId(session_id=ids["session_id"]),
                                        owner="owner-a")
            assert listed["state"] == "closing" and not listed["cleanup_in_progress"]
            with pytest.raises(ValueError, match="unavailable"):
                await control.stop(BrowserSession(**ids), owner="owner-b")
        # An explicit close after observing the original failure can finish cleanup.
        assert (await control.stop(BrowserSession(**ids), owner="owner-a"))["state"] == "closed"
        assert not control.entries
        assert calls == [True]
    finally:
        await control.close()


async def test_cleanup_caller_timeout_preserves_the_original_driver_stop(monkeypatch):
    pytest.importorskip("playwright.async_api")
    import anywhere_computer.browser_control as browser_module

    control = BrowserControl(channel="chrome")
    entered = asyncio.Event()
    release = asyncio.Event()
    try:
        ids = tab_args(await control.open(owner="owner-a"))
        entry = control.entries[ids["session_id"]]
        original_stop = entry.playwright.stop
        # Isolate driver-stop timing from variable real browser shutdown latency.
        await entry.browser.close()
        calls = []

        async def held_stop():
            calls.append(True)
            entered.set()
            await release.wait()
            await original_stop()

        with monkeypatch.context() as patch:
            patch.setattr(browser_module, "_CLEANUP_WAIT_SECONDS", 0.2)
            patch.setattr(entry.playwright, "stop", held_stop)
            first = asyncio.create_task(control.stop(BrowserSession(**ids), owner="owner-a"))
            await asyncio.wait_for(entered.wait(), 3)
            with pytest.raises(BrowserActionUnknown, match="cleanup unconfirmed"):
                await first
            cleanup = entry.tabs.close_task
            assert not cleanup.done()
            assert ids["session_id"] in control.entries
            assert not entry.tabs.lock.locked()
            assert control.busy(entry)
            release.set()
            await asyncio.wait_for(asyncio.shield(cleanup), 3)
        assert calls == [True]
        assert ids["session_id"] not in control.entries
    finally:
        release.set()
        await control.close()


async def test_slow_browser_close_finishes_after_caller_timeout(monkeypatch):
    pytest.importorskip("playwright.async_api")
    import anywhere_computer.browser_control as browser_module

    control = BrowserControl(channel="chrome")
    release = asyncio.Event()
    try:
        ids = tab_args(await control.open(owner="owner-a"))
        entry = control.entries[ids["session_id"]]
        # Keep real process teardown outside the tiny synthetic deadline.
        await entry.browser.close()

        async def held_close():
            await release.wait()

        with monkeypatch.context() as patch:
            patch.setattr(browser_module, "_CLEANUP_WAIT_SECONDS", 0.2)
            patch.setattr(entry.browser, "close", held_close)
            with pytest.raises(BrowserActionUnknown, match="cleanup unconfirmed"):
                await control.stop(BrowserSession(**ids), owner="owner-a")
            cleanup = entry.tabs.close_task
            assert cleanup is not None and not cleanup.done()
            assert ids["session_id"] in control.entries
            await asyncio.sleep(0.05)
            release.set()
            await asyncio.wait_for(asyncio.shield(cleanup), 3)
        assert ids["session_id"] not in control.entries
    finally:
        release.set()
        await control.close()
