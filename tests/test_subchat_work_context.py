import pytest
from test_subchat_lifecycle import BrowserFixture

from anywhere_computer.models import Request
from anywhere_computer.state import Ledger
from anywhere_computer.subchat import Subchats
from anywhere_computer.subchat_mcp import session
from anywhere_computer.subchat_state import SubchatSubmissions, SubchatWorkContext


async def test_context_survives_restart_and_conflicting_retry_never_sends(tmp_path):
    context = {'task_id': 'f' * 32, 'device_id': 'local', 'workspace': '/example/project',
               'inputs': [{'reference': 'source.py', 'sha256': '0' * 64}]}
    ledger = Ledger(tmp_path)
    backend = BrowserFixture()
    server = session(Subchats(SubchatSubmissions(ledger.connection), backend))
    args = {'prompt': 'inspect the specified code', 'model': 'model', 'effort': 'effort',
            'work_context': context}
    try:
        sent = await server.execute(Request(operation_id='a' * 32, tool='subchat_send',
                                            arguments=args))
        assert sent.state == 'unknown'
        assert backend.receipt.prompt == args['prompt']
        assert backend.sends == 1
    finally:
        ledger.close()
    ledger = Ledger(tmp_path)
    backend = BrowserFixture()
    server = session(Subchats(SubchatSubmissions(ledger.connection), backend))
    try:
        saved = await server.execute(Request(operation_id='b' * 32, tool='subchat_status',
                                             arguments={'operation_id': 'a' * 32}))
        assert saved.data['work_context']['task_id'] == context['task_id']
        assert saved.data['work_context']['inputs'] == context['inputs']
        replay = await server.execute(Request(operation_id='a' * 32, tool='subchat_send',
                                               arguments=args))
        assert replay.data['state'] == 'sending'
        conflict = await server.execute(Request(operation_id='a' * 32, tool='subchat_send',
            arguments={**args, 'work_context': {**context, 'workspace': '/another'}}))
        assert conflict.state == 'failed'
        injected = await server.execute(Request(operation_id='c' * 32, tool='subchat_send',
            arguments={**args, 'work_context': {**context, 'owner': 'administrator'}}))
        assert injected.state == 'failed'
        assert backend.sends == 0
    finally:
        ledger.close()


def test_parent_reference_requires_same_owner_and_rejects_self(tmp_path):
    ledger = Ledger(tmp_path)
    store = SubchatSubmissions(ledger.connection)
    try:
        store.prepare('a' * 32, 'parent', 'model', 'effort', owner='first')
        context = SubchatWorkContext(task_id='f' * 32, parent_operation_id='a' * 32,
                                     device_id='local', workspace='/project')
        with pytest.raises(ValueError, match='Unknown subchat'):
            store.prepare('b' * 32, 'child', 'model', 'effort', owner='second',
                          work_context=context)
        with pytest.raises(ValueError, match='own parent'):
            store.prepare('a' * 32, 'child', 'model', 'effort', owner='first',
                          work_context=context)
        child = store.prepare('b' * 32, 'child', 'model', 'effort', owner='first',
                              work_context=context)
        assert child.work_context.parent_operation_id == 'a' * 32
    finally:
        ledger.close()
