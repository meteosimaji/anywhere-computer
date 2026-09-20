import json

import pytest
from test_subchat_lifecycle import BrowserFixture

from anywhere_computer.state import Ledger
from anywhere_computer.subchat import SubchatOutcomeUnknown, Subchats
from anywhere_computer.subchat_cli import Command, dispatch
from anywhere_computer.subchat_state import SubchatSubmissions


async def test_json_commands_recover_without_repeating_send(tmp_path):
    ledger = Ledger(tmp_path)
    backend = BrowserFixture()
    service = Subchats(SubchatSubmissions(ledger.connection), backend)
    op = '7' * 32
    command = Command.model_validate_json(json.dumps({
        'action': 'send', 'operation_id': op, 'prompt': '日本語\nprint(42)',
        'model': 'observed model', 'effort': 'observed effort',
    }))
    try:
        with pytest.raises(SubchatOutcomeUnknown):
            await dispatch(service, command)
        assert json.loads(await dispatch(service, command))['state'] == 'sending'
        recovery = Command(action='recover', operation_id=op)
        assert json.loads(await dispatch(service, recovery))['state'] == 'submitted'
        backend.thinking = False
        assert json.loads(await dispatch(service, recovery))['answer'] == '42'
        assert backend.sends == 1
        with pytest.raises(ValueError, match='only an operation'):
            await dispatch(service, Command(action='recover', operation_id=op, prompt='other'))
        with pytest.raises(ValueError, match='requires prompt'):
            await dispatch(service, Command(action='send', operation_id='8' * 32))
        assert backend.sends == 1
    finally:
        ledger.close()


async def test_json_lines_continue_after_invalid_input_and_keep_unknown_identity(tmp_path):
    from io import StringIO

    from anywhere_computer.subchat_cli import process_lines

    ledger = Ledger(tmp_path)
    backend = BrowserFixture()
    service = Subchats(SubchatSubmissions(ledger.connection), backend)
    op = '6' * 32
    send = {'action': 'send', 'operation_id': op, 'prompt': 'private prompt',
            'model': 'observed model', 'effort': 'observed effort'}
    source = StringIO('\n'.join(['invalid secret', json.dumps(send), json.dumps(send),
                                json.dumps({'action': 'recover', 'operation_id': op})]) + '\n')
    destination = StringIO()
    try:
        await process_lines(service, source, destination)
        replies = [json.loads(line) for line in destination.getvalue().splitlines()]
        assert len(replies) == 4
        assert replies[0]['state'] == 'command_failed'
        assert replies[0]['operation_id'] is None
        assert 'secret' not in json.dumps(replies[0])
        assert replies[1] == {'state': 'submission_unconfirmed', 'operation_id': op,
                              'error_type': 'SubchatOutcomeUnknown', 'automatic_retry': False}
        assert replies[2]['state'] == 'sending'
        assert replies[3]['state'] == 'submitted'
        assert backend.sends == 1
    finally:
        ledger.close()


async def test_saved_commands_do_not_start_chrome(tmp_path, monkeypatch):
    from io import StringIO

    import playwright.async_api

    from anywhere_computer import subchat_cli

    def no_browser():
        raise AssertionError('Saved-state commands must not launch or focus Chrome')

    monkeypatch.setattr(playwright.async_api, 'async_playwright', no_browser)
    ledger = Ledger(tmp_path)
    store = SubchatSubmissions(ledger.connection)
    completed, prepared = 'a' * 32, 'b' * 32
    for operation in (completed, prepared):
        store.prepare(operation, 'work', 'model', 'effort', owner=None)
    store.begin_send(completed, owner=None)
    store.submitted(completed, 'chat', 'user', owner=None)
    store.complete(completed, 'answer', 'saved result', owner=None)
    ledger.close()
    source = StringIO('\n'.join(json.dumps({'action': action, 'operation_id': operation})
                               for action, operation in [('status', completed),
                                                         ('recover', completed),
                                                         ('cancel', prepared)]) + '\n')
    output = StringIO()
    monkeypatch.setattr(subchat_cli.sys, 'stdin', source)
    monkeypatch.setattr(subchat_cli.sys, 'stdout', output)
    await subchat_cli.run(tmp_path / 'unused-profile', tmp_path)
    replies = [json.loads(line) for line in output.getvalue().splitlines()]
    assert [item['state'] for item in replies] == ['completed', 'completed', 'cancelled']
    assert replies[1]['answer'] == 'saved result'
    assert not (tmp_path / 'unused-profile').exists()


@pytest.mark.parametrize('confirmed', [True, False])
async def test_browser_start_requires_minimization_before_dispatch(
    tmp_path, monkeypatch, confirmed,
):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    import playwright.async_api

    from anywhere_computer import subchat_cli
    from anywhere_computer.subchat_browser import catalog

    events = []

    class Session:
        async def send(self, method):
            assert method == 'Browser.getWindowForTarget'
            return {'windowId': 17}

        async def detach(self):
            events.append('detach')

    class Context:
        pages = [object()]

        async def new_cdp_session(self, page):
            assert page is self.pages[0]
            return Session()

        async def close(self):
            events.append('close')

    async def launch(profile, **options):
        assert options == {'channel': 'chrome', 'headless': False,
                           'args': ['--start-minimized']}
        events.append('launch')
        return Context()

    @asynccontextmanager
    async def driver():
        yield SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch))

    async def minimize(session, window):
        assert window == 17
        events.append('minimize')
        return confirmed

    async def commands(service, source, destination):
        if confirmed:
            first = await service.backend._browser()
            assert await service.backend._browser() is first
            events.append('dispatch')
        else:
            with pytest.raises(ConnectionError, match='minimization'):
                await service.backend._browser()
            assert 'close' in events

    monkeypatch.setattr(playwright.async_api, 'async_playwright', driver)
    monkeypatch.setattr(catalog, 'minimize_window', minimize)
    monkeypatch.setattr(subchat_cli, 'process_lines', commands)
    await subchat_cli.run(tmp_path / 'profile', tmp_path / 'state')
    assert events[:3] == ['launch', 'minimize', 'detach']
    assert ('dispatch' in events) is confirmed
    assert events[-1] == 'close'
