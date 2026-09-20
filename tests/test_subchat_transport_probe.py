"""Metadata observation must not retain credentials or mutate its existing tab."""
import importlib.util
import json
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    'transport_probe', Path(__file__).parents[1] / 'scripts/probe_subchat_transport.py')
assert SPEC and SPEC.loader
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


async def test_observes_real_browser_requests_without_retaining_payloads():
    from playwright.async_api import Error, async_playwright

    async with async_playwright() as driver:
        try:
            browser = await driver.chromium.launch(headless=True, channel='chrome')
        except Error as error:
            if 'not found' in str(error) or "doesn't exist" in str(error):
                pytest.skip('Chrome required for transport observation integration')
            raise
        try:
            page = await browser.new_page()
            await page.route('https://chatgpt.com/**', lambda route: route.fulfill(
                content_type=('text/event-stream' if '/backend-api/' in route.request.url
                              else 'text/html'),
                body=('data: secret-response\n\n' if '/backend-api/' in route.request.url
                      else '<title>Existing tab</title>')))
            await page.goto('https://chatgpt.com/')
            observation = probe.Observation(page)
            await page.evaluate('''async () => {
              await fetch('/backend-api/f/conversation?secret-query', {
                method:'POST', headers:{Authorization:'Bearer secret-auth'},
                body:JSON.stringify({messages:['secret-prompt']})});
              await fetch('/backend-api/unrelated-secret', {method:'POST'});
            }''')
            # Wait on the browser's transport event, not an invented answer flag.
            await page.wait_for_load_state('networkidle')
            report = observation.close()
            assert not page.is_closed() and await page.title() == 'Existing tab'
            assert [event['event'] for event in report['events']] == [
                'request', 'response', 'transport_finished']
            assert report['events'][1]['format'] == 'text/event-stream'
            assert report['pending_requests'] == []
            assert 'secret' not in json.dumps(report)
            assert report['answer_completion_verified'] is False
            count = len(report['events'])
            await page.evaluate("fetch('/backend-api/stop_conversation', {method:'POST'})")
            await page.wait_for_load_state('networkidle')
            assert len(report['events']) == count
        finally:
            await browser.close()


@pytest.mark.parametrize('endpoint,url,seconds', [
    ('https://127.0.0.1:9222', 'https://chatgpt.com/', 1),
    ('http://example.com:9222', 'https://chatgpt.com/', 1),
    ('http://127.0.0.1:9222', 'https://chatgpt.com/?secret', 1),
    ('http://127.0.0.1:9222', 'https://chatgpt.com/', 301),
])
async def test_invalid_target_rejected_before_connection(endpoint, url, seconds):
    with pytest.raises(ValueError):
        await probe.observe(endpoint, url, seconds)
