"""Resources must survive HTTP dispatch, ledger recovery and exact input matching."""
import json

import pytest
from test_subchat_browser_backend import HTML
from test_subchat_http_history import sample

from anywhere_computer.state import Ledger
from anywhere_computer.subchat import Subchats
from anywhere_computer.subchat_browser.backend import BrowserSubchatBackend
from anywhere_computer.subchat_browser.history import project_history, project_receipt
from anywhere_computer.subchat_browser.request_content import add_resources
from anywhere_computer.subchat_content import SubchatResources
from anywhere_computer.subchat_state import SubchatSubmissions


def resources():
    return SubchatResources.model_validate({
        'attachments': [{'id': 'file_fixture', 'name': '日本語.txt',
                         'mime_type': 'text/plain', 'size': 12}],
        'plugins': [{'label': 'Observed Plugin', 'uri': 'plugin://observed@provider',
                     'system_hint': 'plugin:observed'}],
    })


@pytest.mark.parametrize('case', ['decorated', 'wrong_target', 'wrong_label', 'newline'])
def test_generated_url_decoration_preserves_canonical_prompt(case):
    submission, history = sample()
    url = 'https://github.com/meteosimaji/anywhere-computer/pull/99'
    prompt = f'Review {url} now'
    submission = submission.model_copy(update={'prompt': prompt, 'resources': resources()})
    label = url if case != 'wrong_label' else 'another label'
    target = url if case != 'wrong_target' else 'https://example.com/other'
    text = f'Review [{label}]({target}) now'
    if case == 'newline':
        text += '\n'
    message = history['messages'][0].copy()
    message.update(content={'content_type': 'text', 'parts': [text]}, metadata={})
    payload = json.dumps({'action': 'next', 'messages': [message]})
    if case != 'decorated':
        with pytest.raises(ValueError, match='input'):
            add_resources(payload, submission)
    else:
        sent = json.loads(add_resources(payload, submission))
        assert sent['messages'][0]['content']['parts'] == [submission.wire_prompt]


@pytest.mark.parametrize('case', ['match', 'text', 'conversation', 'extra', 'missing_file',
                                  'missing_hint', 'thinking'])
def test_resource_dispatch_and_saved_history(case):
    submission, history = sample()
    submission = submission.model_copy(update={'resources': resources()})
    message = history['messages'][0].copy()
    message['metadata'] = {}
    body = {'action': 'next', 'model': 'observed', 'messages': [message]}
    if case == 'text':
        message['content'] = {'content_type': 'text', 'parts': ['unrelated draft']}
    elif case == 'conversation':
        body['conversation_id'] = 'unrelated'
    elif case == 'extra':
        body['system_hints'] = ['another-plugin']
    if case in {'text', 'conversation', 'extra'}:
        with pytest.raises(ValueError):
            add_resources(json.dumps(body), submission)
        return
    sent = json.loads(add_resources(json.dumps(body), submission))
    user, answer = history['messages']
    user['content'] = sent['messages'][0]['content']
    user['metadata'].update(sent['messages'][0]['metadata'])
    assert sent['system_hints'] == ['plugin:observed']
    assert '日本語 work' in user['content']['parts'][0]
    if case == 'missing_file':
        user['metadata']['attachments'] = []
    elif case == 'missing_hint':
        user['metadata']['system_hints'] = []
    if case in {'missing_file', 'missing_hint'}:
        with pytest.raises(ValueError, match='resources'):
            project_receipt(json.dumps(history).encode(), submission)
        return
    if case == 'thinking':
        history['messages'].remove(answer)
    payload = json.dumps(history).encode()
    assert project_receipt(payload, submission).prompt == submission.prompt
    result = project_history(payload, submission)
    assert (result is None) == (case == 'thinking')


def test_resource_identity_is_durable_and_cannot_be_reused(tmp_path):
    ledger = Ledger(tmp_path)
    store = SubchatSubmissions(ledger.connection)
    store.prepare('a' * 32, 'prompt', 'model', 'effort', owner=None, resources=resources())
    ledger.close()
    ledger = Ledger(tmp_path)
    try:
        store = SubchatSubmissions(ledger.connection)
        assert store.get('a' * 32, owner=None).resources == resources()
        with pytest.raises(ValueError, match='different arguments'):
            store.prepare('a' * 32, 'prompt', 'model', 'effort', owner=None)
    finally:
        ledger.close()


@pytest.mark.parametrize('entry', ['cli', 'mcp'])
async def test_resources_reach_persisted_submission_through_public_entry(entry, tmp_path):
    from test_subchat_lifecycle import BrowserFixture

    from anywhere_computer.models import Request
    from anywhere_computer.subchat_cli import Command, dispatch
    from anywhere_computer.subchat_mcp import session

    ledger = Ledger(tmp_path)
    service = Subchats(SubchatSubmissions(ledger.connection), BrowserFixture())
    args = {'prompt': 'read the attached file', 'model': 'observed model',
            'effort': 'observed effort', 'resources': resources().model_dump(mode='json')}
    operation_id = 'c' * 32
    try:
        if entry == 'cli':
            from anywhere_computer.subchat import SubchatOutcomeUnknown

            with pytest.raises(SubchatOutcomeUnknown):
                await dispatch(service, Command(action='send', operation_id=operation_id, **args))
        else:
            server = session(service)
            try:
                reply = await server.execute(Request(operation_id=operation_id,
                                                      tool='subchat_send', arguments=args))
                assert reply.state == 'unknown'
            finally:
                await server.close()
        saved = service.store.get(operation_id, owner=None)
        assert saved.state == 'sending'
        assert saved.resources == resources()
    finally:
        ledger.close()


@pytest.mark.parametrize(('checkpoint', 'with_resources'), [
    ('none', True), ('saved', True), ('failed', True), ('saved', False), ('failed', False),
    ('missing_account', False), ('changed_account', False),
])
async def test_browser_dispatches_resources_without_enter_or_clipboard(
        tmp_path, monkeypatch, checkpoint, with_resources):
    from playwright.async_api import Route, async_playwright

    selected_resources = resources() if with_resources else None

    script = '''<script>
    send.onclick=async()=>{
      window.sends++;
      const body={action:'next',model:'observed',messages:[{id:'user',author:{role:'user'},
        content:{content_type:'text',parts:[document.querySelector('[role=textbox]').innerText]},
        metadata:{}}]};
      const post=()=>fetch('/backend-api/f/conversation',
        {method:'POST',headers:{'chatgpt-account-id':'fixture-account'},
         body:JSON.stringify(body)});
      const response=await Promise.any([post(),post()]);
      window.wire=await response.json();
    };
    </script>'''
    if checkpoint == 'missing_account':
        script = script.replace("'chatgpt-account-id':'fixture-account'", "'unrelated':'fixture'")
    async with async_playwright() as driver:
        browser = await driver.chromium.launch(channel='chrome', headless=True)
        ledger = Ledger(tmp_path)
        try:
            context = await browser.new_context()
            bodies = []

            async def transport(route, *, post_data):
                # Intercept the final network boundary, not the application's
                # draft/Send behavior. Live service acceptance is separate.
                if checkpoint == 'saved':
                    assert store.get('b' * 32, owner=None).user_message_id == 'user'
                    assert store.get('b' * 32, owner=None).provider_account_id == 'fixture-account'
                bodies.append(json.loads(post_data))
                await route.fulfill(content_type='application/json', body=post_data)

            monkeypatch.setattr(Route, 'continue_', transport)

            async def fixture(route):
                if route.request.method == 'POST':
                    await route.abort()  # Never send a fixture to the real service.
                else:
                    await route.fulfill(content_type='text/html', body=HTML + script)

            await context.route('https://chatgpt.com/**', fixture)
            store = SubchatSubmissions(ledger.connection)

            def record(operation_id, message_id, account_id):
                if checkpoint == 'failed':
                    raise OSError('fixture storage unavailable')
                store.observe_request(operation_id, message_id, owner=None,
                                      provider_account_id=account_id)

            backend = BrowserSubchatBackend(context, http_read=True,
                record_request=record if checkpoint != 'none' else None)
            if checkpoint == 'changed_account':
                backend._http_reader._headers = {'chatgpt-account-id': 'previous-account'}
            service = Subchats(store, backend)
            if checkpoint in ('failed', 'missing_account', 'changed_account'):
                from anywhere_computer.subchat import SubchatOutcomeUnknown

                with pytest.raises(SubchatOutcomeUnknown):
                    await service.send('b' * 32, 'two lines\n日本語',
                        'Future model', 'Future effort', owner=None, resources=selected_resources)
                assert bodies == []
                assert store.get('b' * 32, owner=None).state == 'sending'
                return
            reply = await service.send('b' * 32, 'two lines\n日本語',
                'Future model', 'Future effort', owner=None, resources=selected_resources)
            assert reply.state == 'sending'  # A POST is not a saved server receipt.
            assert len(bodies) == 1
            assert bodies[0]['messages'][0]['metadata'].get('attachments', []) == (
                resources().files() if with_resources else [])
            assert bodies[0]['messages'][0]['content']['parts'] == [reply.wire_prompt]
            assert await backend.pages[reply.operation_id].evaluate('window.sends') == 1
            assert await backend.pages[reply.operation_id].evaluate('window.osWrites') == 0
            await service.send(reply.operation_id, reply.prompt, reply.model, reply.effort,
                               owner=None, resources=selected_resources)
            assert len(bodies) == 1  # No duplicate dispatch while receipt is unknown.
        finally:
            ledger.close()
            await browser.close()
