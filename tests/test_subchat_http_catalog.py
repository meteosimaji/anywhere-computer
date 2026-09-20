"""HTTP catalog identities are dynamic, authenticated and distinct from UI labels."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from anywhere_computer.subchat_browser.catalog import project_http_catalog


@pytest.mark.parametrize('phase', ['browser', 'page'])
async def test_http_catalog_deadline_includes_startup(monkeypatch, phase):
    from anywhere_computer.subchat_browser.backend import BrowserSubchatBackend

    entered = asyncio.Event()

    async def stalled():
        entered.set()
        await asyncio.Event().wait()

    original_timeout = asyncio.timeout
    monkeypatch.setattr(asyncio, 'timeout',
                        lambda seconds: original_timeout(.02 if seconds == 20 else seconds))
    backend = BrowserSubchatBackend(stalled if phase == 'browser'
                                    else SimpleNamespace(new_page=stalled, browser=None,
                                                         on=lambda *args: None))
    task = asyncio.create_task(backend.http_catalog())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        done, _ = await asyncio.wait({task}, timeout=1)
        assert task in done, 'Startup escaped the catalog deadline'
        assert isinstance(task.exception(), TimeoutError)
        assert not backend._context_lock.locked()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def catalog():
    return {'models': [
        {'slug': 'future-chat', 'title': 'Future Chat', 'is_work_mode_model': False},
        {'slug': 'future-work', 'title': 'Future Work', 'is_work_mode_model': True},
    ], 'versions': [{'id': 'future', 'display_text': 'Future version', 'enabled': True,
                     'intelligence_presets': [
                         {'id': 7, 'title': 'Future effort', 'model_slug': 'future-chat',
                          'selected_display_version': 'Future generation',
                          'thinking_effort': 'future-effort', 'preset_type': 'available'},
                         {'id': 8, 'title': 'Work', 'model_slug': 'future-work',
                          'selected_display_version': 'Future generation',
                          'preset_type': 'available'},
                     ]}]}


def test_dynamic_catalog_excludes_work_and_preserves_unavailable_choices():
    payload = catalog()
    result = project_http_catalog(json.dumps(payload).encode())
    choice, = result['versions'][0]['choices']
    assert choice['model_slug'] == 'future-chat'
    assert choice['thinking_effort'] == 'future-effort'
    assert choice['selected_display_version'] == 'Future generation'
    assert choice['available'] is True
    assert result['submitted'] is False and result['send_requires_ui_labels'] is True
    payload['versions'][0]['enabled'] = False
    assert not project_http_catalog(json.dumps(payload).encode())['versions'][0]['choices'][0][
        'available']


@pytest.mark.parametrize('change', ['duplicate_model', 'duplicate_version', 'duplicate_preset',
                                    'unknown_model', 'missing_work_marker', 'oversized'])
def test_ambiguous_or_changed_schema_is_not_a_catalog(change):
    payload = catalog()
    if change == 'duplicate_model':
        payload['models'].append(payload['models'][0])
    elif change == 'duplicate_version':
        payload['versions'].append(payload['versions'][0])
    elif change == 'duplicate_preset':
        payload['versions'][0]['intelligence_presets'][1]['id'] = 7
    elif change == 'unknown_model':
        payload['versions'][0]['intelligence_presets'][0]['model_slug'] = 'missing'
    elif change == 'missing_work_marker':
        del payload['models'][0]['is_work_mode_model']
    raw = json.dumps(payload).encode()
    with pytest.raises(ValueError):
        project_http_catalog(raw if change != 'oversized' else b' ' * 1_048_577)


@pytest.mark.parametrize('authenticated', [True, False])
async def test_actual_http_catalog_request_without_picker_or_send(authenticated):
    from playwright.async_api import Error, async_playwright

    from anywhere_computer.subchat_browser.backend import BrowserSubchatBackend

    requests = []
    async with async_playwright() as driver:
        try:
            browser = await driver.chromium.launch(channel='chrome', headless=True)
        except Error as error:
            if 'not found' in str(error) or "doesn't exist" in str(error):
                pytest.skip('Chrome required for HTTP catalog integration')
            raise
        try:
            context = await browser.new_context()
            original = await context.new_page()

            async def route(request):
                requests.append((request.request.method, request.request.url))
                if '/backend-api/models' in request.request.url:
                    await request.fulfill(content_type='application/json',
                                          body=json.dumps(catalog()))
                else:
                    headers = '{Authorization:"Bearer test-fixture"}' if authenticated else '{}'
                    await request.fulfill(content_type='text/html', body=(
                        '<script>fetch("/backend-api/models",{headers:' + headers + '})</script>'))

            await context.route('https://chatgpt.com/**', route)
            backend = BrowserSubchatBackend(context)
            if authenticated:
                assert (await backend.http_catalog())['state'] == 'http_catalog_observed'
            else:
                with pytest.raises(ConnectionError, match='Authenticated'):
                    await backend.http_catalog()
            assert context.pages == [original] and not original.is_closed()
            assert requests == [('GET', 'https://chatgpt.com/'),
                                ('GET', 'https://chatgpt.com/backend-api/models')]
        finally:
            await browser.close()
