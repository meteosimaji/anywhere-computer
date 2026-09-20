"""Browser adapter integration against an offline, effectful Chat DOM fixture."""
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
 onkeydown="thumb.setAttribute('aria-valuenow',event.key==='ArrowRight'?'1':'0');
 effort.textContent=event.key==='ArrowRight'?'Future effort':'Initial effort'">
<span id="thumb" role="slider" aria-valuemin="0" aria-valuemax="1"
 aria-valuenow="0">slider</span></div><div id="effort" role="status">Initial effort</div></div>
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
async def test_browser_send_pending_completion_and_database_recovery(
    tmp_path, monkeypatch, followup,
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
            document.querySelector('main').innerHTML='<div data-turn-key="old">'+
              '<div data-user-message-bubble="true">old prompt</div>'+
              '<button aria-label="Copy message">copy</button></div>';
            </script>'''
            await context.route('**/*', lambda route: route.fulfill(
                content_type='text/html; charset=utf-8',
                body=HTML + (prior if '/c/' in route.request.url else '')))
            backend = BrowserSubchatBackend(context)
            service = Subchats(SubchatSubmissions(ledger.connection), backend)
            operation = 'b' * 32
            prompt = '日本語 🚀\n```python\nprint("<tag>")\n```'
            sent = await service.send(operation, prompt, 'Future model', 'Future effort',
                                      owner=None, conversation_id=(
                                          '11111111-2222-3333-4444-555555555555'
                                          if followup else None))
            assert sent.baseline_message_ids == (('old',) if followup else ())
            assert sent.state == 'submitted'
            assert sent.conversation_id == '11111111-2222-3333-4444-555555555555'
            page = context.pages[0]
            assert await page.evaluate('window.sentText') == prompt
            for _ in range(3):
                assert (await service.recover(operation, owner=None)).state == 'submitted'
            assert not page.is_closed()
            await page.evaluate('window.finish()')
            result = await service.recover(operation, owner=None)
            assert result.state == 'completed'
            assert result.answer == '日本語 answer 42'
            ledger.close()
            ledger = Ledger(tmp_path)
            service = Subchats(SubchatSubmissions(ledger.connection),
                               BrowserSubchatBackend(context))
            assert await service.send(operation, prompt, 'Future model', 'Future effort',
                                      owner=None,
                                      conversation_id=sent.requested_conversation_id) == result
            assert await service.recover(operation, owner=None) == result
            assert len(context.pages) == 1
            assert await page.evaluate('window.sends') == 1
            assert await page.evaluate('window.osWrites') == 0
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
