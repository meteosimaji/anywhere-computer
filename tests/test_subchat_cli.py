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
