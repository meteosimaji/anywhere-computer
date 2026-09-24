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
        assert replies[0]['state'] == 'invalid_parameter'
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
                                                         ('cancel', prepared)]) + '\n' +
                      json.dumps({'action': 'list'}) + '\n')
    queued = {'action': 'queue', 'operation_id': 'c' * 32,
              'target_operation_id': completed, 'prompt': 'Review the result'}
    source = StringIO(source.getvalue() + '\n'.join(json.dumps(item) for item in
                        (queued, queued, {**queued, 'prompt': 'changed'},
                         {**queued, 'model': 'unauthorized override'})) + '\n')
    output = StringIO()
    monkeypatch.setattr(subchat_cli.sys, 'stdin', source)
    monkeypatch.setattr(subchat_cli.sys, 'stdout', output)
    import builtins

    original_import = builtins.__import__

    def no_browser_dependency(name, *args, **kwargs):
        if name == 'playwright' or name.startswith('playwright.'):
            raise ModuleNotFoundError('Browser extra is not installed')
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, '__import__', no_browser_dependency)
    await subchat_cli.run(tmp_path / 'unused-profile', tmp_path)
    replies = [json.loads(line) for line in output.getvalue().splitlines()]
    assert [item['state'] for item in replies[:3]] == ['completed', 'completed', 'cancelled']
    assert {item['operation_id'] for item in replies[3]['submissions']} == {completed, prepared}
    assert replies[1]['answer'] == 'saved result'
    assert replies[4] == replies[5]
    assert replies[4]['state'] == 'queued'
    assert replies[4]['after_operation_id'] == completed
    assert replies[4]['model'] == 'model'
    assert replies[6]['state'] == 'command_failed'
    assert replies[7]['state'] == 'invalid_parameter'
    assert replies[6]['operation_id'] == queued['operation_id']
    ledger = Ledger(tmp_path)
    try:
        saved = SubchatSubmissions(ledger.connection).get(queued['operation_id'], owner=None)
        assert saved.state == 'queued'
        assert saved.prompt == queued['prompt']
    finally:
        ledger.close()
    assert not (tmp_path / 'unused-profile').exists()


async def test_listing_paginates_owned_records_after_reopen_without_browser(tmp_path):
    from anywhere_computer.subchat_cli import ListCommand
    from anywhere_computer.subchat_state import SubchatList

    ledger = Ledger(tmp_path)
    store = SubchatSubmissions(ledger.connection)
    for index, owner in [(1, None), (2, 'other'), (3, None)]:
        store.prepare(f'{index:032x}', 'private prompt', 'observed model', 'high', owner=owner)
    ledger.close()
    ledger = Ledger(tmp_path)
    try:
        store = SubchatSubmissions(ledger.connection)
        service = Subchats(store, object())
        page = json.loads(await dispatch(service, ListCommand(action='list', limit=1)))
        assert page['submissions'][0]['operation_id'] == f'{3:032x}'
        assert 'prompt' not in page['submissions'][0]
        assert 'answer' not in page['submissions'][0]
        store.prepare('4' * 32, 'new prompt', 'model', 'high', owner=None)
        older = store.list(SubchatList(limit=1, before=page['next_before']), owner=None)
        assert [entry.operation_id for entry in older.submissions] == [f'{1:032x}']
        assert older.next_before is None
        other = store.list(SubchatList(), owner='other')
        assert [entry.operation_id for entry in other.submissions] == [f'{2:032x}']
    finally:
        ledger.close()


@pytest.mark.parametrize('fail', [False, True])
async def test_standalone_http_client_lifetime_without_browser(tmp_path, monkeypatch, fail):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    import playwright.async_api

    from anywhere_computer import subchat_cli

    events = []

    class Client:
        async def dispose(self):
            events.append('dispose')

    async def new_context(**kwargs):
        assert kwargs == {}  # No browser cookies, storage or credential persistence.
        events.append('create')
        return Client()

    @asynccontextmanager
    async def runtime():
        events.append('runtime')
        try:
            # Deliberately has no chromium: creating an HTTP client must not launch it.
            yield SimpleNamespace(request=SimpleNamespace(new_context=new_context))
        finally:
            events.append('runtime_closed')

    async def commands(service, source, destination, *, owner=None):
        factory = service.backend._http_reader._request_factory
        first = await factory()
        assert await factory() is first
        if fail:
            raise RuntimeError('fixture command failure')

    monkeypatch.setattr(playwright.async_api, 'async_playwright', runtime)
    monkeypatch.setattr(subchat_cli, 'process_lines', commands)
    if fail:
        with pytest.raises(RuntimeError, match='fixture command failure'):
            await subchat_cli.run(tmp_path / 'unused-profile', tmp_path, http_read=True)
    else:
        await subchat_cli.run(tmp_path / 'unused-profile', tmp_path, http_read=True)
    assert events == ['runtime', 'create', 'dispose', 'runtime_closed']


async def test_run_scopes_json_commands_to_ledger_owner(tmp_path, monkeypatch):
    from io import StringIO

    from anywhere_computer import subchat_cli

    ledger = Ledger(tmp_path)
    store = SubchatSubmissions(ledger.connection)
    local, owned = 'a' * 32, 'b' * 32
    store.prepare(local, 'local', 'model', 'effort', owner=None)
    store.prepare(owned, 'owned', 'model', 'effort', owner='grant-1')
    ledger.close()

    commands = [{'action': 'list'},
                {'action': 'status', 'operation_id': owned},
                {'action': 'status', 'operation_id': local}]
    output = StringIO()
    monkeypatch.setattr(subchat_cli.sys, 'stdin', StringIO(
        ''.join(json.dumps(item) + '\n' for item in commands)))
    monkeypatch.setattr(subchat_cli.sys, 'stdout', output)
    await subchat_cli.run(tmp_path / 'unused-profile', tmp_path, ledger_owner='grant-1')

    replies = [json.loads(line) for line in output.getvalue().splitlines()]
    assert [entry['operation_id'] for entry in replies[0]['submissions']] == [owned]
    assert replies[1]['operation_id'] == owned
    assert replies[2]['state'] == 'unknown_operation'


async def test_run_binds_http_observation_callbacks_to_ledger_owner(tmp_path, monkeypatch):
    from anywhere_computer import subchat_cli

    operation = 'c' * 32
    ledger = Ledger(tmp_path)
    store = SubchatSubmissions(ledger.connection)
    store.prepare(operation, 'owned', 'model', 'effort', owner='grant-1')
    store.begin_send(operation, owner='grant-1')
    ledger.close()

    async def inspect(service, _source, _destination, *, owner):
        assert owner == 'grant-1'
        assert service.backend._owner == owner
        service.backend._record_request(operation, 'user-1', 'account-1')
        saved = service.store.get(operation, owner=owner)
        assert saved.user_message_id == 'user-1'
        assert saved.provider_account_id == 'account-1'

    monkeypatch.setattr(subchat_cli, 'process_lines', inspect)
    await subchat_cli.run(tmp_path / 'unused-profile', tmp_path, http_read=True,
                          ledger_owner='grant-1')


async def test_run_binds_http_only_backend_to_ledger_owner(tmp_path, monkeypatch):
    from anywhere_computer import subchat_cli

    async def inspect(service, _source, _destination, *, owner):
        assert owner == 'grant-1'
        assert service.backend._owner == owner

    monkeypatch.setattr(subchat_cli, 'process_lines', inspect)
    await subchat_cli.run(None, tmp_path, http_only=True, ledger_owner='grant-1')
