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


async def test_finalize_records_only_order_and_response_metadata_without_chrome():
    class Page:
        def on(self, *_):
            pass

        def remove_listener(self, *_):
            pass

    class Request:
        method = 'POST'

        def __init__(self, url):
            self.url = url

        @property
        def headers(self):
            raise AssertionError('Request headers must not be read')

        @property
        def post_data(self):
            raise AssertionError('Request body must not be read')

    class Response:
        def __init__(self, request):
            self.request = request
            self.status = 200
            self.headers = {'content-type': 'application/json',
                            'set-cookie': 'session=secret-cookie'}

        async def body(self):
            raise AssertionError('Finalize response body must not be read')

    observation = probe.Observation(Page())
    prepare = Request('https://chatgpt.com/backend-api/f/conversation/prepare')
    finalize = Request(
        'https://chatgpt.com/backend-api/sentinel/chat-requirements/finalize?secret-query')
    generation = Request('https://chatgpt.com/backend-api/f/conversation')
    for request in (prepare, finalize, generation):
        observation.request(request)
        observation.response(Response(request))
        observation.finished(request)
    report = await observation.close()
    assert report['events'] == [
        {'event': 'request', 'request': 1, 'path': '/backend-api/f/conversation/prepare'},
        {'event': 'response', 'request': 1, 'status': 200, 'format': 'application/json'},
        {'event': 'transport_finished', 'request': 1},
        {'event': 'request', 'request': 2,
         'path': '/backend-api/sentinel/chat-requirements/finalize'},
        {'event': 'response', 'request': 2, 'status': 200, 'format': 'application/json'},
        {'event': 'transport_finished', 'request': 2},
        {'event': 'request', 'request': 3, 'path': '/backend-api/f/conversation'},
        {'event': 'response', 'request': 3, 'status': 200, 'format': 'application/json'},
        {'event': 'transport_finished', 'request': 3},
    ]
    assert 'secret' not in json.dumps(report)
    assert report['pending_requests'] == []


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
            report = await observation.close()
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


async def test_sentinel_observation_keeps_only_required_flags():
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
            async def fulfill(route):
                if '/backend-api/sentinel/' in route.request.url:
                    await route.fulfill(content_type='application/json', body=json.dumps({
                        'prepare_token': 'secret-prepare-token',
                        'turnstile': {'required': True, 'dx': 'secret-dx'},
                        'proofofwork': {'required': True, 'seed': 'secret-seed'},
                        'so': {'required': False, 'collector_dx': 'secret-collector'},
                    }))
                else:
                    await route.fulfill(content_type='text/html',
                                        body='<title>Existing tab</title>')
            await page.route('https://chatgpt.com/**', fulfill)
            await page.goto('https://chatgpt.com/')
            observation = probe.Observation(page)
            await page.evaluate("""fetch('/backend-api/sentinel/chat-requirements/prepare', {
                method:'POST', body:JSON.stringify({p:'secret-request'})})""")
            report = await observation.close()
            response = next(event for event in report['events'] if event['event'] == 'response')
            assert response['required'] == {
                'turnstile': True, 'proofofwork': True, 'so': False}
            assert 'secret' not in json.dumps(report)
            assert not page.is_closed()
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
