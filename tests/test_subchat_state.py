"""Durable submission stages, independent of browser/account availability."""
import json
import sqlite3

import pytest

from anywhere_computer.state import Ledger
from anywhere_computer.subchat_state import (
    SubchatAccountMismatch,
    SubchatConcurrentSend,
    SubchatSubmissions,
)


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


def test_distinct_controllers_cannot_send_to_one_conversation_concurrently(tmp_path):
    first_ledger = Ledger(tmp_path)
    second_ledger = Ledger(tmp_path)
    try:
        first = SubchatSubmissions(first_ledger.connection)
        second = SubchatSubmissions(second_ledger.connection)
        conversation = 'same-conversation'
        first.prepare('a' * 32, 'first', 'model', 'effort', owner=None,
                      conversation_id=conversation)
        second.prepare('b' * 32, 'second', 'model', 'effort', owner=None,
                       conversation_id=conversation)
        first.begin_send('a' * 32, owner=None)
        with pytest.raises(SubchatConcurrentSend, match='another active send'):
            second.begin_send('b' * 32, owner=None)
        assert second.get('b' * 32, owner=None).state == 'prepared'
        first.submitted('a' * 32, conversation, 'first-user', owner=None)
        with pytest.raises(SubchatConcurrentSend, match='another active send'):
            second.begin_send('b' * 32, owner=None)
        first.complete('a' * 32, 'first-answer', 'done', owner=None)
        assert second.begin_send('b' * 32, owner=None).state == 'sending'
    finally:
        second_ledger.close()
        first_ledger.close()


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


def test_http_input_and_account_reservation_is_atomic_and_survives_restart(tmp_path):
    ledger = Ledger(tmp_path)
    store = SubchatSubmissions(ledger.connection)
    first, second = '1' * 32, '2' * 32
    try:
        for operation in (first, second):
            store.prepare(operation, 'prompt', 'model', 'effort', owner='peer')
        reserved = store.begin_send(first, owner='peer', user_message_id='input-1',
                                    provider_account_id='account-1')
        assert (reserved.state, reserved.user_message_id,
                reserved.provider_account_id) == ('sending', 'input-1', 'account-1')
        with pytest.raises(ValueError, match='already bound'):
            store.begin_send(second, owner='peer', user_message_id='input-1',
                             provider_account_id='account-1')
        assert store.get(second, owner='peer').state == 'prepared'
        assert ledger.connection.execute(
            'SELECT count(*) FROM subchat_account_bindings').fetchone()[0] == 1
        with pytest.raises(ValueError, match='together'):
            store.begin_send(second, owner='peer', user_message_id='input-2')
        assert store.get(second, owner='peer').state == 'prepared'
    finally:
        ledger.close()
    ledger = Ledger(tmp_path)
    try:
        restored = SubchatSubmissions(ledger.connection).get(first, owner='peer')
        assert restored.state == 'sending'
        assert (restored.user_message_id, restored.provider_account_id) == (
            'input-1', 'account-1')
        with pytest.raises(ValueError, match='without resending'):
            SubchatSubmissions(ledger.connection).begin_send(
                first, owner='peer', user_message_id='input-1',
                provider_account_id='account-1')
    finally:
        ledger.close()


def test_http_queue_reservation_rejects_a_different_parent_account(tmp_path):
    ledger = Ledger(tmp_path)
    store = SubchatSubmissions(ledger.connection)
    parent, child = '3' * 32, '4' * 32
    try:
        store.prepare(parent, 'parent', 'model', 'effort', owner=None)
        store.begin_send(parent, owner=None, user_message_id='parent-user',
                         provider_account_id='account-1')
        store.submitted(parent, 'chat', 'parent-user', owner=None)
        store.complete(parent, 'answer', '42', owner=None)
        store.prepare(child, 'child', 'model', 'effort', owner=None,
                      conversation_id='chat', after_operation_id=parent)
        with pytest.raises(SubchatAccountMismatch):
            store.begin_send(child, owner=None, user_message_id='child-user',
                             provider_account_id='account-2')
        assert store.get(child, owner=None).state == 'queued'
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
            body.pop('baseline_identity_kind', None)
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
        assert store.get(operation, owner=None).baseline_identity_kind is None
        with pytest.raises(ValueError, match='predates'):
            store.submitted(operation, 'conversation', 'old', owner=None)
        assert store.submitted(operation, 'conversation', 'new', owner=None).state == 'submitted'
    finally:
        ledger.close()


def test_versioned_baseline_identity_survives_restart(tmp_path):
    ledger = Ledger(tmp_path)
    store = SubchatSubmissions(ledger.connection)
    operation = '8' * 32
    try:
        store.prepare(operation, 'prompt', 'model', 'effort', owner=None)
        with pytest.raises(ValueError, match='kind'):
            store.begin_send(operation, owner=None, baseline_identity_kind='unrecognized')
        with pytest.raises(ValueError, match='empty history'):
            store.begin_send(operation, owner=None, baseline_message_ids=('old',),
                             baseline_identity_kind='empty')
        sent = store.begin_send(operation, owner=None, baseline_message_ids=('old',),
                                baseline_identity_kind='message_id')
        assert sent.baseline_identity_kind == 'message_id'
    finally:
        ledger.close()
    ledger = Ledger(tmp_path)
    try:
        restored = SubchatSubmissions(ledger.connection).get(operation, owner=None)
        assert restored.baseline_message_ids == ('old',)
        assert restored.baseline_identity_kind == 'message_id'
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


def test_answer_settings_commit_with_answer_without_changing_legacy_json(tmp_path):
    from anywhere_computer.subchat_state import SubchatReportedSettings

    ledger = Ledger(tmp_path)
    store = SubchatSubmissions(ledger.connection)
    op = 'f' * 32
    settings = SubchatReportedSettings(model_slug='future-model')
    try:
        store.prepare(op, 'question', 'dynamic', 'dynamic', owner='parent')
        store.begin_send(op, owner='parent')
        store.submitted(op, 'conversation', 'input', owner='parent')
        ledger.connection.execute(
            "CREATE TRIGGER reject_settings BEFORE INSERT ON subchat_answer_settings "
            "BEGIN SELECT RAISE(ABORT, 'fixture failure'); END")
        with pytest.raises(sqlite3.IntegrityError, match='fixture failure'):
            store.complete(op, 'answer', 'result', owner='parent', reported_settings=settings)
        assert store.get(op, owner='parent').state == 'submitted'
        ledger.connection.execute('DROP TRIGGER reject_settings')
        store.complete(op, 'answer', 'result', owner='parent', reported_settings=settings)
        raw = ledger.connection.execute(
            'SELECT body FROM subchat_submissions WHERE operation_id=?', (op,)).fetchone()[0]
        assert 'reported_settings' not in json.loads(raw)
        with pytest.raises(ValueError, match='Unknown'):
            store.get(op, owner='child')
        assert store.get(op, owner='parent').reported_settings == settings
    finally:
        ledger.close()


def test_http_selection_survives_restart_and_queue_without_reassignment(tmp_path):
    from anywhere_computer.subchat import Subchats
    from anywhere_computer.subchat_state import SubchatHTTPSelection

    selected = SubchatHTTPSelection(version_id='observed', preset_id=7,
                                    model_slug='observed-model', thinking_effort=None)
    parent, child = 'a' * 32, 'b' * 32
    ledger = Ledger(tmp_path)
    store = SubchatSubmissions(ledger.connection)
    store.prepare(parent, 'prompt', 'UI model', 'UI effort', owner=None,
                  http_selection=selected)
    store.begin_send(parent, owner=None)
    store.submitted(parent, 'chat', 'input', owner=None)
    ledger.close()
    ledger = Ledger(tmp_path)
    try:
        store = SubchatSubmissions(ledger.connection)
        from test_subchat_lifecycle import BrowserFixture

        queued = Subchats(store, BrowserFixture()).queue(child, parent, 'next', owner=None)
        assert queued.http_selection == selected
        with pytest.raises(ValueError, match='different arguments'):
            store.prepare(parent, 'prompt', 'UI model', 'UI effort', owner=None,
                          http_selection=selected.model_copy(update={'model_slug': 'other'}))
        with pytest.raises(ValueError, match='Queue HTTP selection'):
            store.prepare('c' * 32, 'next', 'UI model', 'UI effort', owner=None,
                          conversation_id='chat', after_operation_id=parent)
        assert store.get(parent, owner=None).state == 'submitted'
    finally:
        ledger.close()
