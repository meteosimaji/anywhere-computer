"""Browser adapter integration against an offline, effectful Chat DOM fixture."""
import json
from pathlib import Path

import pytest

from anywhere_computer.state import Ledger
from anywhere_computer.subchat import Subchats
from anywhere_computer.subchat_state import SubchatSubmissions


async def test_image_lookup_uses_backend_ledger_owner(tmp_path):
    from anywhere_computer.subchat_browser.backend import BrowserSubchatBackend
    from anywhere_computer.subchat_state import SubchatOperationNotFound

    ledger = Ledger(tmp_path)
    try:
        store = SubchatSubmissions(ledger.connection)
        operation = 'e' * 32
        store.prepare(operation, 'private', 'model', 'effort', owner='other-grant')

        async def no_browser():
            raise AssertionError('Owner check must precede browser access')

        backend = BrowserSubchatBackend(no_browser, store=store, owner='grant-1')
        backend.image_download_available = True
        with pytest.raises(SubchatOperationNotFound):
            await backend.download_image(operation, max_bytes=1024)
    finally:
        ledger.close()

CONVERSATION_ID = '11111111-2222-3333-4444-555555555555'

HTML = '''<button aria-pressed="true">Chat</button>
<button data-composer-navigation-target="reasoning"
 onclick="menu.hidden=false">Model</button>
<div id="menu" role="menu" tabindex="0" hidden
 onkeydown="if(event.key==='Escape')this.hidden=true">
<button data-model-picker-view-toggle="true"
 onclick="models.hidden=!models.hidden; control.hidden=!control.hidden">Models</button>
<div id="models"><button role="menuitemradio" aria-checked="true"
 onclick="models.hidden=true;control.hidden=false"><span>Future model</span></button></div>
<div id="control" data-reasoning-slider="true" tabindex="0" aria-describedby="effort" hidden
 onkeydown="if(!['ArrowLeft','ArrowRight'].includes(event.key))return;
 thumb.setAttribute('aria-valuenow',event.key==='ArrowRight'?'1':'0');
 effort.textContent=event.key==='ArrowRight'?'Future effort':'Initial effort'">
<span id="thumb" role="slider" aria-valuemin="0" aria-valuemax="1"
 aria-valuenow="1">slider</span></div><div id="effort" role="status">Future effort</div></div>
<form data-chatgpt-composer><div data-composer-markdown role="textbox"
 contenteditable="true"><p><br></p></div></form>
<button id="send">Send</button><main></main>
<script>
window.sends=0; window.osWrites=0;
Object.defineProperty(navigator.clipboard,'writeText',
 {configurable:true,value:async()=>{window.osWrites++}});
Object.defineProperty(navigator.clipboard,'write',
 {configurable:true,value:async()=>{window.osWrites++}});
send.onclick=()=>{
 window.sends++;
 window.sentText=document.querySelector('[role=textbox]').innerText;
 history.pushState({},'', '/c/11111111-2222-3333-4444-555555555555');
 document.querySelector('main').innerHTML+='<div data-turn-key="user">'+
 '<div data-user-message-bubble="true">rendered</div>'+
 '<button aria-label="Copy message">copy</button></div>';
 document.querySelector('[data-turn-key="user"] [aria-label="Copy message"]').onclick=()=>
 navigator.clipboard.writeText(window.sentText);
};
window.finish=()=>{
 document.querySelector('[data-turn-key="user"]').insertAdjacentHTML('beforeend',
 '<div data-content-search-unit-key="unit:2:assistant">'+
 '<div data-markdown-text-style="assistant-message">rendered answer</div></div>'+
 '<div class="turn-action-controls"><button aria-label="Copy">copy</button>'+
 '<button aria-label="Regenerate response">regenerate</button></div>');
 document.querySelector('[aria-label="Copy"]').onclick=()=>navigator.clipboard.write([
 new ClipboardItem({'text/plain':new Blob(['日本語 answer 42'],{type:'text/plain'})})]);
};
</script>'''


async def test_prepare_preserves_original_error_when_page_close_fails(monkeypatch):
    from anywhere_computer.subchat import SubchatAccessError
    from anywhere_computer.subchat_browser.backend import BrowserSubchatBackend
    from anywhere_computer.subchat_state import SubchatSubmission

    class Page:
        async def close(self):
            raise RuntimeError('fixture close failure')

    async def context_factory():
        return object()

    backend = BrowserSubchatBackend(context_factory)
    page = Page()

    async def new_page():
        return page

    async def fail_prepare(*args, **kwargs):
        raise SubchatAccessError(401)

    monkeypatch.setattr(backend, '_new_page', new_page)
    monkeypatch.setattr(backend, '_prepare_page', fail_prepare)
    submission = SubchatSubmission(
        operation_id='0' * 32, prompt='fixture', model='fixture', effort='fixture')
    with pytest.raises(SubchatAccessError):
        await backend.prepare(submission)
    assert submission.operation_id not in backend.pages


async def test_baseline_uses_latest_user_identity_in_current_chat_markup():
    playwright = pytest.importorskip('playwright.async_api')
    from anywhere_computer.subchat_browser.backend import BrowserSubchatBackend

    async with playwright.async_playwright() as driver:
        browser = await driver.chromium.launch(channel='chrome', headless=True)
        try:
            context = await browser.new_context()
            page = await context.new_page()
            backend = BrowserSubchatBackend(context)
            await page.set_content('''<main>
                <div data-turn-id-container="user-a"><div data-message-author-role="user"
                     data-message-id="user-a"></div></div>
                <div data-turn-id-container="assistant-a">
                     <div data-message-author-role="assistant"
                          data-message-id="assistant-a"></div></div>
            </main>''')
            assert await backend._baseline_with_last_user(page) == (
                ('user-a', 'assistant-a'), 'user-a')
            await page.locator('main').evaluate('''main => main.insertAdjacentHTML(
                'beforeend', '<div data-message-author-role="user" '
                + 'data-message-id="user-b"></div>')''')
            assert await backend._baseline_with_last_user(page) == (
                ('user-a', 'assistant-a', 'user-b'), 'user-b')
            await page.locator('main').evaluate('''main => main.insertAdjacentHTML(
                'beforeend', '<div data-message-author-role="assistant" '
                + 'data-message-id="assistant-a"></div>')''')
            with pytest.raises(ValueError, match='ambiguous'):
                await backend._baseline_with_last_user(page)
        finally:
            await browser.close()


async def test_browser_generation_stays_live_until_final_answer(tmp_path):
    playwright = pytest.importorskip('playwright.async_api')
    from anywhere_computer.subchat_browser.backend import BrowserSubchatBackend

    async with playwright.async_playwright() as driver:
        browser = await driver.chromium.launch(channel='chrome', headless=True)
        ledger = Ledger(tmp_path)
        try:
            context = await browser.new_context()
            await context.route('**/*', lambda route: route.fulfill(
                content_type='text/html; charset=utf-8', body=HTML))
            backend = BrowserSubchatBackend(context)
            service = Subchats(SubchatSubmissions(ledger.connection), backend)
            operation = 'a' * 32
            sent = await service.send(operation, 'work', 'Future model', 'Initial effort',
                                      owner=None)
            assert sent.state == 'submitted'
            assert backend.has_live_generation()
            assert (await service.recover(operation, owner=None)).state == 'submitted'
            assert backend.has_live_generation()
            await backend.pages[operation].evaluate('window.finish()')
            assert (await service.recover(operation, owner=None)).state == 'completed'
            assert not backend.has_live_generation()
        finally:
            ledger.close()
            await browser.close()


async def test_browser_prepared_route_remains_live_after_send_returns(monkeypatch):
    from anywhere_computer.subchat_browser import backend as backend_module
    from anywhere_computer.subchat_browser.backend import BrowserSubchatBackend
    from anywhere_computer.subchat_state import SubchatSubmission

    class Page:
        routed = False
        unrouted = False
        closed = False

        def is_closed(self):
            return self.closed

        async def route(self, pattern, callback):
            self.callback = callback
            self.routed = True

        async def unroute(self, pattern, callback):
            assert callback is self.callback
            self.unrouted = True

    class Request:
        method = 'POST'
        post_data = '{}'

        async def header_value(self, name):
            assert name == 'chatgpt-account-id'
            return 'account-a'

    class Route:
        request = Request()
        continued = False

        async def continue_(self, *, post_data):
            assert post_data == '{}'
            self.continued = True

    page = Page()
    route = Route()
    recorded = []

    async def unused_browser():
        raise AssertionError('Browser should not be opened by this send fixture')

    backend = BrowserSubchatBackend(unused_browser,
                                    record_request=lambda *args: recorded.append(args))
    submission = SubchatSubmission(operation_id='b' * 32, prompt='work', model='model',
                                   effort='effort', state='sending')
    backend.pages[submission.operation_id] = page

    async def dispatch(submission):
        await page.callback(route)
        return None

    monkeypatch.setattr(backend, '_send', dispatch)
    monkeypatch.setattr(backend_module, 'generation_input', lambda *args: {
        'messages': [{'id': 'message-a'}]})
    await backend.send(submission)
    assert route.continued and page.routed and page.unrouted
    assert recorded == [(submission.operation_id, 'message-a', 'account-a')]
    assert backend.has_live_generation()
    page.closed = True
    assert not backend.has_live_generation()


async def test_failed_prepare_closes_new_tabs_and_does_not_reuse_touched_tab(tmp_path):
    playwright = pytest.importorskip('playwright.async_api')
    from anywhere_computer.subchat_browser.backend import BrowserSubchatBackend

    async with playwright.async_playwright() as driver:
        browser = await driver.chromium.launch(channel='chrome', headless=True)
        ledger = Ledger(tmp_path)
        try:
            context = await browser.new_context()
            history = HTML + '''<script>document.querySelector('main').innerHTML =
              '<div data-turn-key="prior"></div>';</script>'''
            await context.route('**/*', lambda route: route.fulfill(
                content_type='text/html; charset=utf-8', body=history))
            backend = BrowserSubchatBackend(context)
            store = SubchatSubmissions(ledger.connection)
            failed = store.prepare('1' * 32, 'prompt', 'Missing model', 'Initial effort',
                                   owner=None, conversation_id=CONVERSATION_ID)
            for _ in range(2):
                with pytest.raises(ValueError, match='Requested model'):
                    await backend.prepare(failed)
                assert context.pages == []
                assert failed.operation_id not in backend.pages

            first = store.prepare('2' * 32, 'prompt', 'Future model', 'Initial effort',
                                  owner=None, conversation_id=CONVERSATION_ID)
            assert await backend.prepare(first) == ('prior',)
            owned = backend.pages[first.operation_id]
            second = store.prepare('3' * 32, 'prompt', 'Missing model', 'Initial effort',
                                   owner=None, conversation_id=CONVERSATION_ID)
            with pytest.raises(ValueError, match='Requested model'):
                await backend.prepare(second)
            assert backend.pages[first.operation_id] is owned
            assert second.operation_id not in backend.pages
            third = store.prepare('4' * 32, 'prompt', 'Future model', 'Initial effort',
                                  owner=None, conversation_id=CONVERSATION_ID)
            assert await backend.prepare(third) == ('prior',)
            assert backend.pages[third.operation_id] is not owned
            assert not owned.is_closed()
            assert len(context.pages) == 2
        finally:
            ledger.close()
            await browser.close()


async def test_existing_chat_waits_for_history_after_composer_is_ready(tmp_path):
    playwright = pytest.importorskip('playwright.async_api')
    from anywhere_computer.subchat_browser.backend import BrowserSubchatBackend

    delayed_history = HTML + '''<script>
document.querySelector('#menu').addEventListener('keydown', event => {
  if (event.key === 'Escape') setTimeout(() => {
    document.querySelector('main').innerHTML = '<div data-turn-key="prior"></div>';
  }, 250);
});
</script>'''
    async with playwright.async_playwright() as driver:
        browser = await driver.chromium.launch(channel='chrome', headless=True)
        ledger = Ledger(tmp_path)
        try:
            context = await browser.new_context()
            await context.route('**/*', lambda route: route.fulfill(
                content_type='text/html; charset=utf-8', body=delayed_history))
            backend = BrowserSubchatBackend(context)
            store = SubchatSubmissions(ledger.connection)
            submission = store.prepare('a' * 32, 'prompt', 'Future model',
                                       'Initial effort', owner=None,
                                       conversation_id=CONVERSATION_ID)
            assert await backend.prepare(submission) == ('prior',)
            assert await backend.pages[submission.operation_id].evaluate('window.sends') == 0
        finally:
            ledger.close()
            await browser.close()


async def test_existing_chat_without_history_stops_before_send(tmp_path, monkeypatch):
    playwright = pytest.importorskip('playwright.async_api')
    from anywhere_computer.subchat_browser import backend as backend_module

    monkeypatch.setattr(backend_module, 'EXISTING_HISTORY_TIMEOUT_MS', 50)
    async with playwright.async_playwright() as driver:
        browser = await driver.chromium.launch(channel='chrome', headless=True)
        ledger = Ledger(tmp_path)
        try:
            context = await browser.new_context()
            await context.route('**/*', lambda route: route.fulfill(
                content_type='text/html; charset=utf-8', body=HTML))
            backend = backend_module.BrowserSubchatBackend(context)
            store = SubchatSubmissions(ledger.connection)
            submission = store.prepare('b' * 32, 'prompt', 'Future model',
                                       'Initial effort', owner=None,
                                       conversation_id=CONVERSATION_ID)
            with pytest.raises(ValueError, match='Existing conversation history is unavailable'):
                await backend.prepare(submission)
            assert context.pages == []
            assert store.get(submission.operation_id, owner=None).state == 'prepared'
        finally:
            ledger.close()
            await browser.close()


async def test_work_handoff_uses_latest_user_when_assistant_id_follows():
    playwright = pytest.importorskip('playwright.async_api')
    from anywhere_computer.subchat_browser.backend import BrowserSubchatBackend
    from anywhere_computer.subchat_state import SubchatSubmission

    async with playwright.async_playwright() as driver:
        browser = await driver.chromium.launch(channel='chrome', headless=True)
        try:
            context = await browser.new_context()
            html = HTML + '''<script>
              document.querySelector('main').innerHTML =
                '<div data-turn-id-container="user"><div data-message-author-role="user" '+
                'data-message-id="user"><div data-user-message-bubble="true">prompt</div>'+
                '<button aria-label="Copy message">'+
                'copy</button></div></div>'+
                '<div data-turn-id-container="assistant">'+
                '<div data-message-author-role="assistant" '+
                'data-message-id="assistant"></div></div>';
              document.querySelector('[aria-label="Copy message"]').onclick = () =>
                navigator.clipboard.writeText('prompt');
              window.stayClicks = 0;
              document.body.insertAdjacentHTML('beforeend',
                '<button id="stay">Stay in Chat</button><button>Continue in Work</button>');
              stay.onclick = () => window.stayClicks++;
            </script>'''
            await context.route('**/*', lambda route: route.fulfill(
                content_type='text/html; charset=utf-8', body=html))
            page = await context.new_page()
            await page.goto('https://chatgpt.com/c/' + CONVERSATION_ID)
            submission = SubchatSubmission(operation_id='5' * 32, prompt='prompt',
                model='Future model', effort='Initial effort', state='submitted',
                requested_conversation_id=CONVERSATION_ID, conversation_id=CONVERSATION_ID,
                user_message_id='user', baseline_identity_kind='message_id')
            backend = BrowserSubchatBackend(context)
            backend.pages[submission.operation_id] = page
            assert await backend.read_answer(submission) is None
            assert await page.evaluate('window.stayClicks') == 1
        finally:
            await browser.close()


async def test_resource_recovery_ignores_assistant_ids_and_unversioned_history():
    playwright = pytest.importorskip('playwright.async_api')
    from anywhere_computer.subchat_browser.backend import BrowserSubchatBackend
    from anywhere_computer.subchat_content import SubchatResources
    from anywhere_computer.subchat_state import SubchatSubmission

    async with playwright.async_playwright() as driver:
        browser = await driver.chromium.launch(channel='chrome', headless=True)
        try:
            context = await browser.new_context()
            await context.route('**/*', lambda route: route.fulfill(
                content_type='text/html; charset=utf-8', body=HTML))
            page = await context.new_page()
            await page.goto('https://chatgpt.com/c/' + CONVERSATION_ID)
            backend = BrowserSubchatBackend(context, http_read=True)
            submission = SubchatSubmission(operation_id='6' * 32, prompt='prompt',
                model='Future model', effort='Initial effort', state='sending',
                requested_conversation_id=CONVERSATION_ID, conversation_id=CONVERSATION_ID,
                baseline_message_ids=('prior-user', 'prior-assistant'),
                baseline_identity_kind='message_id', resources=SubchatResources())
            backend.pages[submission.operation_id] = page
            observed = []

            async def receipt(_context, candidate):
                observed.append(candidate.user_message_id)
                return None

            backend._http_reader.receipt = receipt
            await page.locator('main').evaluate('''node => node.innerHTML =
                '<div data-message-author-role="user" data-message-id="prior-user"></div>'+
                '<div data-message-author-role="assistant" '+
                'data-message-id="prior-assistant"></div>'+
                '<div data-message-author-role="user" data-message-id="new-user"></div>'+
                '<div data-message-author-role="assistant" '+
                'data-message-id="new-assistant"></div>' ''')
            assert await backend.find_submission(submission) is None
            assert observed == ['new-user']
            observed.clear()
            legacy = submission.model_copy(update={'baseline_identity_kind': None})
            assert await backend.find_submission(legacy) is None
            assert observed == []
        finally:
            await browser.close()


async def test_completed_page_release_waits_for_queue_and_live_aliases():
    playwright = pytest.importorskip('playwright.async_api')
    from anywhere_computer.subchat_browser.backend import BrowserSubchatBackend
    from anywhere_computer.subchat_state import SubchatSubmission

    async with playwright.async_playwright() as driver:
        browser = await driver.chromium.launch(channel='chrome', headless=True)
        try:
            context = await browser.new_context()
            await context.route('**/*', lambda route: route.fulfill(
                content_type='text/html; charset=utf-8', body=HTML))
            page = await context.new_page()
            await page.goto('https://chatgpt.com/c/' + CONVERSATION_ID)
            backend = BrowserSubchatBackend(context)
            parent = SubchatSubmission(operation_id='a' * 32, prompt='first',
                model='Future model', effort='Initial effort', state='completed',
                requested_conversation_id=CONVERSATION_ID,
                conversation_id=CONVERSATION_ID, user_message_id='first')
            child = parent.model_copy(update={'operation_id': 'b' * 32,
                                              'state': 'sending', 'prompt': 'next'})
            backend.pages[parent.operation_id] = page
            await backend.release_completed(parent, keep_for_queue=True)
            assert backend.queue_watch_ready(child)
            assert not page.is_closed()

            backend.pages[child.operation_id] = page
            await backend.release_completed(parent, keep_for_queue=False)
            assert parent.operation_id not in backend.pages
            assert backend.pages[child.operation_id] is page
            assert not page.is_closed()

            completed_child = child.model_copy(update={'state': 'completed'})
            await backend.release_completed(completed_child, keep_for_queue=False)
            assert backend.pages == {}
            assert page.is_closed()
        finally:
            await browser.close()


@pytest.mark.parametrize('late_element', ['composer', 'menu', 'model_rows', 'control'])
async def test_prepare_waits_for_delayed_chat_ui_without_dispatch(tmp_path, late_element):
    playwright = pytest.importorskip('playwright.async_api')
    from anywhere_computer.subchat_browser.backend import BrowserSubchatBackend

    async with playwright.async_playwright() as driver:
        browser = await driver.chromium.launch(channel='chrome', headless=True)
        ledger = Ledger(tmp_path)
        try:
            context = await browser.new_context()
            if late_element == 'composer':
                delayed = '''<script>
                  const composer = document.querySelector('form');
                  composer.remove();
                  setTimeout(() => document.body.append(composer), 100);
                </script>'''
                html = HTML + delayed
            elif late_element == 'menu':
                html = HTML.replace('onclick="menu.hidden=false"',
                                    'onclick="setTimeout(()=>menu.hidden=false, 100)"')
            elif late_element == 'model_rows':
                html = HTML + '''<script>
                  models.hidden=true; control.hidden=false;
                  document.querySelector('[data-model-picker-view-toggle]').onclick=()=>
                    setTimeout(()=>{models.hidden=false; control.hidden=true}, 100);
                </script>'''
            else:
                html = HTML + '''<script>
                  models.hidden=true; control.hidden=true;
                  setTimeout(()=>control.hidden=false, 100);
                </script>'''
            await context.route('**/*', lambda route: route.fulfill(
                content_type='text/html; charset=utf-8', body=html))
            store = SubchatSubmissions(ledger.connection)
            draft = store.prepare('5' * 32, 'delayed input', 'Future model',
                                  'Initial effort', owner=None)
            backend = BrowserSubchatBackend(context)
            assert await backend.prepare(draft) == ()
            page = backend.pages[draft.operation_id]
            assert await page.evaluate('window.sends') == 0
            assert not (await page.get_by_role('textbox').inner_text()).strip()
            assert store.get(draft.operation_id, owner=None).state == 'prepared'
        finally:
            ledger.close()
            await browser.close()


@pytest.mark.parametrize('followup', [False, True])
@pytest.mark.parametrize('deferred_receipt', [False, True])
async def test_browser_send_pending_completion_and_database_recovery(
    tmp_path, monkeypatch, followup, deferred_receipt,
):
    playwright = pytest.importorskip('playwright.async_api')
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1] / 'scripts'))
    from subchat_browser_backend import BrowserSubchatBackend

    async with playwright.async_playwright() as driver:
        try:
            browser = await driver.chromium.launch(channel='chrome', headless=True)
        except playwright.Error as error:
            if 'not found' in str(error) or "doesn't exist" in str(error):
                pytest.skip('Chrome required for browser adapter integration')
            raise
        ledger = Ledger(tmp_path)
        try:
            context = await browser.new_context()
            prior = '''<script>
            document.querySelector('[aria-pressed]').remove();
            document.querySelector('main').innerHTML='<div data-turn-key="old">'+
              '<div data-user-message-bubble="true">old prompt</div>'+
              '<button aria-label="Copy message">copy</button></div>';
            </script>'''
            await context.route('**/*', lambda route: route.fulfill(
                content_type='text/html; charset=utf-8',
                body=HTML + (prior if '/c/' in route.request.url else '')))
            class DeferredBrowser(BrowserSubchatBackend):
                first_observation = deferred_receipt

                async def find_submission(self, submission):
                    if self.first_observation:
                        self.first_observation = False
                        return None
                    return await super().find_submission(submission)

            context_requests = 0

            async def context_factory():
                nonlocal context_requests
                context_requests += 1
                return context

            backend = DeferredBrowser(context_factory)
            assert context_requests == 0
            service = Subchats(SubchatSubmissions(ledger.connection), backend)
            operation = 'b' * 32
            prompt = '日本語 🚀\n```python\nprint("<tag>")\n```'
            sent = await service.send(operation, prompt, 'Future model', 'Initial effort',
                                      owner=None, conversation_id=(
                                          '11111111-2222-3333-4444-555555555555'
                                          if followup else None))
            assert sent.baseline_message_ids == (('old',) if followup else ())
            assert context_requests == 1
            if deferred_receipt:
                assert sent.state == 'sending'
                if not followup:
                    # After losing the in-memory page before identity is saved,
                    # a new adapter cannot invent the new conversation URL.
                    fresh = Subchats(SubchatSubmissions(ledger.connection),
                                     BrowserSubchatBackend(context))
                    assert (await fresh.recover(operation, owner=None)).state == 'sending'
                    assert len(context.pages) == 1
                sent = await service.recover(operation, owner=None)
            assert sent.state == 'submitted'
            assert sent.conversation_id == '11111111-2222-3333-4444-555555555555'
            page = context.pages[0]
            assert await page.evaluate('window.sentText') == prompt
            for _ in range(3):
                assert (await service.recover(operation, owner=None)).state == 'submitted'
            assert context_requests == 1
            assert not page.is_closed()
            # A provider choice is not a final answer or permission to start Work.
            for label, work_label in [('Chat に留まる', 'Work で続ける'),
                                      ('Stay in Chat', 'Continue in Work')]:
                await page.evaluate('''([label, work]) => {
                    window.workStarts=0; window.chatChoices=0;
                    const box=document.createElement('div'); box.id='choice';
                    const stay=document.createElement('button'); stay.textContent=label;
                    stay.onclick=()=>{window.chatChoices++;box.remove()};
                    const other=document.createElement('button'); other.textContent=work;
                    other.onclick=()=>window.workStarts++;
                    box.append(stay,other); document.body.append(box);
                }''', [label, work_label])
                await page.locator('#choice button').last.evaluate('node=>node.hidden=true')
                assert (await service.recover(operation, owner=None)).state == 'submitted'
                assert await page.evaluate('window.chatChoices') == 0
                await page.locator('#choice button').last.evaluate('node=>node.hidden=false')
                await page.locator('#choice button').first.evaluate(
                    'node=>node.parentNode.append(node.cloneNode(true))')
                assert (await service.recover(operation, owner=None)).state == 'submitted'
                assert await page.evaluate('window.chatChoices') == 0
                await page.locator('#choice button').last.evaluate('node=>node.remove()')
                await page.locator('main').evaluate('''node=>node.insertAdjacentHTML(
                    'beforeend','<div data-turn-key="newer"></div>')''')
                assert (await service.recover(operation, owner=None)).state == 'submitted'
                assert await page.evaluate('window.chatChoices') == 0
                await page.locator('[data-turn-key="newer"]').evaluate('node=>node.remove()')
                assert (await service.recover(operation, owner=None)).state == 'submitted'
                assert await page.evaluate('window.chatChoices') == 1
                assert await page.evaluate('window.workStarts') == 0
                assert await page.evaluate('window.sends') == 1
            # Recover an unfinished submission through a new adapter and SQLite
            # connection. The provider serves the already-saved history, without
            # replaying Send or manufacturing a completed ledger entry.
            history = await page.locator('main').evaluate('node => node.outerHTML')
            selector = '[data-turn-key="user"] [aria-label="Copy message"]'
            restored = (HTML + '<script>document.querySelector("main").outerHTML='
                        + json.dumps(history) + ';window.sentText=' + json.dumps(prompt)
                        + ';document.querySelector(' + json.dumps(selector)
                        + ').onclick=()=>navigator.clipboard.writeText(window.sentText);</script>')
            await context.route('**/*', lambda route: route.fulfill(
                content_type='text/html; charset=utf-8', body=restored))
            ledger.close()
            ledger = Ledger(tmp_path)
            service = Subchats(SubchatSubmissions(ledger.connection),
                               BrowserSubchatBackend(context))
            assert (await service.recover(operation, owner=None)).state == 'submitted'
            recovered_page = context.pages[-1]
            assert recovered_page != page
            assert await recovered_page.evaluate('window.sends') == 0
            await recovered_page.evaluate('window.finish()')
            result = await service.recover(operation, owner=None)
            assert result.state == 'completed'
            assert result.answer == '日本語 answer 42'
            assert recovered_page.is_closed()
            assert operation not in service.backend.pages
            follow = service.store.prepare('7' * 32, 'next', 'Future model',
                                           'Initial effort', owner=None,
                                           conversation_id=result.conversation_id)
            assert await service.backend.prepare(follow) == (
                ('old', 'user') if followup else ('user',))
            follow_page = service.backend.pages[follow.operation_id]
            assert follow_page is not recovered_page
            assert await follow_page.evaluate('window.sends') == 0
            assert len(context.pages) == 2
            await follow_page.locator('[role=textbox]').fill('user draft')
            with pytest.raises(ValueError, match='contains a draft'):
                await service.backend.prepare(follow)
            assert await follow_page.locator('[role=textbox]').inner_text() == 'user draft'
            assert len(context.pages) == 2
            await follow_page.locator('[role=textbox]').fill('')
            await follow_page.evaluate('''() => {
                const button=document.createElement('button');
                button.id='generating'; button.setAttribute('aria-label','Stop');
                document.body.append(button);
            }''')
            with pytest.raises(ValueError, match='is generating'):
                await service.backend.prepare(follow)
            assert len(context.pages) == 2
            await follow_page.locator('#generating').evaluate('node=>node.remove()')
            ledger.close()
            ledger = Ledger(tmp_path)
            service = Subchats(SubchatSubmissions(ledger.connection),
                               BrowserSubchatBackend(context))
            assert await service.send(operation, prompt, 'Future model', 'Initial effort',
                                      owner=None,
                                      conversation_id=sent.requested_conversation_id) == result
            assert await service.recover(operation, owner=None) == result
            assert len(context.pages) == 2
            assert await page.evaluate('window.sends') == 1
            assert await page.evaluate('window.osWrites') == 0
            assert await follow_page.evaluate('window.osWrites') == 0
            assert await follow_page.evaluate('window.sends') == 0
            await context.unroute('**/*')
            await context.route('**/*', lambda route: route.fulfill(
                content_type='text/html; charset=utf-8', body=HTML))
            # A missing model is rejected before the durable send boundary.
            rejected = 'c' * 32
            with pytest.raises(ValueError, match='Requested model'):
                await service.send(rejected, prompt, 'Unavailable model', 'Future effort',
                                   owner=None)
            assert service.store.get(rejected, owner=None).state == 'prepared'
            assert await context.pages[-1].evaluate('window.sends') == 0
            # Never submit a draft that changed between preparation and dispatch.
            prepared = service.store.prepare('d' * 32, prompt, 'Future model',
                                             'Future effort', owner=None)
            baseline = await service.backend.prepare(prepared)
            prepared = service.store.begin_send(prepared.operation_id, owner=None,
                                                baseline_message_ids=baseline)
            draft_page = context.pages[-1]
            await draft_page.get_by_role('textbox').fill('unrelated user draft')
            with pytest.raises(ValueError, match='draft changed'):
                await service.backend.send(prepared)
            assert await draft_page.evaluate('window.sends') == 0
        finally:
            ledger.close()
            await browser.close()


@pytest.mark.parametrize('changed', ['generating', 'history'])
async def test_browser_never_converts_prepared_send_to_queue(tmp_path, changed):
    playwright = pytest.importorskip('playwright.async_api')
    from anywhere_computer.subchat_browser.backend import BrowserSubchatBackend

    async with playwright.async_playwright() as driver:
        browser = await driver.chromium.launch(channel='chrome', headless=True)
        ledger = Ledger(tmp_path)
        try:
            context = await browser.new_context()
            await context.route('**/*', lambda route: route.fulfill(
                content_type='text/html; charset=utf-8', body=HTML))
            backend = BrowserSubchatBackend(context)
            store = SubchatSubmissions(ledger.connection)
            draft = store.prepare('6' * 32, 'next', 'Future model', 'Initial effort', owner=None)
            baseline = await backend.prepare(draft)
            reserved = store.begin_send(draft.operation_id, owner=None,
                                         baseline_message_ids=baseline)
            page = context.pages[0]
            if changed == 'generating':
                await page.evaluate("document.body.insertAdjacentHTML('beforeend',"
                                    "'<button aria-label=\"Stop\">Stop</button>')")
            else:
                await page.evaluate("document.querySelector('main').innerHTML="
                                    "'<div data-turn-key=\"unexpected\"></div>'")
            with pytest.raises(ValueError):
                await backend.send(reserved)
            assert await page.evaluate('window.sends') == 0
            assert not (await page.locator('[role=textbox]').inner_text()).strip()
        finally:
            ledger.close()
            await browser.close()


async def test_browser_queue_stale_target_fails_before_draft(tmp_path):
    playwright = pytest.importorskip('playwright.async_api')
    from anywhere_computer.subchat_browser.backend import BrowserSubchatBackend

    async with playwright.async_playwright() as driver:
        browser = await driver.chromium.launch(channel='chrome', headless=True)
        ledger = Ledger(tmp_path)
        try:
            context = await browser.new_context()
            history = HTML + '''<script>document.querySelector('main').innerHTML=
                '<div data-turn-key="expected"></div><div data-turn-key="newer"></div>';
                </script>'''
            await context.route('**/*', lambda route: route.fulfill(
                content_type='text/html; charset=utf-8', body=history))
            store = SubchatSubmissions(ledger.connection)
            parent = '7' * 32
            store.prepare(parent, 'first', 'Future model', 'Initial effort', owner=None,
                          conversation_id='11111111-2222-3333-4444-555555555555')
            store.begin_send(parent, owner=None)
            store.submitted(parent, '11111111-2222-3333-4444-555555555555',
                            'expected', owner=None)
            store.complete(parent, 'answer', '42', owner=None)
            service = Subchats(store, BrowserSubchatBackend(context))
            service.queue('8' * 32, parent, 'correction', owner=None)
            with pytest.raises(ValueError, match='stale'):
                await service.recover('8' * 32, owner=None)
            assert store.get('8' * 32, owner=None).state == 'queued'
            assert context.pages == []
            assert '8' * 32 not in service.backend.pages
        finally:
            ledger.close()
            await browser.close()


@pytest.mark.parametrize('manual_action', ['send', 'edit', 'delayed_click', 'delayed_enter',
                                         'delayed_submit'])
async def test_manual_send_after_draft_is_reserved_and_never_clicked_twice(tmp_path, manual_action):
    playwright = pytest.importorskip('playwright.async_api')
    from anywhere_computer.subchat import SubchatOutcomeUnknown
    from anywhere_computer.subchat_browser.backend import BrowserSubchatBackend

    class InspectPreparation(BrowserSubchatBackend):
        async def prepare(self, submission):
            baseline = await super().prepare(submission)
            page = self.pages[submission.operation_id]
            # A visible draft can be manually sent; reserve its identity first.
            assert not (await page.get_by_role('textbox').inner_text()).strip()
            return baseline

    async with playwright.async_playwright() as driver:
        browser = await driver.chromium.launch(channel='chrome', headless=True)
        ledger = Ledger(tmp_path)
        try:
            context = await browser.new_context()
            manual = '''<script>
            const editor=document.querySelector('[role=textbox]');
            const manualSend=new MutationObserver(()=>{
              if(!editor.innerText.trim())return;
              manualSend.disconnect();
              document.querySelector('#send').click();
              editor.textContent='';
            });
            manualSend.observe(editor,{childList:true,subtree:true,characterData:true});
            </script>'''
            if manual_action == 'edit':
                manual = manual.replace(
                    "document.querySelector('#send').click();\n"
                    "              editor.textContent='';",
                    "editor.textContent='user replacement';")
            elif manual_action.startswith('delayed_'):
                # The send has an effect immediately, but no DOM acknowledgement yet.
                manual = manual.replace("editor.textContent='';", '')
                delayed = '''<script>
                send.onclick=()=>{
                  window.sends++;
                  window.sentText=document.querySelector('[role=textbox]').innerText;
                };
                const form=document.querySelector('form');
                form.onsubmit=event=>{event.preventDefault();window.sends++};
                form.onkeydown=event=>{if(event.key==='Enter')window.sends++};
                window.publishManual=()=>{
                  history.pushState({},'', '/c/11111111-2222-3333-4444-555555555555');
                  document.querySelector('main').innerHTML='<div data-turn-key="user">'+
                    '<div data-user-message-bubble="true">manual send</div>'+
                    '<button aria-label="Copy message">copy</button></div>';
                  document.querySelector('[aria-label="Copy message"]').onclick=()=>
                    navigator.clipboard.writeText('manual send');
                };
                </script>'''
                if manual_action == 'delayed_enter':
                    manual = manual.replace("document.querySelector('#send').click();",
                                            "editor.dispatchEvent(new KeyboardEvent('keydown',"
                                            "{key:'Enter',bubbles:true}));")
                elif manual_action == 'delayed_submit':
                    manual = manual.replace("document.querySelector('#send').click();",
                                            "editor.closest('form').requestSubmit();")
                manual = delayed + manual
            await context.route('**/*', lambda route: route.fulfill(
                content_type='text/html; charset=utf-8', body=HTML + manual))
            service = Subchats(SubchatSubmissions(ledger.connection), InspectPreparation(context))
            operation = '9' * 32
            with pytest.raises(SubchatOutcomeUnknown):
                await service.send(operation, 'manual send', 'Future model', 'Initial effort',
                                   owner=None)
            assert service.store.get(operation, owner=None).state == 'sending'
            page = context.pages[0]
            expected_sends = 0 if manual_action == 'edit' else 1
            assert await page.evaluate('window.sends') == expected_sends
            if manual_action.startswith('delayed_'):
                assert (await service.recover(operation, owner=None)).state == 'sending'
                await page.evaluate('window.publishManual()')
            receipt = await service.recover(operation, owner=None)
            assert receipt.state == ('sending' if manual_action == 'edit' else 'submitted')
            if manual_action != 'edit':
                assert receipt.user_message_id == 'user'
            else:
                assert await page.get_by_role('textbox').inner_text() == 'user replacement'
            await service.send(operation, 'manual send', 'Future model', 'Initial effort',
                               owner=None)
            assert await page.evaluate('window.sends') == expected_sends
        finally:
            ledger.close()
            await browser.close()
