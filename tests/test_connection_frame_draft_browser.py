"""Actual opaque iframe with only the documented optional host state API simulated."""

import asyncio
import inspect
import json
import time
from dataclasses import dataclass, field

import pytest
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright, expect
from test_connection_draft_acceptance import (
    FIELDS,
    assert_fields,
    fill_draft,
    tool_calls,
    trace_rpc,
)
from test_other_pages_ui_browser import HOST, launch
from test_other_pages_ui_browser import backend as backend

from anywhere_computer.mcp_server import MCPSession
from anywhere_computer.workspace_ui import workspace_resource


@dataclass
class HostState:
    snapshot: dict | None = None
    writes: list = field(default_factory=list)
    calls: list = field(default_factory=list)


def instrument_bootstrap(session, trace):
    connector = session.execute.__self__
    connector.fixture_bootstrap_trace = trace
    if getattr(connector, 'fixture_bootstrap_instrumented', False):
        session.catalog = connector.catalog
        return
    connector.fixture_bootstrap_instrumented = True

    def instrument(owner, name, stage):
        original = getattr(owner, name)
        if inspect.iscoroutinefunction(original):
            async def observed(*args, **kwargs):
                callback = connector.fixture_bootstrap_trace
                callback(stage + '_start')
                try:
                    return await original(*args, **kwargs)
                finally:
                    callback(stage + '_end')
        else:
            def observed(*args, **kwargs):
                callback = connector.fixture_bootstrap_trace
                callback(stage + '_start')
                try:
                    return original(*args, **kwargs)
                finally:
                    callback(stage + '_end')
        setattr(owner, name, observed)

    # These are owned fixture instances. Keep the real catalog, SQLite commits,
    # operation bindings and setup controller; record only method boundaries.
    instrument(connector, 'catalog', 'catalog')
    instrument(connector, '_bind', 'connector_bind')
    instrument(connector, '_claim', 'connector_claim')
    instrument(connector, '_save_reply', 'connector_record')
    instrument(connector, '_status_reply', 'connector_status_transaction')
    instrument(connector.controller, 'progress', 'connector_progress')
    instrument(connector.fixture_engine.ledger, 'claim', 'engine_claim')
    instrument(connector.fixture_engine.ledger, 'finish', 'engine_record')
    session.catalog = connector.catalog


async def open_with_state(browser, session, folder, store, *, width=390, supported=True):
    context = await browser.new_context(viewport={"width": width, "height": 860},
                                        reduced_motion="reduce")
    page = await context.new_page()
    page.rpc_replies = []
    started = time.monotonic()
    events = []

    def trace(stage, **details):
        if len(events) < 40:
            events.append({'at_ms': round((time.monotonic() - started) * 1000),
                           'stage': stage, **details})

    instrument_bootstrap(session, trace)

    # Record bootstrap message names and RPC timing, never form values or state.
    # A failure retains the original deadline/assertion; no initialization retry.
    await page.add_init_script("""(() => {
      let count = 0;
      addEventListener('message', event => {
        const data = event.data;
        if (!data || data.jsonrpc !== '2.0' || count++ >= 20) return;
        console.debug('fixture-bootstrap:' + JSON.stringify({
          frame: self === top ? 'host' : 'widget', method: data.method || 'reply',
          tool: data.params?.name || null, error: Boolean(data.error)
        }));
      });
    })();""")

    def captured(message):
        prefix = "fixture-widget-state:"
        if message.text.startswith(prefix):
            value = json.loads(message.text[len(prefix):])
            store.snapshot = value
            store.writes.append(value)
        elif message.text.startswith('fixture-bootstrap:'):
            trace('message', **json.loads(message.text[len('fixture-bootstrap:'):]))

    page.on("console", captured)
    page.on('pageerror', lambda error: trace('page_error', message=str(error)[:160]))

    async def route(handler):
        url = handler.request.url
        try:
            if url.endswith("/workspace"):
                bridge = ("<script>window.openai={widgetState:" + json.dumps(store.snapshot)
                          + ",setWidgetState(value){this.widgetState=structuredClone(value);"
                          "console.debug('fixture-widget-state:'+JSON.stringify(value));}};"
                          "</script>" if supported else "")
                if supported == "refused":
                    bridge = ("<script>window.openai={widgetState:null,setWidgetState(){"
                              "throw new Error('fixture host refused state');}};</script>")
                html = str(workspace_resource()["text"])
                await handler.fulfill(content_type="text/html; charset=utf-8",
                                      body=html.replace("<script>", bridge + "<script>", 1))
            elif url.endswith("/rpc"):
                packet = json.loads(handler.request.post_data)
                fields = {'method': packet.get('method'),
                          'tool': packet.get('params', {}).get('name')}
                trace('rpc_received', **fields)
                store.calls.append(packet)
                reply = await session.handle(packet)
                trace('rpc_handled', **fields)
                page.rpc_replies.append((packet, reply))
                await handler.fulfill(content_type="application/json", body=json.dumps(reply))
                trace('rpc_fulfilled', **fields)
            elif url.endswith("/away"):
                await handler.fulfill(content_type="text/html", body="<p>fixture away</p>")
            else:
                await handler.fulfill(content_type="text/html; charset=utf-8", body=HOST)
        except PlaywrightError:
            trace('route_error', page_closed=page.is_closed())
            if not page.is_closed():
                raise

    await page.route("https://host.test/**", route)
    await page.goto(f"https://host.test/?path={folder}&view=connection")
    frame = page.frame_locator("iframe")
    try:
        await expect(frame.locator("#setup-reload")).to_be_enabled()
        await expect(frame.locator("#notice")).to_contain_text("このコンピューター")
    except AssertionError as error:
        try:
            observation = await asyncio.wait_for(
                frame.locator('body').evaluate("""body => ({
                  reloadDisabled: body.querySelector('#setup-reload')?.disabled,
                  notice: body.querySelector('#notice')?.textContent?.slice(0, 180),
                  status: body.querySelector('#setup-status')?.textContent?.slice(0, 180)
                })"""), 2,
            )
        except (PlaywrightError, TimeoutError):
            observation = {'state': 'inspection_unavailable'}
        raise AssertionError(f'{error}\nBootstrap observation: '
                             + json.dumps({'events': events, 'ui': observation},
                                          ensure_ascii=False)) from error
    return context, page, frame


async def test_initial_readiness_failure_records_bootstrap_without_draft_fields(
    backend, monkeypatch,
):
    folder, session = backend
    gate = asyncio.Event()
    original_handle = session.handle

    async def held_status(packet):
        if packet.get('params', {}).get('name') == 'connection_setup_status':
            await gate.wait()
        return await original_handle(packet)

    monkeypatch.setattr(session, 'handle', held_status)
    async with async_playwright() as driver:
        browser = await launch(driver)
        try:
            with pytest.raises(AssertionError) as result:
                await open_with_state(browser, session, folder, HostState())
            evidence = json.loads(str(result.value).split('Bootstrap observation: ', 1)[1])
            assert evidence['ui']['reloadDisabled'] is True
            assert len(evidence['events']) <= 40
            stages = {item['stage'] for item in evidence['events']}
            assert {'catalog_start', 'catalog_end', 'connector_bind_start',
                    'connector_bind_end', 'engine_claim_start', 'engine_claim_end',
                    'engine_record_start', 'engine_record_end'} <= stages
            status = [item for item in evidence['events']
                      if item.get('tool') == 'connection_setup_status'
                      and item['stage'].startswith('rpc_')]
            assert [item['stage'] for item in status] == ['rpc_received']
            assert all('values' not in item and 'params' not in item for item in evidence['events'])
        finally:
            gate.set()
            await browser.close()


async def review_and_change(frame, *, changed="https://unreviewed.example/mcp"):
    await fill_draft(frame)
    await frame.locator("#setup-plan").click()
    await expect(frame.locator("#setup-confirm")).to_be_enabled()
    await frame.locator("#setup-resource").fill(changed)
    await expect(frame.locator("#setup-confirm")).to_be_disabled()


@pytest.mark.parametrize("width", [390, 1000])
@pytest.mark.parametrize("lifecycle", ["close", "back", "frame"])
async def test_unreviewed_draft_survives_host_frame_recreation(backend, width, lifecycle):
    folder, session = backend
    store = HostState()
    changed = "https://unreviewed.example/mcp"
    async with async_playwright() as driver:
        browser = await launch(driver)
        try:
            context, page, frame = await open_with_state(
                browser, session, folder, store, width=width)
            await review_and_change(frame, changed=changed)
            await frame.locator("#setup-owner").fill("draft-owner")
            await expect(frame.locator("#setup-draft-note")).to_be_hidden()
            assert store.snapshot["privateContent"]["connectionDraft"]["values"][
                "setup-resource"] == changed
            if lifecycle == "close":
                await context.close()
                context, page, frame = await open_with_state(
                    browser, session, folder, store, width=width)
            elif lifecycle == "back":
                await page.goto("https://host.test/away")
                await page.go_back()
                await expect(frame.locator("#setup-reload")).to_be_enabled()
            else:
                await page.evaluate("""async () => {
                  const frame=document.querySelector('iframe');
                  await new Promise(done=>{
                    const id='fixture-teardown';
                    const listener=event=>{
                      if(event.source===frame.contentWindow&&event.data?.id===id){
                        removeEventListener('message',listener);done();}};
                    addEventListener('message',listener);
                    frame.contentWindow.postMessage({jsonrpc:'2.0',id,method:'ui/resource-teardown',params:{}},'*');
                  });
                  frame.src='/workspace';
                }""")
                await expect(frame.locator("#setup-reload")).to_be_enabled()
            for field_id, value in {**FIELDS, "setup-resource": changed,
                                    "setup-owner": "draft-owner"}.items():
                await expect(frame.locator("#" + field_id)).to_have_value(value)
            await expect(frame.locator("#setup-confirm")).to_be_disabled()
            assert len(tool_calls(store.calls, "connection_setup_plan")) == 1
            assert not tool_calls(store.calls, "connection_setup_confirm")
            assert not (folder.parents[1] / "connector" / "http-server").exists()
            await frame.locator("#files-tab").click()
            await frame.locator("#keep").click()
            await expect(frame.locator("#setup-resource")).to_have_value(changed)
            await frame.locator("#files-tab").click()
            await frame.locator("#discard-go").click()
            assert store.snapshot["privateContent"]["connectionDraft"] is None
            await frame.locator("#setup-tab").click()
            await expect(frame.locator("#setup-resource")).to_have_value(FIELDS["setup-resource"])
            await context.close()
        finally:
            await browser.close()


@pytest.mark.parametrize("loss", ["before_dispatch", "after_dispatch"])
async def test_save_reply_lost_then_close_uses_only_status(backend, loss):
    folder, session = backend
    store = HostState()
    async with async_playwright() as driver:
        browser = await launch(driver)
        try:
            context, page, frame = await open_with_state(browser, session, folder, store)
            await fill_draft(frame)
            await frame.locator("#setup-plan").click()
            await expect(frame.locator("#setup-confirm")).to_be_enabled()
            calls, entered, _, _ = await trace_rpc(page, session, drop_save=loss)
            await frame.locator("#setup-confirm").click()
            await asyncio.wait_for(entered.wait(), 5)
            assert store.snapshot["privateContent"]["connectionDraft"]["held"] is True
            await context.close()
            context, page, frame = await open_with_state(browser, session, folder, store)
            assert len(tool_calls(calls, "connection_setup_confirm")) == 1
            assert not tool_calls(store.calls, "connection_setup_confirm")
            assert len(tool_calls(store.calls, "connection_setup_plan")) == 1
            if loss == "before_dispatch":
                await assert_fields(frame)
                await expect(frame.locator("#setup-confirm")).to_be_disabled()
                assert not (folder.parents[1] / "connector" / "http-server").exists()
            else:
                await expect(frame.locator("#setup-status")).to_contain_text("保存済み")
                await expect(frame.locator("#setup-form")).to_be_hidden()
            await context.close()
        finally:
            await browser.close()


@pytest.mark.parametrize("supported", [False, "refused"])
async def test_unsupported_host_preserves_existing_behavior(backend, supported):
    folder, session = backend
    store = HostState()
    async with async_playwright() as driver:
        browser = await launch(driver)
        try:
            context, page, frame = await open_with_state(
                browser, session, folder, store, supported=supported)
            await review_and_change(frame)
            await context.close()
            context, page, frame = await open_with_state(
                browser, session, folder, store, supported=supported)
            await expect(frame.locator("#setup-resource")).to_have_value(FIELDS["setup-resource"])
            await expect(frame.locator("#setup-draft-note")).to_contain_text(
                "このホストでは画面を閉じると未保存の入力が失われます")
            assert not store.writes
            assert not tool_calls(store.calls, "connection_setup_confirm")
            # The notice and a refused optional API must not block explicit normal saving.
            await frame.locator("#setup-confirm").click()
            await expect(frame.locator("#setup-status")).to_contain_text("保存済み")
            assert len(tool_calls(store.calls, "connection_setup_confirm")) == 1
            await context.close()
        finally:
            await browser.close()


@pytest.mark.parametrize("case", ["session", "target", "version", "secret-key", "userinfo",
                                 "query", "fragment", "oversize"])
async def test_snapshot_from_wrong_scope_or_secret_fields_is_never_restored(backend, case):
    folder, session = backend
    store = HostState()
    async with async_playwright() as driver:
        browser = await launch(driver)
        try:
            context, page, frame = await open_with_state(browser, session, folder, store)
            await review_and_change(frame)
            await context.close()
            snapshot = store.snapshot["privateContent"]["connectionDraft"]
            if case in {"target", "version"}:
                snapshot[case] = "other" if case == "target" else 99
            elif case == "secret-key":
                snapshot["values"]["authorization"] = "fixture-never-restore-secret"
            elif case in {"userinfo", "query", "fragment", "oversize"}:
                snapshot["values"]["setup-resource"] = {
                    "userinfo": "https://fixture-never-store-secret@fixture.example/mcp",
                    "query": "https://fixture.example/mcp?token=fixture-never-store-secret",
                    "fragment": "https://fixture.example/mcp#fixture-never-store-secret",
                    "oversize": "https://fixture.example/" + "x" * 2048,
                }[case]
            else:
                # Same OS owner/controller does not allow another transport session's draft.
                session = MCPSession(session.catalog, session.execute)
                from test_workspace_ui import initialize
                await initialize(session)
            context, page, frame = await open_with_state(browser, session, folder, store)
            await expect(frame.locator("#setup-resource")).to_have_value(FIELDS["setup-resource"])
            assert not tool_calls(store.calls, "connection_setup_confirm")
            assert len(tool_calls(store.calls, "connection_setup_plan")) == 1
            await context.close()
        finally:
            await browser.close()


@pytest.mark.parametrize("resource", [
    "https://fixture-never-store-secret@fixture.example/mcp",
    "https://fixture.example/mcp?token=fixture-never-store-secret",
    "https://fixture.example/mcp#fixture-never-store-secret",
])
async def test_secret_shaped_url_is_not_written_to_host_state(backend, resource):
    folder, session = backend
    store = HostState()
    async with async_playwright() as driver:
        browser = await launch(driver)
        try:
            context, page, frame = await open_with_state(browser, session, folder, store)
            await review_and_change(frame, changed=resource)
            await expect(frame.locator("#setup-draft-note")).to_contain_text(
                "この入力は画面を閉じると保持できません")
            assert store.snapshot["privateContent"]["connectionDraft"] is None
            assert "fixture-never-store-secret" not in json.dumps(store.writes)
            assert not tool_calls(store.calls, "connection_setup_confirm")
            await context.close()
        finally:
            await browser.close()


async def test_teardown_blocks_old_frame_snapshot_and_late_save_reply(backend):
    folder, session = backend
    store = HostState()
    async with async_playwright() as driver:
        browser = await launch(driver)
        try:
            context, page, frame = await open_with_state(browser, session, folder, store)
            await fill_draft(frame)
            await frame.locator("#setup-plan").click()
            await expect(frame.locator("#setup-confirm")).to_be_enabled()
            calls, entered, released, finished = await trace_rpc(
                page, session, hold="connection_setup_confirm")
            try:
                await frame.locator("#setup-confirm").click()
                await asyncio.wait_for(entered.wait(), 5)
                await page.evaluate("""async () => {
                  await new Promise(done=>{
                    const id='fixture-teardown';
                    const listener=event=>{if(event.data?.id===id){
                      removeEventListener('message',listener);done();}};
                    addEventListener('message',listener);
                    document.querySelector('iframe').contentWindow.postMessage({
                      jsonrpc:'2.0',id,method:'ui/resource-teardown',params:{}},'*');
                  });
                }""")
                await expect(frame.locator("#setup-reload")).to_be_disabled()
                writes = len(store.writes)
                # A late old-frame callback cannot overwrite the new frame's snapshot.
                await frame.locator("#setup-resource").evaluate("""element=>{
                  element.value='https://old-frame.example/mcp';
                  element.dispatchEvent(new Event('input',{bubbles:true}));
                }""")
                released.set()
                await asyncio.wait_for(finished.wait(), 5)
                assert len(store.writes) == writes
                await context.close()
                context, page, frame = await open_with_state(browser, session, folder, store)
                await expect(frame.locator("#setup-form")).to_be_hidden()
                await expect(frame.locator("#setup-status")).to_contain_text("保存済み")
                assert len(tool_calls(calls, "connection_setup_confirm")) == 1
                assert not tool_calls(store.calls, "connection_setup_confirm")
                await context.close()
            finally:
                released.set()
        finally:
            await browser.close()


async def test_pending_plan_for_another_owner_or_device_is_not_adopted(backend):
    folder, session = backend
    store = HostState()
    async with async_playwright() as driver:
        browser = await launch(driver)
        try:
            context, page, frame = await open_with_state(browser, session, folder, store)
            await fill_draft(frame)
            await frame.locator("#setup-plan").click()
            await expect(frame.locator("#setup-confirm")).to_be_enabled()
            calls, entered, _, _ = await trace_rpc(
                page, session, drop_save="before_dispatch")
            await frame.locator("#setup-confirm").click()
            await asyncio.wait_for(entered.wait(), 5)
            original_plan = store.snapshot["privateContent"]["connectionDraft"]["pendingPlan"]
            await context.close()
            # Independent explicit fixture actor reviews/saves a different public plan.
            other = await session.handle({"jsonrpc": "2.0", "id": "fixture-other-plan",
                                          "method": "tools/call", "params": {
                "name": "connection_setup_plan", "arguments": {
                    "resource": FIELDS["setup-resource"], "owner": "different-owner",
                    "client_kind": "native", "client": FIELDS["setup-client"],
                    "mode": "files", "port": int(FIELDS["setup-port"]),
                }}})
            other_plan = other["result"]["structuredContent"]["data"]["plan_id"]
            assert other_plan != original_plan  # identity includes generated device and owner
            await session.handle({"jsonrpc": "2.0", "id": "fixture-other-confirm",
                                  "method": "tools/call", "params": {
                "name": "connection_setup_confirm", "arguments": {"plan_id": other_plan}}})
            context, page, frame = await open_with_state(browser, session, folder, store)
            await assert_fields(frame)
            await expect(frame.locator("#setup-form")).to_be_visible()
            await expect(frame.locator("#setup-resource")).to_have_attribute("readonly", "")
            await expect(frame.locator("#setup-technical")).to_contain_text("different-owner")
            assert len(tool_calls(calls, "connection_setup_confirm")) == 1
            assert not tool_calls(store.calls, "connection_setup_confirm")
            await context.close()
        finally:
            await browser.close()


async def test_cached_page_resume_can_hold_further_edits_but_teardown_cannot(backend):
    folder, session = backend
    store = HostState()
    async with async_playwright() as driver:
        browser = await launch(driver)
        try:
            context, page, frame = await open_with_state(browser, session, folder, store)
            await review_and_change(frame)
            # Browser lifecycle notifications, rather than claiming an actual bfcache hit.
            await frame.locator("#setup-resource").evaluate("""() => {
              dispatchEvent(new PageTransitionEvent('pagehide',{persisted:true}));
              dispatchEvent(new PageTransitionEvent('pageshow',{persisted:true}));
            }""")
            # fill's reply can precede delivery of the host's console publication.
            async with page.expect_console_message(
                lambda message: message.text.startswith("fixture-widget-state:")
                and "https://after-back.example/mcp" in message.text,
                timeout=5000,
            ):
                await frame.locator("#setup-resource").fill("https://after-back.example/mcp")
            assert store.snapshot["privateContent"]["connectionDraft"]["values"][
                "setup-resource"] == "https://after-back.example/mcp"
            await page.evaluate("""() =>
              document.querySelector('iframe').contentWindow.postMessage({
              jsonrpc:'2.0',id:'fixture-teardown',method:'ui/resource-teardown',params:{}},'*')""")
            await expect(frame.locator("#setup-reload")).to_be_disabled()
            writes = len(store.writes)
            host_writes = await frame.locator("#setup-resource").evaluate("""element => {
              let writes=0;
              const publish=window.openai.setWidgetState.bind(window.openai);
              window.openai.setWidgetState=(value)=>{writes++;return publish(value);};
              dispatchEvent(new PageTransitionEvent('pageshow',{persisted:true}));
              element.value='https://closed-frame.example/mcp';
              element.dispatchEvent(new Event('input',{bubbles:true}));
              return writes;
            }""")
            assert host_writes == 0
            assert len(store.writes) == writes
            assert not tool_calls(store.calls, "connection_setup_confirm")
            await context.close()
        finally:
            await browser.close()
