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
@pytest.mark.parametrize('http_read', [True, False])
async def test_actual_http_catalog_request_without_picker_or_send(authenticated, http_read):
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
            backend = BrowserSubchatBackend(context, http_read=http_read)
            if authenticated:
                result = await backend.http_catalog()
                assert result['state'] == 'http_catalog_observed'
                assert result['http_selection_send_supported'] is http_read
                assert result['generation_transport'] == 'browser_prepared'
            else:
                with pytest.raises(ConnectionError, match='Authenticated'):
                    await backend.http_catalog()
            assert context.pages == [original] and not original.is_closed()
            assert requests == [('GET', 'https://chatgpt.com/'),
                                ('GET', 'https://chatgpt.com/backend-api/models')]
        finally:
            await browser.close()


@pytest.mark.parametrize(('change', 'field', 'reason'), [
    ('none', None, None), ('version', 'version_id', 'not_found'),
    ('preset', 'preset_id', 'not_found'), ('model', 'model_slug', 'mismatch'),
    ('effort', 'thinking_effort', 'mismatch'),
    ('disabled', 'preset_id', 'unavailable'), ('work', 'preset_id', 'not_found'),
])
def test_exact_http_selection_is_revalidated(change, field, reason):
    from anywhere_computer.subchat_browser.catalog import require_http_selection
    from anywhere_computer.subchat_state import SubchatHTTPSelection, SubchatSelectionError

    payload = catalog()
    selected = SubchatHTTPSelection.model_validate(project_http_catalog(
        json.dumps(payload).encode())['versions'][0]['choices'][0]['http_selection'])
    if change in ('version', 'preset', 'model', 'effort'):
        selected = selected.model_copy(update={
            {'version': 'version_id', 'preset': 'preset_id',
             'model': 'model_slug', 'effort': 'thinking_effort'}[change]:
            -1 if change == 'preset' else 'different'})
    elif change == 'disabled':
        payload['versions'][0]['enabled'] = False
    elif change == 'work':
        payload['models'][0]['is_work_mode_model'] = True
    projected = project_http_catalog(json.dumps(payload).encode())
    if change == 'none':
        require_http_selection(projected, selected)
    else:
        with pytest.raises(SubchatSelectionError) as caught:
            require_http_selection(projected, selected)
        assert (caught.value.field, caught.value.reason) == (field, reason)


@pytest.mark.parametrize('preset_id', ['7', 7.0, True])
def test_http_selection_does_not_coerce_preset_id(preset_id):
    from pydantic import ValidationError

    from anywhere_computer.subchat_state import SubchatHTTPSelection

    with pytest.raises(ValidationError):
        SubchatHTTPSelection.model_validate({'version_id': 'future',
            'preset_id': preset_id, 'model_slug': 'future-chat', 'thinking_effort': None})


async def test_http_send_without_selection_does_not_open_browser(tmp_path):
    from anywhere_computer.state import Ledger
    from anywhere_computer.subchat import SubchatPreparationFailed, Subchats
    from anywhere_computer.subchat_browser.backend import BrowserSubchatBackend
    from anywhere_computer.subchat_state import SubchatSubmissions

    async def forbidden():
        pytest.fail('Missing selection must reject before browser work')

    ledger = Ledger(tmp_path)
    try:
        service = Subchats(SubchatSubmissions(ledger.connection),
                           BrowserSubchatBackend(forbidden, http_read=True))
        with pytest.raises(SubchatPreparationFailed, match='http_selection'):
            await service.send('a' * 32, 'prompt', 'UI model', 'UI effort', owner=None)
        saved = service.store.get('a' * 32, owner=None)
        assert saved.state == 'prepared'
        assert 'http_selection' not in json.loads(ledger.connection.execute(
            'SELECT body FROM subchat_submissions').fetchone()[0])
    finally:
        ledger.close()


async def test_cli_catalog_returns_reusable_selection_without_submission(tmp_path):
    from io import StringIO

    from anywhere_computer.state import Ledger
    from anywhere_computer.subchat import Subchats
    from anywhere_computer.subchat_cli import process_lines
    from anywhere_computer.subchat_state import SubchatSubmissions

    class Reader:
        async def http_catalog(self):
            return project_http_catalog(json.dumps(catalog()).encode())

    ledger = Ledger(tmp_path)
    try:
        output = StringIO()
        await process_lines(Subchats(SubchatSubmissions(ledger.connection), Reader()),
                            StringIO('{"action":"catalog"}\n'), output)
        reply = json.loads(output.getvalue())
        assert reply['versions'][0]['choices'][0]['http_selection'] == {
            'version_id': 'future', 'preset_id': 7, 'model_slug': 'future-chat',
            'thinking_effort': 'future-effort'}
        assert ledger.connection.execute(
            'SELECT count(*) FROM subchat_submissions').fetchone()[0] == 0
    finally:
        ledger.close()


@pytest.mark.parametrize('entry', ['cli', 'mcp'])
async def test_legacy_parent_cannot_create_undispatchable_http_queue(tmp_path, entry):
    from anywhere_computer.models import Request
    from anywhere_computer.state import Ledger
    from anywhere_computer.subchat import SubchatPreparationFailed, Subchats
    from anywhere_computer.subchat_browser.backend import BrowserSubchatBackend
    from anywhere_computer.subchat_cli import QueueCommand, dispatch
    from anywhere_computer.subchat_mcp import session
    from anywhere_computer.subchat_state import SubchatSubmissions

    async def forbidden():
        pytest.fail('Invalid queue must not open a browser')

    ledger = Ledger(tmp_path)
    store = SubchatSubmissions(ledger.connection)
    parent, child = 'a' * 32, 'b' * 32
    store.prepare(parent, 'old input', 'model', 'effort', owner=None)
    store.begin_send(parent, owner=None)
    store.submitted(parent, 'chat', 'input', owner=None)
    service = Subchats(store, BrowserSubchatBackend(forbidden, http_read=True))
    try:
        if entry == 'cli':
            with pytest.raises(SubchatPreparationFailed, match='http_selection'):
                await dispatch(service, QueueCommand(action='queue', operation_id=child,
                    target_operation_id=parent, prompt='follow-up'))
        else:
            server = session(service)
            try:
                reply = await server.execute(Request(operation_id=child, tool='subchat_message',
                    arguments={'mode': 'queue', 'target_operation_id': parent,
                               'prompt': 'follow-up'}))
                assert reply.state == 'failed'
                assert reply.data['error_code'] == 'preparation_failed'
            finally:
                await server.close()
        assert ledger.connection.execute(
            'SELECT operation_id FROM subchat_submissions').fetchall() == [(parent,)]
    finally:
        ledger.close()
