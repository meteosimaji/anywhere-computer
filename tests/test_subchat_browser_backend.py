"""Browser adapter integration against an offline, effectful Chat DOM fixture."""
import json
from pathlib import Path

import pytest

from anywhere_computer.state import Ledger
from anywhere_computer.subchat import Subchats
from anywhere_computer.subchat_state import SubchatSubmissions

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

            backend = DeferredBrowser(context)
            service = Subchats(SubchatSubmissions(ledger.connection), backend)
            operation = 'b' * 32
            prompt = '日本語 🚀\n```python\nprint("<tag>")\n```'
            sent = await service.send(operation, prompt, 'Future model', 'Initial effort',
                                      owner=None, conversation_id=(
                                          '11111111-2222-3333-4444-555555555555'
                                          if followup else None))
            assert sent.baseline_message_ids == (('old',) if followup else ())
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
            assert not page.is_closed()
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
            assert await recovered_page.evaluate('window.osWrites') == 0
            assert await recovered_page.evaluate('window.sends') == 0
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
            await service.backend.prepare(prepared)
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
            assert await page.locator('[role=textbox]').inner_text() == 'next'
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
            assert await context.pages[0].evaluate('window.sends') == 0
            assert not (await context.pages[0].locator('[role=textbox]').inner_text()).strip()
        finally:
            ledger.close()
            await browser.close()
