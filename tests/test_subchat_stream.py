"""Bounded response identity observation; no live provider or generation calls."""
import asyncio
import json

import pytest

from anywhere_computer.state import Ledger
from anywhere_computer.subchat_browser.backend import STREAM
from anywhere_computer.subchat_state import SubchatSubmissions


async def test_parallel_stream_candidates_survive_restart_without_acceptance(tmp_path):
    playwright = pytest.importorskip('playwright.async_api')
    ledger = Ledger(tmp_path)
    store = SubchatSubmissions(ledger.connection)
    identities = ['11111111-2222-3333-4444-555555555555',
                  '22222222-2222-3333-4444-555555555555']
    async with playwright.async_playwright() as driver:
        try:
            browser = await driver.chromium.launch(channel='chrome', headless=True)
        except playwright.Error as error:
            ledger.close()
            if 'not found' in str(error) or "doesn't exist" in str(error):
                pytest.skip('Chrome required for stream observation')
            raise
        try:
            async def run(index):
                operation = str(index + 1) * 32
                message = 'input-' + str(index)
                store.prepare(operation, 'prompt', 'model', 'effort', owner=None)
                store.begin_send(operation, owner=None)
                store.observe_request(operation, message, owner=None, provider_account_id='account')
                page = await browser.new_page()
                done = asyncio.Event()

                def observed(source, received, conversation):
                    assert source['page'] is page
                    store.observe_conversation(operation, received, conversation, owner=None)
                    done.set()

                await page.expose_binding('saveCandidate', observed)
                # Split JSON across chunks, ignore a nested decoy, retain original response.
                frames = 'data: ' + json.dumps({'text': {'conversation_id': identities[1-index]}})
                frames += '\r\n\r\ndata: ' + json.dumps({'conversation_id': identities[index]})
                frames += '\r\n\r\n'
                await page.evaluate('''text => {
                    window.fetch=async()=>{
                        const stream=new ReadableStream({start(c){
                            const data=new TextEncoder().encode(text);
                            c.enqueue(data.slice(0,17));c.enqueue(data.slice(17));c.close();
                        }});
                        const response=new Response(stream,
                            {headers:{'content-type':'text/event-stream'}});
                        Object.defineProperty(response,'url',{value:'https://chatgpt.com/backend-api/f/conversation'});
                        return response;
                    };
                }''', frames)
                await page.evaluate(STREAM + '\nobserveSubchatStream', 'saveCandidate')
                result = await page.evaluate('''async id => (await fetch('/unused',
                    {body:JSON.stringify({messages:[{id}]})})).text()''', message)
                assert result == frames
                await asyncio.wait_for(done.wait(), 3)
                assert store.get(operation, owner=None).state == 'sending'
                with pytest.raises(ValueError, match='outgoing request'):
                    store.observe_conversation(
                        operation, 'wrong-input', identities[index], owner=None)
                with pytest.raises(ValueError, match='changed'):
                    store.observe_conversation(operation, message, identities[1-index], owner=None)

            await asyncio.gather(run(0), run(1))
        finally:
            await browser.close()
            ledger.close()
    ledger = Ledger(tmp_path)
    try:
        store = SubchatSubmissions(ledger.connection)
        for index, conversation in enumerate(identities):
            saved = store.get(str(index + 1) * 32, owner=None)
            assert saved.conversation_id == conversation
            assert saved.state == 'sending'
    finally:
        ledger.close()
