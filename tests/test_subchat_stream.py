"""Bounded response identity observation; no live provider or generation calls."""
import asyncio
import json

import pytest

from anywhere_computer.state import Ledger
from anywhere_computer.subchat_browser.backend import STREAM
from anywhere_computer.subchat_state import SubchatSubmissions


async def test_stream_started_before_observer_expiry_keeps_late_response_candidate():
    playwright = pytest.importorskip('playwright.async_api')
    async with playwright.async_playwright() as driver:
        try:
            browser = await driver.chromium.launch(channel='chrome', headless=True)
        except playwright.Error as error:
            if 'not found' in str(error) or "doesn't exist" in str(error):
                pytest.skip('Chrome required for stream observation')
            raise
        try:
            page = await browser.new_page()
            candidate = asyncio.Event()
            observed = []

            def save(source, message, conversation, account):
                observed.append((message, conversation, account))
                candidate.set()

            await page.expose_binding('saveCandidate', save)
            await page.evaluate('''() => {
                const nativeTimeout = window.setTimeout;
                window.setTimeout = (callback, delay, ...args) => {
                    if (delay === 60000 && !window.expireObserver) {
                        window.expireObserver = callback;
                        return 0;
                    }
                    return nativeTimeout(callback, delay, ...args);
                };
                window.fetch = () => new Promise(resolve => {
                    window.releaseResponse = () => {
                        const response = new Response(
                            'data: {"conversation_id":' +
                            '"11111111-2222-3333-4444-555555555555"}\\n\\n',
                            {headers:{'content-type':'text/event-stream'}});
                        Object.defineProperty(response, 'url',
                            {value:'https://chatgpt.com/backend-api/f/conversation'});
                        resolve(response);
                    };
                });
            }''')
            # Start the fetch before expiring the observer, then release its
            # response afterward. Keep the independent reader timeout intact.
            assert await page.evaluate(STREAM + '''\n() => {
                observeSubchatStream('saveCandidate');
                window.pendingFetch = fetch('/unused', {
                    headers:{'chatgpt-account-id':'account'},
                    body:JSON.stringify({messages:[{id:'input'}]})});
                return Boolean(window.expireObserver && window.releaseResponse);
            }''')
            await page.evaluate('''() => {
                window.expireObserver();
                window.releaseResponse();
            }''')
            assert await page.evaluate('''async () => (await window.pendingFetch).text()''') == (
                'data: {"conversation_id":"11111111-2222-3333-4444-555555555555"}\n\n')
            await asyncio.wait_for(candidate.wait(), 3)
            assert observed == [('input', '11111111-2222-3333-4444-555555555555',
                                 'account')]
        finally:
            await browser.close()


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

                def observed(source, received, conversation, account):
                    assert source['page'] is page
                    store.observe_conversation(operation, received, conversation, owner=None,
                                               provider_account_id=account)
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
                    {headers:{'chatgpt-account-id':'account'},
                     body:JSON.stringify({messages:[{id}]})})).text()''', message)
                assert result == frames
                # Observation must not reject a response another wrapper already consumed.
                await page.evaluate('''() => { window.fetch=async()=>{
                    const response=new Response('already consumed',
                        {headers:{'content-type':'text/event-stream'}});
                    Object.defineProperty(response,'url',
                        {value:'https://chatgpt.com/backend-api/f/conversation'});
                    await response.text(); return response;
                }; }''')
                await page.evaluate(STREAM + '\nobserveSubchatStream', 'saveCandidate')
                assert await page.evaluate('''async id => (await fetch('/unused',
                    {headers:{'chatgpt-account-id':'account'},
                     body:JSON.stringify({messages:[{id}]})})).bodyUsed''', message)
                await asyncio.wait_for(done.wait(), 3)
                assert store.get(operation, owner=None).state == 'sending'
                with pytest.raises(ValueError, match='outgoing request'):
                    store.observe_conversation(
                        operation, 'wrong-input', identities[index], owner=None,
                        provider_account_id='account')
                with pytest.raises(ValueError, match='changed'):
                    store.observe_conversation(operation, message, identities[1-index], owner=None,
                                               provider_account_id='account')
                with pytest.raises(ValueError, match='outgoing request'):
                    store.observe_conversation(operation, message, identities[index], owner=None,
                                               provider_account_id='different-account')

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
