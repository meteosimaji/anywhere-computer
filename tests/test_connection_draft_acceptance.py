"""Real Chrome acceptance of setup drafts against a disposable local connector."""

import asyncio
import json

import pytest
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright, expect
from test_other_pages_ui_browser import backend as backend
from test_other_pages_ui_browser import launch, open_workspace

from anywhere_computer.http_service import load_http_config

FIELDS = {
    "setup-resource": "https://reviewed.example/mcp",
    "setup-kind": "native",
    "setup-mode": "files",
    "setup-owner": "fixture-owner",
    "setup-client": "fixture-native",
    "setup-port": "18768",
}


async def fill_draft(frame):
    await frame.locator("#setup-resource").fill(FIELDS["setup-resource"])
    await frame.locator("#setup-kind").select_option(FIELDS["setup-kind"])
    await frame.locator("#setup-mode").select_option(FIELDS["setup-mode"])
    await frame.locator("#setup-options > summary").click()
    for field in ("setup-owner", "setup-client", "setup-port"):
        await frame.locator("#" + field).fill(FIELDS[field])


async def assert_fields(frame, values=FIELDS):
    for field, value in values.items():
        await expect(frame.locator("#" + field)).to_have_value(value)


def tool_calls(calls, name):
    return [p for p in calls if p.get("params", {}).get("name") == name]


async def trace_rpc(page, session, *, drop_save=None, hold=None, gateway=False):
    """Keep backend execution real; inject only the host transport's failure/delay."""
    calls = []
    entered = asyncio.Event()
    released = asyncio.Event()
    finished = asyncio.Event()

    async def route(handler):
        packet = json.loads(handler.request.post_data)
        calls.append(packet)
        name = packet["params"]["name"]
        # Only the device catalogue is a stand-in: no remote enrolment or auth occurs.
        if gateway and name in {"devices_list", "devices_tools"}:
            data = ({"devices": [
                {"device_id": "local", "name": "Fixture local", "state": "connected"},
                {"device_id": "fixture-other", "name": "Fixture other", "state": "connected"},
            ]} if name == "devices_list" else {"device_id": "fixture-other", "tools": []})
            operation = packet["params"]["_meta"][
                "io.github.meteosimaji.anywhere-computer/operation_id"]
            reply = {"jsonrpc": "2.0", "id": packet["id"], "result": {"structuredContent": {
                "operation_id": operation, "state": "completed", "data": data}}}
        else:
            reply = None
        save = name == "connection_setup_confirm"
        if save and drop_save == "before_dispatch":
            await handler.abort()
            entered.set()
            return
        if reply is None:
            reply = await session.handle(packet)
        page.rpc_replies.append((packet, reply))
        if save and drop_save == "after_dispatch":
            await handler.abort()
            entered.set()
            return
        try:
            if name == hold:
                entered.set()
                await released.wait()
            await handler.fulfill(content_type="application/json", body=json.dumps(reply))
        except PlaywrightError:
            if not page.is_closed():
                raise
        finally:
            if name == hold:
                finished.set()

    await page.route("https://host.test/rpc", route)
    return calls, entered, released, finished


@pytest.mark.parametrize("width", [390, 1000])
async def test_target_switch_requires_discard_and_never_sends_setup_to_other_device(backend, width):
    folder, session = backend
    async with async_playwright() as driver:
        browser = await launch(driver)
        try:
            context, page, frame = await open_workspace(
                browser, session, folder, width=width, view="connection")
            await fill_draft(frame)
            calls, _, _, _ = await trace_rpc(page, session, gateway=True)
            opened = await session.handle({
                "jsonrpc": "2.0", "id": "fixture-routing", "method": "tools/call",
                "params": {"name": "workspace_open", "arguments": {"view": "connection"}},
            })
            opened["result"]["_meta"]["workspaceTools"].extend(
                ["devices_list", "devices_tools", "devices_call"])
            await page.evaluate(
                "result=>send({method:'ui/notifications/tool-result',params:result})",
                opened["result"])
            await expect(frame.locator("#device")).to_be_enabled()
            await frame.locator("#device").select_option("fixture-other")
            await expect(frame.locator("#discard")).to_be_visible()
            await frame.locator("#keep").click()
            await expect(frame.locator("#device")).to_have_value("local")
            await assert_fields(frame)
            assert not tool_calls(calls, "devices_tools")
            await frame.locator("#device").select_option("fixture-other")
            await frame.locator("#discard-go").click()
            await expect(frame.locator("#device")).to_have_value("fixture-other")
            await expect(frame.locator("#setup-tab")).to_be_hidden()
            assert len(tool_calls(calls, "devices_tools")) == 1
            assert not tool_calls(calls, "devices_call")
            assert not tool_calls(calls, "connection_setup_plan")
            assert not tool_calls(calls, "connection_setup_confirm")
            await frame.locator("#device").select_option("local")
            await frame.locator("#setup-tab").click()
            await expect(frame.locator("#setup-reload")).to_be_enabled()
            await expect(frame.locator("#setup-resource")).to_have_value("")
            await context.close()
        finally:
            await browser.close()


@pytest.mark.parametrize("width", [390, 1000])
async def test_changed_setup_fields_survive_reconnect_and_cancelled_navigation(backend, width):
    folder, session = backend
    async with async_playwright() as driver:
        browser = await launch(driver)
        try:
            context, page, frame = await open_workspace(
                browser, session, folder, width=width, view="connection")
            await fill_draft(frame)
            await frame.locator("#setup-plan").click()
            await expect(frame.locator("#setup-confirm")).to_be_enabled()
            changed = {**FIELDS, "setup-resource": "https://changed.example/mcp"}
            await frame.locator("#setup-resource").fill(changed["setup-resource"])
            opened = await session.handle({
                "jsonrpc": "2.0", "id": "reconnect", "method": "tools/call",
                "params": {"name": "workspace_open", "arguments": {"view": "connection"}},
            })
            async with page.expect_response(lambda r: r.url.endswith("/rpc")):
                await page.evaluate(
                    "result=>send({method:'ui/notifications/tool-result',params:result})",
                    opened["result"])
            await expect(frame.locator("#setup-reload")).to_be_enabled()
            await assert_fields(frame, changed)
            await expect(frame.locator("#setup-confirm")).to_be_disabled()
            await frame.locator("#settings-tab").click()
            await expect(frame.locator("#discard")).to_be_visible()
            await page.keyboard.press("Escape")
            await expect(frame.locator("#discard")).to_be_hidden()
            await assert_fields(frame, changed)
            await frame.locator("#files-tab").click()
            await frame.locator("#keep").click()
            await assert_fields(frame, changed)
            await frame.locator("#settings-tab").click()
            await frame.locator("#discard-go").click()
            await expect(frame.locator("#setup")).to_be_hidden()
            await expect(frame.locator("#settings-tab")).to_be_enabled()
            await frame.locator("#setup-tab").click()
            await expect(frame.locator("#setup-reload")).to_be_enabled()
            await assert_fields(frame)
            assert len(tool_calls([p for p, _ in page.rpc_replies],
                                  "connection_setup_plan")) == 1
            assert not tool_calls([p for p, _ in page.rpc_replies],
                                  "connection_setup_confirm")
            await context.close()
        finally:
            await browser.close()


@pytest.mark.parametrize("width", [390, 1000])
async def test_double_click_plan_and_save_dispatch_once(backend, width):
    folder, session = backend
    async with async_playwright() as driver:
        browser = await launch(driver)
        try:
            context, page, frame = await open_workspace(
                browser, session, folder, width=width, view="connection")
            await fill_draft(frame)
            for name, button in (("connection_setup_plan", "setup-plan"),
                                 ("connection_setup_confirm", "setup-confirm")):
                calls, entered, released, finished = await trace_rpc(page, session, hold=name)
                try:
                    await frame.locator("#" + button).dblclick()
                    await asyncio.wait_for(entered.wait(), 5)
                    assert len(tool_calls(calls, name)) == 1
                    await expect(frame.locator("#" + button)).to_be_disabled()
                finally:
                    released.set()
                await asyncio.wait_for(finished.wait(), 5)
                if name == "connection_setup_plan":
                    await expect(frame.locator("#setup-confirm")).to_be_enabled()
                else:
                    await expect(frame.locator("#setup-status")).to_contain_text("保存済み")
                assert len(tool_calls(calls, name)) == 1
            saved = load_http_config(folder.parents[1] / "connector")
            assert saved.resource == FIELDS["setup-resource"]
            assert saved.owner == FIELDS["setup-owner"] and saved.port == int(FIELDS["setup-port"])
            assert len(tool_calls([p for p, _ in page.rpc_replies],
                                  "connection_setup_confirm")) == 1
            await context.close()
        finally:
            await browser.close()


@pytest.mark.parametrize("width", [390, 1000])
@pytest.mark.parametrize("loss", ["before_dispatch", "after_dispatch"])
async def test_lost_save_reply_requires_status_without_resending(backend, width, loss):
    folder, session = backend
    async with async_playwright() as driver:
        browser = await launch(driver)
        try:
            context, page, frame = await open_workspace(
                browser, session, folder, width=width, view="connection")
            await fill_draft(frame)
            await frame.locator("#setup-plan").click()
            await expect(frame.locator("#setup-confirm")).to_be_enabled()
            # Exercise the actual 75-second RPC deadline without a 75-second wall-clock wait.
            await page.clock.install()
            calls, entered, _, _ = await trace_rpc(page, session, drop_save=loss)
            await frame.locator("#setup-confirm").click()
            await asyncio.wait_for(entered.wait(), 5)
            await page.clock.fast_forward(75010)
            await expect(frame.locator("#setup-status")).to_contain_text("応答を確認できません")
            await expect(frame.locator("#setup-reload")).to_be_enabled()
            await assert_fields(frame)
            await expect(frame.locator("#setup-confirm")).to_be_disabled()
            box = await frame.locator("#setup-confirm").bounding_box()
            assert box is not None
            await page.mouse.click(box["x"] + box["width"] / 2,
                                   box["y"] + box["height"] / 2, click_count=2)
            await frame.locator("#setup-reload").click()
            await expect(frame.locator("#setup-reload")).to_be_enabled()
            assert len(tool_calls(calls, "connection_setup_confirm")) == 1
            assert len(tool_calls(calls, "connection_setup_status")) == 1
            if loss == "before_dispatch":
                await assert_fields(frame)
                await expect(frame.locator("#setup-form")).to_be_visible()
                await expect(frame.locator("#setup-confirm")).to_be_disabled()
                assert not (folder.parents[1] / "connector" / "http-server").exists()
            else:
                await expect(frame.locator("#setup-status")).to_contain_text("保存済み")
                await expect(frame.locator("#setup-form")).to_be_hidden()
                saved = load_http_config(folder.parents[1] / "connector")
                assert saved.resource == FIELDS["setup-resource"]
            await context.close()
        finally:
            await browser.close()


@pytest.mark.parametrize("width", [390, 1000])
async def test_host_teardown_during_save_reopens_status_without_retry(backend, width):
    folder, session = backend
    async with async_playwright() as driver:
        browser = await launch(driver)
        try:
            context, page, frame = await open_workspace(
                browser, session, folder, width=width, view="connection")
            await fill_draft(frame)
            await frame.locator("#setup-plan").click()
            await expect(frame.locator("#setup-confirm")).to_be_enabled()
            calls, entered, released, finished = await trace_rpc(
                page, session, hold="connection_setup_confirm")
            try:
                await frame.locator("#setup-confirm").click()
                await asyncio.wait_for(entered.wait(), 5)
                await asyncio.wait_for(page.evaluate("""() => new Promise(resolve => {
              const id='fixture-teardown';
              const listener=event=>{if(event.source===frame.contentWindow && event.data?.id===id) {
                window.removeEventListener('message',listener);resolve(event.data.result);}};
              window.addEventListener('message',listener);
              send({id,method:'ui/resource-teardown',params:{}});
                })"""), 5)
                await page.close()
            finally:
                released.set()
            await asyncio.wait_for(finished.wait(), 5)
            await context.close()
            context, next_page, next_frame = await open_workspace(
                browser, session, folder, width=width, view="connection")
            await expect(next_frame.locator("#setup-status")).to_contain_text("保存済み")
            assert len(tool_calls(calls, "connection_setup_confirm")) == 1
            assert not tool_calls([p for p, _ in next_page.rpc_replies],
                                  "connection_setup_confirm")
            await context.close()
        finally:
            await browser.close()
