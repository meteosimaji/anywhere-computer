"""Distinguish a refused draft, a live worker, and an uncertain failed dispatch."""
import asyncio
from types import SimpleNamespace

import pytest
from test_subchat_browser_backend import HTML
from test_subchat_lifecycle import BrowserFixture

from anywhere_computer.models import Request
from anywhere_computer.state import Ledger
from anywhere_computer.subchat import (
    SubchatOutcomeUnknown,
    SubchatPreflightFailed,
    SubchatReceipt,
    Subchats,
)
from anywhere_computer.subchat_mcp import session
from anywhere_computer.subchat_state import SubchatHTTPSelection, SubchatSubmissions

SELECTION = SubchatHTTPSelection(version_id='fixture', preset_id=0,
                                 model_slug='fixture', thinking_effort=None)


async def test_late_dispatch_error_is_visible_without_blocking_receipt_recovery(
        tmp_path, monkeypatch, caplog):
    from anywhere_computer import subchat_mcp

    release = asyncio.Event()
    monkeypatch.setattr(subchat_mcp, 'SEND_ACK_TIMEOUT', .001)

    class Backend(BrowserFixture):
        receipt_ready = False

        async def send(self, submission):
            self.sends += 1
            await release.wait()
            raise ConnectionError('private provider body and access-token')

        async def find_submission(self, submission):
            return (SubchatReceipt(conversation_id='conversation', user_message_id='user',
                                   prompt=submission.prompt) if self.receipt_ready else None)

    ledger = Ledger(tmp_path)
    store = SubchatSubmissions(ledger.connection)
    backend = Backend()
    service = Subchats(store, backend)
    server = session(service)
    operation = '1' * 32
    try:
        sent = await server.execute(Request(operation_id=operation, tool='subchat_send',
            arguments={'prompt': 'fixture', 'model': 'model', 'effort': 'effort',
                       'http_selection': SELECTION.model_dump()}))
        assert sent.state == 'running'
        assert sent.data['send_worker'] == {'state': 'running'}
        release.set()
        with pytest.raises(SubchatOutcomeUnknown):
            await server.sends[operation]
        await asyncio.sleep(0)
        for tool in ['subchat_status', 'subchat_wait']:
            observed = await server.execute(Request(operation_id='2' * 32, tool=tool,
                arguments={'operation_id': operation,
                           **({'wait_ms': 1} if tool == 'subchat_wait' else {})}))
            assert observed.data['state'] == 'sending'
            assert observed.data['provider_receipt'] == 'unconfirmed'
            assert observed.data['send_worker'] == {
                'state': 'failed', 'reason': 'connection_failed'}
            assert observed.data['http_progress']['stage'] == 'send_worker_failed'
            assert 'access-token' not in observed.model_dump_json()
        assert 'access-token' not in caplog.text
        activity = await server.execute(Request(operation_id='3' * 32,
                                                 tool='subchat_activity', arguments={}))
        assert activity.data['active_sends'] == 0
        assert activity.data['failed_send_workers'] == 1
        backend.receipt_ready = True
        observed = await server.execute(Request(operation_id='4' * 32,
            tool='subchat_observe', arguments={'operation_id': operation}))
        assert observed.data['provider_receipt'] == 'confirmed'
        assert backend.sends == 1
        await server.close()
        server = session(service)
        restored = await server.execute(Request(operation_id='5' * 32,
            tool='subchat_status', arguments={'operation_id': operation}))
        assert restored.data['send_worker'] == {'state': 'not_owned'}
        assert restored.data['http_progress']['stage'] == 'send_worker_failed'
        assert backend.sends == 1
    finally:
        release.set()
        await server.close()
        ledger.close()


@pytest.mark.parametrize('condition', ['removed_lf', 'delayed_fetch', 'manual_gesture',
                                       'later_edit', 'close_unconfirmed',
                                       'close_unconfirmed_guard_install_failed',
                                       'close_unconfirmed_guard_install_stalled',
                                       'close_unconfirmed_unroute_stalled',
                                       'close_unconfirmed_client_close_stalled'])
async def test_browser_draft_rejection_requires_closed_page_and_no_dispatch(
        tmp_path, monkeypatch, condition, caplog):
    playwright = pytest.importorskip('playwright.async_api')
    from anywhere_computer import subchat_chrome_login
    from anywhere_computer.subchat_browser import backend as backend_module
    from anywhere_computer.subchat_browser import httpx_generation
    from anywhere_computer.subchat_browser.backend import BrowserSubchatBackend

    clients = []
    stopped = asyncio.Event()
    monkeypatch.setattr(backend_module, '_REJECTED_DRAFT_CLEANUP_TIMEOUT', .02)

    async def auth(_context, client, **_kwargs):
        clients.append(client)
        if condition == 'close_unconfirmed_client_close_stalled':
            close_client = client.aclose

            async def stalled_client_close():
                await close_client()
                await stopped.wait()

            monkeypatch.setattr(client, 'aclose', stalled_client_close)
        return SimpleNamespace(account_id='fixture-account',
            authorization=SimpleNamespace(get_secret_value=lambda: 'Bearer fixture'))

    async def post(*_args, **_kwargs):
        pytest.fail('A locally rejected input must not reach HTTPX generation')

    monkeypatch.setattr(subchat_chrome_login, 'chrome_http_session', auth)
    monkeypatch.setattr(httpx_generation, 'post_browser_prepared_once', post)
    ledger = Ledger(tmp_path)
    store = SubchatSubmissions(ledger.connection)
    operation = '6' * 32
    escaped_requests = []
    async with playwright.async_playwright() as driver:
        browser = await driver.chromium.launch(channel='chrome', headless=True)
        try:
            context = await browser.new_context()
            original = await context.new_page()
            await original.set_content('<title>Original page</title>Keep this page')
            # Simulate the observed provider editor normalization. Ordinary
            # contenteditable itself preserves this LF; this fixture removes it.
            normalize = '''<script>
              document.querySelector('[role=textbox]').addEventListener('input', event => {
                const editor=event.currentTarget;
                const breaks=editor.querySelectorAll('br');
                if (breaks.length) breaks[breaks.length-1].remove();
              });
            </script>'''
            if condition == 'manual_gesture':
                normalize = normalize.replace(
                    'if (breaks.length)',
                    "document.querySelector('#send').dispatchEvent(new MouseEvent("
                    "'click',{bubbles:true})); if (breaks.length)")
            elif condition == 'later_edit':
                normalize = ''
            async def serve(route):
                if '/backend-api/f/conversation' in route.request.url:
                    escaped_requests.append(route.request.method)
                    await route.fulfill(content_type='application/json', body='{}')
                    return
                await route.fulfill(content_type='text/html', body=HTML + normalize)

            await context.route('https://chatgpt.com/**', serve)

            class PreparedBackend(BrowserSubchatBackend):
                async def prepare(self, submission):
                    page = await context.new_page()
                    self.pages[submission.operation_id] = page
                    self._prepared_baseline_kinds[submission.operation_id] = 'empty'
                    await page.goto('https://chatgpt.com/')
                    if condition in {'close_unconfirmed_guard_install_failed',
                                     'close_unconfirmed_guard_install_stalled'}:
                        register_route = page.route
                        registrations = 0

                        async def failing_second_route(pattern, handler):
                            nonlocal registrations
                            registrations += 1
                            if registrations == 2:
                                if condition.endswith('stalled'):
                                    await stopped.wait()
                                raise RuntimeError('private route failure detail')
                            await register_route(pattern, handler)

                        monkeypatch.setattr(page, 'route', failing_second_route)
                    if condition == 'close_unconfirmed_unroute_stalled':
                        async def stalled_unroute(*_args):
                            await stopped.wait()
                        monkeypatch.setattr(page, 'unroute', stalled_unroute)
                    if condition == 'later_edit':
                        evaluate_handle = page.evaluate_handle

                        async def edit_after_insertion(expression, arg=None):
                            guard = await evaluate_handle(expression, arg)
                            await page.get_by_role('textbox').fill('user replacement')
                            return guard

                        monkeypatch.setattr(page, 'evaluate_handle', edit_after_insertion)
                    return ()

            backend = PreparedBackend(context, http_read=True, httpx_generation=True,
                store=store, record_preflight_failure=lambda operation_id:
                    store.fail_http_before_dispatch(operation_id, owner=None))
            if condition.startswith('close_unconfirmed'):
                async def failed_close(_page):
                    return None
                monkeypatch.setattr(backend._http_reader, 'close_owned_page', failed_close)
            elif condition == 'delayed_fetch':
                close_page = backend._http_reader.close_owned_page

                async def fetch_then_close(page):
                    assert await page.evaluate('''async () => {
                      try { await fetch('/backend-api/f/conversation', {
                        method:'POST', body:'{}'}); return 'escaped'; }
                      catch { return 'blocked'; }
                    }''') == 'blocked'
                    await close_page(page)

                monkeypatch.setattr(backend._http_reader, 'close_owned_page', fetch_then_close)
            service = Subchats(store, backend)
            proven_unsent = condition in {'removed_lf', 'delayed_fetch'}
            expected = (SubchatPreflightFailed if proven_unsent
                        else SubchatOutcomeUnknown)
            with pytest.raises(expected):
                await asyncio.wait_for(service.send(operation, 'https://example.com/docs\n',
                    'Future model', 'Initial effort', owner=None, http_selection=SELECTION), 5)
            saved = store.get(operation, owner=None)
            page = backend.pages[operation]
            assert saved.state == ('preflight_failed' if proven_unsent else 'sending')
            assert saved.user_message_id is None
            assert page.is_closed() is proven_unsent
            assert not original.is_closed()
            assert await original.title() == 'Original page'
            if condition not in {'manual_gesture', 'later_edit'}:
                assert store.http_progress(operation, owner=None)['stage'] == 'draft_rejected'
            else:
                assert store.http_progress(operation, owner=None) is None
            if condition == 'later_edit':
                assert await page.get_by_role('textbox').inner_text() == 'user replacement'
            # The final refusal cannot be re-entered as a fresh send.
            repeated = await service.send(operation, 'https://example.com/docs\n',
                'Future model', 'Initial effort', owner=None, http_selection=SELECTION)
            assert repeated.state == saved.state
            assert len(context.pages) == (1 if proven_unsent else 2)
            assert len(clients) == 1 and clients[0].is_closed
            assert 'private route failure detail' not in caplog.text
            if condition.startswith('close_unconfirmed'):
                late_result = await page.evaluate('''async () => {
                  try { await fetch('/backend-api/f/conversation', {
                    method:'POST', body:'{}'}); return 'escaped'; }
                  catch { return 'blocked'; }
                }''')
                assert late_result == 'blocked', escaped_requests
            assert escaped_requests == []
            if condition.startswith('close_unconfirmed'):
                await page.close()
                assert page.is_closed()
                assert not original.is_closed()
                # The retained block belongs only to the rejected page.
                another = await context.new_page()
                await another.goto('https://chatgpt.com/')
                assert await another.evaluate('''async () => {
                  await fetch('/backend-api/f/conversation', {method:'POST', body:'{}'});
                  return 'outside-rejected-page';
                }''') == 'outside-rejected-page'
                assert escaped_requests == ['POST']
        finally:
            await browser.close()
            ledger.close()
