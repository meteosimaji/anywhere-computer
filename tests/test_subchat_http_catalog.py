"""HTTP catalog identities are dynamic, authenticated and distinct from UI labels."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from anywhere_computer.subchat_browser.catalog import (
    compare_http_and_ui_catalog,
    decode_choice_id,
    project_http_catalog,
)


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
    selected, model, effort = decode_choice_id(choice['choice_id'])
    assert selected.model_dump() == choice['http_selection']
    assert (model, effort) == ('Future Chat', 'Future effort')
    payload['versions'][0]['enabled'] = False
    assert not project_http_catalog(json.dumps(payload).encode())['versions'][0]['choices'][0][
        'available']


def test_latest_pro_uses_catalog_version_picker_label_not_wire_model_title():
    from anywhere_computer.subchat_browser.catalog import require_http_selection
    from anywhere_computer.subchat_state import SubchatHTTPSelection

    payload = catalog()
    payload['models'][0]['slug'] = 'gpt-6-pro'
    payload['models'][0]['title'] = 'GPT-6 Pro'
    payload['versions'][0]['id'] = 'latest'
    payload['versions'][0]['display_text'] = '最新'
    payload['versions'][0]['intelligence_presets'][0].update({
        'id': 3, 'title': 'Pro', 'model_slug': 'gpt-6-pro', 'thinking_effort': None,
    })
    observed = project_http_catalog(json.dumps(payload).encode())
    choice = observed['versions'][0]['choices'][0]
    assert choice['available'] is True
    assert choice['availability_basis'] == 'authenticated_http_catalog'
    assert choice['generation_sendability'] == 'unknown'
    selection = SubchatHTTPSelection.model_validate(choice['http_selection'])
    assert require_http_selection(observed, selection, model='GPT-6 Pro',
                                  effort='Pro') == '最新'


def test_catalog_comparison_keeps_ui_pickability_separate_from_generation():
    payload = catalog()
    projected = project_http_catalog(json.dumps(payload).encode())
    version = projected['versions'][0]
    ui = {'state': 'catalog_partial', 'models': [
        {'label': version['label'], 'disabled': False},
    ]}
    matched = compare_http_and_ui_catalog(projected, ui)
    choice = matched['versions'][0]['choices'][0]
    assert choice['ui_picker_status'] == 'selectable_row_observed'
    assert choice['ui_picker_label'] == version['label']
    assert choice['generation_sendability'] == 'unknown'
    assert matched['generation_http_verified'] is False
    missing = compare_http_and_ui_catalog(projected, {'state': 'catalog_partial',
        'models': [{'label': version['label'], 'disabled': True}]})
    assert missing['versions'][0]['choices'][0]['ui_picker_status'] == 'not_confirmed'
    unavailable = compare_http_and_ui_catalog(projected,
        {'state': 'catalog_unavailable', 'reason': 'browser_challenge',
         'failure_stage': 'navigation', 'http_status': 403})
    assert unavailable['versions'][0]['choices'][0]['ui_picker_status'] == 'unknown'
    assert unavailable['ui_reason'] == 'browser_challenge'
    assert unavailable['ui_failure_stage'] == 'navigation'
    assert unavailable['ui_http_status'] == 403


def test_pro_choice_cannot_use_same_version_sol_picker_row():
    from anywhere_computer.subchat_browser.catalog import resolve_picker_label

    rows = [{'label': 'GPT-5.6 Sol', 'disabled': False}]
    with pytest.raises(ValueError, match='not available'):
        resolve_picker_label(rows, version_label='5.6', version_id='5.6',
                             model_title='GPT-5.6 Pro')
    assert resolve_picker_label(rows, version_label='5.6', version_id='5.6',
                                model_title='GPT-5.6 Sol') == 'GPT-5.6 Sol'
    rows.append({'label': 'GPT-5.6 Pro', 'disabled': False})
    assert resolve_picker_label(rows, version_label='5.6', version_id='5.6',
                                model_title='GPT-5.6 Pro') == 'GPT-5.6 Pro'


async def test_subchat_catalog_compare_reads_both_without_sending(tmp_path):
    from anywhere_computer.models import Request
    from anywhere_computer.state import Ledger
    from anywhere_computer.subchat import Subchats
    from anywhere_computer.subchat_browser.backend import BrowserSubchatBackend
    from anywhere_computer.subchat_mcp import session
    from anywhere_computer.subchat_state import SubchatSubmissions

    async def forbidden():
        pytest.fail('Comparison must use the explicit catalog observations')

    payload = catalog()
    http = project_http_catalog(json.dumps(payload).encode())
    version_label = http['versions'][0]['label']
    calls: list[str] = []

    async def observed_http():
        calls.append('http')
        return http

    async def observed_ui(model):
        assert model is None
        calls.append('ui')
        return {'state': 'catalog_partial', 'models': [
            {'label': version_label, 'disabled': False}], 'submitted': False}

    ledger = Ledger(tmp_path)
    try:
        backend = BrowserSubchatBackend(forbidden, http_read=True)
        backend.http_catalog = observed_http
        backend.catalog = observed_ui
        controller = session(Subchats(SubchatSubmissions(ledger.connection), backend),
                             observe_catalog=observed_ui,
                             observe_http_catalog=observed_http)
        try:
            reply = await controller.execute(Request(
                operation_id='a' * 32, tool='subchat_catalog',
                arguments={'source': 'compare'}))
            assert reply.state == 'completed', reply
            assert calls == ['http', 'ui']
            assert reply.data['versions'][0]['choices'][0]['ui_picker_status'] == (
                'selectable_row_observed')
            assert reply.data['generation_http_verified'] is False
        finally:
            await controller.close()
    finally:
        ledger.close()


def test_pro_moved_to_explicit_version_uses_current_version_label():
    from anywhere_computer.subchat_browser.catalog import require_http_selection
    from anywhere_computer.subchat_state import SubchatHTTPSelection

    payload = catalog()
    payload['models'][0].update(slug='gpt-6-pro', title='GPT-6 Pro')
    payload['versions'][0].update(id='6', display_text='GPT-6')
    payload['versions'][0]['intelligence_presets'][0].update(
        id=19, title='Pro', model_slug='gpt-6-pro', thinking_effort=None)
    observed = project_http_catalog(json.dumps(payload).encode())
    selected = SubchatHTTPSelection.model_validate(
        observed['versions'][0]['choices'][0]['http_selection'])
    assert require_http_selection(observed, selected, model='GPT-6 Pro',
                                  effort='Pro') == 'GPT-6'


def test_stale_latest_choice_is_rejected_after_pro_moves():
    from anywhere_computer.subchat_browser.catalog import require_http_selection
    from anywhere_computer.subchat_state import SubchatHTTPSelection, SubchatSelectionError

    payload = catalog()
    payload['models'][0].update(slug='gpt-6-pro', title='GPT-6 Pro')
    payload['versions'][0].update(id='latest', display_text='最新')
    payload['versions'][0]['intelligence_presets'][0].update(
        id=3, title='Pro', model_slug='gpt-6-pro', thinking_effort=None)
    stale = SubchatHTTPSelection.model_validate(project_http_catalog(
        json.dumps(payload).encode())['versions'][0]['choices'][0]['http_selection'])
    payload['versions'][0]['intelligence_presets'][0].update(
        model_slug='gpt-7-pro')
    payload['models'].append({'slug': 'gpt-7-pro', 'title': 'GPT-7 Pro',
                              'is_work_mode_model': False})
    payload['versions'].append({**payload['versions'][0], 'id': '6',
                                'display_text': 'GPT-6',
                                'intelligence_presets': [{**payload['versions'][0][
                                    'intelligence_presets'][0], 'model_slug': 'gpt-6-pro'}]})
    fresh = project_http_catalog(json.dumps(payload).encode())
    with pytest.raises(SubchatSelectionError) as error:
        require_http_selection(fresh, stale, model='GPT-6 Pro', effort='Pro')
    assert (error.value.field, error.value.reason) == ('model_slug', 'mismatch')


def test_picker_resolves_localized_version_row_and_rejects_ambiguity():
    from anywhere_computer.subchat_browser.catalog import resolve_picker_label

    rows = [{'label': name, 'disabled': False} for name in
            ('最新', 'GPT-5.6 Sol', 'GPT-5.5', 'GPT-6')]
    assert resolve_picker_label(rows, version_label='最新', version_id='latest',
                                model_title='GPT-6 Pro') == '最新'
    with pytest.raises(ValueError, match='not available'):
        resolve_picker_label(rows, version_label='5.6', version_id='5.6',
                             model_title='GPT-5.6 Pro')
    assert resolve_picker_label(rows, version_label='6', version_id='6',
                                model_title='GPT-6 Pro') == 'GPT-6'
    with pytest.raises(ValueError, match='not available'):
        resolve_picker_label(rows + [{'label': 'GPT-6 Thinking', 'disabled': False}],
                             version_label='6', version_id='6', model_title='GPT-6 Pro')


@pytest.mark.parametrize('choice_id', ['ac1.', 'ac1.bad!', 'ac1.W10', 'other', 'ac1.' + 'x' * 8192])
def test_invalid_choice_id_rejected_without_guessing(choice_id):
    with pytest.raises(ValueError, match='choice ID'):
        decode_choice_id(choice_id)


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


@pytest.mark.parametrize(('model', 'effort', 'field'), [
    ('最新', 'Instant', 'model'),
    ('GPT-5.6 Sol', 'Thinking', 'effort'),
])
def test_http_choice_labels_bind_to_selected_model_and_preset(model, effort, field):
    from anywhere_computer.subchat_browser.catalog import require_http_selection
    from anywhere_computer.subchat_state import SubchatHTTPSelection, SubchatSelectionError

    payload = catalog()
    payload['versions'][0]['id'] = 'latest'
    payload['versions'][0]['display_text'] = '最新'
    payload['versions'][0]['intelligence_presets'][0]['title'] = 'Instant'
    payload['models'][0]['title'] = 'GPT-5.6 Sol'
    payload['versions'].append({**payload['versions'][0], 'id': '5.6',
                                'display_text': 'GPT-5.6 Sol'})
    projected = project_http_catalog(json.dumps(payload).encode())
    selected = SubchatHTTPSelection.model_validate(
        projected['versions'][0]['choices'][0]['http_selection'])
    with pytest.raises(SubchatSelectionError) as caught:
        require_http_selection(projected, selected, model=model, effort=effort)
    assert (caught.value.field, caught.value.reason) == (field, 'mismatch')
    require_http_selection(projected, selected, model='GPT-5.6 Sol', effort='Instant')


def test_http_model_title_can_differ_from_version_label():
    from anywhere_computer.subchat_browser.catalog import require_http_selection
    from anywhere_computer.subchat_state import SubchatHTTPSelection, SubchatSelectionError

    projected = project_http_catalog(json.dumps(catalog()).encode())
    selected = SubchatHTTPSelection.model_validate(
        projected['versions'][0]['choices'][0]['http_selection'])
    require_http_selection(projected, selected, model='Future Chat', effort='Future effort')
    with pytest.raises(SubchatSelectionError) as caught:
        require_http_selection(projected, selected, model='Future version',
                               effort='Future effort')
    assert (caught.value.field, caught.value.reason) == ('model', 'mismatch')


def test_send_schema_identifies_http_model_title_and_effort_title():
    from anywhere_computer.subchat_mcp import direct_gateway_catalog

    send, = [tool for tool in direct_gateway_catalog() if tool['name'] == 'subchat_send']
    assert 'model_title' in send['inputSchema']['properties']['model']['description']
    assert 'version label' in send['inputSchema']['properties']['model']['description']
    assert 'title' in send['inputSchema']['properties']['effort']['description']
    assert 'model_title' in send['description']


def test_bundled_subchat_guidance_uses_http_choice_fields():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    sources = (
        'plugins/anywhere-computer/skills/subchat/SKILL.md',
        'plugins/anywhere-computer/skills/computer-work/SKILL.md',
        'docs/SUBCHAT-PROBE.md',
    )
    for source in sources:
        guidance = (root / source).read_text(encoding='utf-8')
        assert '`source=http`' in guidance, source
        assert '`model_title`' in guidance, source
        assert '`title`' in guidance, source
        assert '`http_selection`' in guidance, source
        assert '`source=ui`' in guidance, source


async def test_browser_rejects_mismatched_http_choice_before_opening_page(tmp_path):
    from anywhere_computer.models import Request
    from anywhere_computer.state import Ledger
    from anywhere_computer.subchat import Subchats
    from anywhere_computer.subchat_browser.backend import BrowserSubchatBackend
    from anywhere_computer.subchat_mcp import session
    from anywhere_computer.subchat_state import (
        SubchatHTTPSelection,
        SubchatSelectionError,
        SubchatSubmissions,
    )

    payload = catalog()
    payload['versions'][0]['id'] = 'latest'
    payload['versions'][0]['display_text'] = '5.6'
    payload['versions'][0]['intelligence_presets'][0]['title'] = 'Instant'
    payload['models'][0]['title'] = 'GPT-5.6 Sol'
    payload['versions'].append({**payload['versions'][0], 'id': '5.6',
                                'display_text': 'GPT-5.6 Sol'})
    selection = SubchatHTTPSelection.model_validate(project_http_catalog(
        json.dumps(payload).encode())['versions'][0]['choices'][0]['http_selection'])

    async def forbidden():
        pytest.fail('Mismatched labels must not open a browser page')

    ledger = Ledger(tmp_path)
    try:
        backend = BrowserSubchatBackend(forbidden, http_read=True)

        async def observed_catalog():
            return project_http_catalog(json.dumps(payload).encode())

        backend.http_catalog = observed_catalog
        service = Subchats(SubchatSubmissions(ledger.connection), backend)
        with pytest.raises(SubchatSelectionError) as caught:
            await service.send('a' * 32, 'prompt', '5.6', 'Instant', owner=None,
                               http_selection=selection)
        assert (caught.value.field, caught.value.reason) == ('model', 'mismatch')
        assert service.store.get('a' * 32, owner=None).state == 'prepared'
        server = session(service)
        try:
            reply = await server.execute(Request(operation_id='b' * 32, tool='subchat_send',
                arguments={'prompt': 'prompt', 'model': '5.6', 'effort': 'Instant',
                           'http_selection': selection.model_dump()}))
            assert reply.state == 'failed'
            assert reply.data == {
                'error_code': 'invalid_parameter', 'field': 'model', 'reason': 'mismatch',
                'dispatched': False, 'corrected_request_requires_new_operation_id': True}
            assert 'model_title' in (reply.error or '')
            assert service.store.get('b' * 32, owner=None).state == 'prepared'
        finally:
            await server.close()
    finally:
        ledger.close()


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
