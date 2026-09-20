"""Durable submission stages, independent of browser/account availability."""
import pytest

from anywhere_computer.state import Ledger
from anywhere_computer.subchat_state import SubchatSubmissions


def test_restart_retains_uncertain_send_and_completed_reply(tmp_path):
    operation = 'a' * 32
    ledger = Ledger(tmp_path)
    store = SubchatSubmissions(ledger.connection)
    try:
        store.prepare(operation, '日本語\n```code```', 'observed-model', 'observed-effort',
                      owner='peer')
        store.begin_send(operation, owner='peer', conversation_id='conversation')
    finally:
        ledger.close()
    ledger = Ledger(tmp_path)
    store = SubchatSubmissions(ledger.connection)
    try:
        assert store.get(operation, owner='peer').state == 'sending'
        with pytest.raises(ValueError, match='without resending'):
            store.begin_send(operation, owner='peer')
        with pytest.raises(ValueError, match='conversation'):
            store.submitted(operation, 'wrong', 'user', owner='peer')
        store.submitted(operation, 'conversation', 'user', owner='peer')
        store.complete(operation, 'answer', '結果42', owner='peer')
        assert store.complete(operation, 'answer', '結果42', owner='peer').state == 'completed'
        with pytest.raises(ValueError, match='cannot be replaced'):
            store.complete(operation, 'other', 'different', owner='peer')
    finally:
        ledger.close()
    ledger = Ledger(tmp_path)
    try:
        recovered = SubchatSubmissions(ledger.connection).get(operation, owner='peer')
        assert recovered.answer == '結果42'
        assert recovered.user_message_id == 'user'
    finally:
        ledger.close()


def test_submission_owner_arguments_and_stage_are_enforced(tmp_path):
    ledger = Ledger(tmp_path)
    store = SubchatSubmissions(ledger.connection)
    operation = 'b' * 32
    try:
        original = store.prepare(operation, 'prompt', 'model', 'effort', owner=None)
        assert store.prepare(operation, 'prompt', 'model', 'effort', owner=None) == original
        with pytest.raises(ValueError, match='Unknown'):
            store.get(operation, owner='remote')
        with pytest.raises(ValueError, match='Unknown'):
            store.prepare(operation, 'prompt', 'model', 'effort', owner='remote')
        with pytest.raises(ValueError, match='different arguments'):
            store.prepare(operation, 'changed', 'model', 'effort', owner=None)
        with pytest.raises(ValueError, match='send stage'):
            store.submitted(operation, 'conversation', 'user', owner=None)
        with pytest.raises(ValueError, match='before its answer'):
            store.complete(operation, 'answer', '42', owner=None)
        store.begin_send(operation, owner=None)
        with pytest.raises(ValueError, match='changed'):
            store._replace(original, original, None)
        store.submitted(operation, 'conversation', 'user', owner=None)
        with pytest.raises(ValueError, match='distinct'):
            store.complete(operation, 'user', '42', owner=None)
    finally:
        ledger.close()


def test_followup_conversation_is_reserved_before_send(tmp_path):
    ledger = Ledger(tmp_path)
    store = SubchatSubmissions(ledger.connection)
    operation = 'f' * 32
    try:
        record = store.prepare(operation, 'followup', 'model', 'effort', owner='peer',
                               conversation_id='existing')
        assert record.conversation_id == 'existing'
        with pytest.raises(ValueError, match='prepared submission'):
            store.begin_send(operation, owner='peer', conversation_id='different')
        assert store.begin_send(operation, owner='peer').conversation_id == 'existing'
        with pytest.raises(ValueError, match='different arguments'):
            store.prepare(operation, 'followup', 'model', 'effort', owner='peer',
                          conversation_id='different')
        with pytest.raises(ValueError, match='different arguments'):
            store.prepare(operation, 'followup', 'model', 'effort', owner='peer')
    finally:
        ledger.close()


def test_baseline_survives_restart_and_legacy_json_can_advance(tmp_path):
    import json

    ledger = Ledger(tmp_path)
    store = SubchatSubmissions(ledger.connection)
    operation = '7' * 32
    try:
        store.prepare(operation, 'same prompt', 'model', 'effort', owner=None,
                      conversation_id='conversation')
        with ledger.connection:
            body = json.loads(ledger.connection.execute(
                'SELECT body FROM subchat_submissions WHERE operation_id=?',
                (operation,)).fetchone()[0])
            body.pop('baseline_message_ids')
            ledger.connection.execute('UPDATE subchat_submissions SET body=? WHERE operation_id=?',
                                      (json.dumps(body), operation))
        with pytest.raises(ValueError, match='baseline'):
            store.begin_send(operation, owner=None, baseline_message_ids=('old', 'old'))
        store.begin_send(operation, owner=None, baseline_message_ids=('old',))
    finally:
        ledger.close()
    ledger = Ledger(tmp_path)
    try:
        store = SubchatSubmissions(ledger.connection)
        assert store.get(operation, owner=None).baseline_message_ids == ('old',)
        with pytest.raises(ValueError, match='predates'):
            store.submitted(operation, 'conversation', 'old', owner=None)
        assert store.submitted(operation, 'conversation', 'new', owner=None).state == 'submitted'
    finally:
        ledger.close()


@pytest.mark.parametrize('owner', [None, 'peer'])
def test_distinct_operations_cannot_claim_same_receipt_or_answer(tmp_path, owner):
    ledger = Ledger(tmp_path)
    store = SubchatSubmissions(ledger.connection)
    try:
        for key in ('1', '2', '3'):
            store.prepare(key * 32, 'same prompt', 'model', 'effort', owner=owner)
            store.begin_send(key * 32, owner=owner)
        store.submitted('1' * 32, 'chat', 'user', owner=owner)
        store.complete('1' * 32, 'answer', '42', owner=owner)
        with pytest.raises(ValueError, match='already bound'):
            store.submitted('2' * 32, 'chat', 'user', owner=owner)
        assert store.get('2' * 32, owner=owner).state == 'sending'
        store.submitted('2' * 32, 'chat', 'other-user', owner=owner)
        with pytest.raises(ValueError, match='already bound'):
            store.complete('2' * 32, 'answer', '42', owner=owner)
        assert store.get('2' * 32, owner=owner).state == 'submitted'
        store.submitted('3' * 32, 'other-chat', 'user', owner=owner)
        store.complete('3' * 32, 'answer', '42', owner=owner)
    finally:
        ledger.close()


@pytest.mark.parametrize('owner', [None, 'peer'])
def test_receipt_claim_is_atomic_across_connections(tmp_path, owner):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    barrier = Barrier(2)
    ledger = Ledger(tmp_path)
    store = SubchatSubmissions(ledger.connection)
    for operation in ('4' * 32, '5' * 32):
        store.prepare(operation, 'same', 'model', 'effort', owner=owner)
        store.begin_send(operation, owner=owner)
    ledger.close()

    def claim(operation):
        local = Ledger(tmp_path)
        try:
            submissions = SubchatSubmissions(local.connection)
            barrier.wait(timeout=5)
            try:
                submissions.submitted(operation, 'chat', 'user', owner=owner)
                return 'submitted'
            except ValueError as error:
                assert 'already bound' in str(error)
                return 'rejected'
        finally:
            local.close()

    with ThreadPoolExecutor(max_workers=2) as workers:
        assert sorted(workers.map(claim, ['4' * 32, '5' * 32])) == ['rejected', 'submitted']
    ledger = Ledger(tmp_path)
    try:
        store = SubchatSubmissions(ledger.connection)
        assert sorted(store.get(key * 32, owner=owner).state for key in ('4', '5')) == [
            'sending', 'submitted']
    finally:
        ledger.close()
