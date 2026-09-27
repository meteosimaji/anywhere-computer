"""Real SQLite lifecycle with an effectful deterministic transport fixture."""
import asyncio

import pytest

from anywhere_computer.state import Ledger
from anywhere_computer.subchat import (
    SubchatAnswer,
    SubchatOutcomeUnknown,
    SubchatReceipt,
    Subchats,
)
from anywhere_computer.subchat_content import SubchatResources
from anywhere_computer.subchat_state import SubchatQueueRevisionConflict, SubchatSubmissions


async def test_queue_resource_edit_between_revision_and_snapshot_prepares_new_resources(
        tmp_path, monkeypatch):
    """A resource edit cannot bypass the checks used to prepare the send."""
    refs = SubchatResources.model_validate({'attachments': [{
        'id': 'file_uploaded', 'name': 'note.txt', 'mime_type': 'text/plain', 'size': 4}]})

    class PreparingBrowser:
        def __init__(self):
            self.prepared_resources = None
            self.sends = 0

        async def prepare(self, submission):
            self.prepared_resources = submission.resources
            return ()

        async def send(self, submission):
            self.sends += 1
            raise ConnectionError('Response lost after submission')

    ledger = Ledger(tmp_path)
    store = SubchatSubmissions(ledger.connection)
    browser = PreparingBrowser()
    parent, child = 'a' * 32, 'b' * 32
    try:
        store.prepare(parent, 'parent', 'model', 'effort', owner='peer',
                      conversation_id='conversation')
        store.begin_send(parent, owner='peer', user_message_id='user',
                         provider_account_id='account')
        store.submitted(parent, 'conversation', 'user', owner='peer')
        store.complete(parent, 'answer', 'done', owner='peer')
        store.prepare(child, 'child', 'model', 'effort', owner='peer',
                      conversation_id='conversation', after_operation_id=parent)
        original_revision = store.queue_revision
        edited = False

        def concurrent_edit(operation_id, *, owner):
            nonlocal edited
            if operation_id == child and not edited:
                edited = True
                monkeypatch.setattr(store, 'queue_revision', original_revision)
                store.change_queued_resources(child, owner='peer', expected_revision=0,
                                              resources=refs)
            return original_revision(operation_id, owner=owner)

        monkeypatch.setattr(store, 'queue_revision', concurrent_edit)
        with pytest.raises(SubchatOutcomeUnknown):
            await Subchats(store, browser).recover(child, owner='peer')
        assert browser.prepared_resources == refs
        assert browser.sends == 1
    finally:
        ledger.close()


async def test_queued_model_change_during_preparation_never_sends_stale_choice(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()

    class PreparingBrowser:
        def __init__(self):
            self.sends = 0

        async def prepare(self, submission):
            entered.set()
            await release.wait()
            return ()

    ledger = Ledger(tmp_path)
    store = SubchatSubmissions(ledger.connection)
    browser = PreparingBrowser()
    service = Subchats(store, browser)
    parent, child = '1' * 32, '2' * 32
    try:
        store.prepare(parent, 'parent', 'old', 'effort', owner='peer',
                      conversation_id='conversation')
        store.begin_send(parent, owner='peer', user_message_id='user',
                         provider_account_id='account')
        store.submitted(parent, 'conversation', 'user', owner='peer')
        store.complete(parent, 'answer', 'done', owner='peer')
        store.prepare(child, 'child', 'old', 'effort', owner='peer',
                      conversation_id='conversation', after_operation_id=parent)
        recovering = asyncio.create_task(service.recover(child, owner='peer'))
        await entered.wait()
        store.change_queued_model(child, owner='peer', expected_revision=0,
                                  model='new', effort='more', http_selection=None)
        release.set()
        with pytest.raises(SubchatQueueRevisionConflict):
            await recovering
        assert store.get(child, owner='peer').state == 'queued'
        assert browser.sends == 0
    finally:
        release.set()
        ledger.close()


async def test_https_cancel_during_preparation_discards_page_without_sending(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()

    class PreparingBrowser:
        def __init__(self):
            self.sends = 0
            self.discards = 0

        async def prepare(self, submission):
            entered.set()
            await release.wait()
            return ()

        async def discard_prepared(self, submission):
            self.discards += 1

        async def send(self, submission):
            self.sends += 1
            raise AssertionError('Cancelled submission was sent')

    ledger = Ledger(tmp_path)
    browser = PreparingBrowser()
    service = Subchats(SubchatSubmissions(ledger.connection), browser)
    operation = '3' * 32
    task = asyncio.create_task(service.send(operation, 'prompt', 'model', 'effort', owner='peer'))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        assert service.store.get(operation, owner='peer').state == 'prepared'
        other = Ledger(tmp_path)
        try:
            SubchatSubmissions(other.connection).cancel(operation, owner='peer')
        finally:
            other.close()
        release.set()
        result = await asyncio.wait_for(task, timeout=5)
        assert result.state == 'cancelled'
        assert browser.discards == 1
        assert browser.sends == 0
    finally:
        release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        ledger.close()


class BrowserFixture:
    def __init__(self):
        self.sends = 0
        self.receipt = None
        self.thinking = True

    async def prepare(self, submission):
        return ()

    async def send(self, submission):
        self.sends += 1
        self.receipt = SubchatReceipt(conversation_id='conversation',
                                     user_message_id='user', prompt=submission.prompt)
        raise ConnectionError('Response lost after submission')

    async def find_submission(self, submission):
        return self.receipt

    async def read_answer(self, submission):
        if self.thinking:
            return None
        return SubchatAnswer(**self.receipt.model_dump(), answer_message_id='answer', text='42')


async def test_send_loss_restart_thinking_and_answer_do_not_resend(tmp_path):
    browser = BrowserFixture()
    operation = 'c' * 32
    ledger = Ledger(tmp_path)
    try:
        service = Subchats(SubchatSubmissions(ledger.connection), browser)
        with pytest.raises(SubchatOutcomeUnknown):
            await service.send(operation, '日本語', 'dynamic model', 'dynamic effort', owner='peer')
    finally:
        ledger.close()
    ledger = Ledger(tmp_path)
    try:
        service = Subchats(SubchatSubmissions(ledger.connection), browser)
        duplicate = await service.send(operation, '日本語', 'dynamic model', 'dynamic effort',
                                       owner='peer')
        assert duplicate.state == 'sending'
        assert browser.sends == 1
        for _ in range(3):
            assert (await service.recover(operation, owner='peer')).state == 'submitted'
        browser.thinking = False
        completed = await service.recover(operation, owner='peer')
        assert completed.answer == '42'
        assert completed.state == 'completed'
        assert await service.recover(operation, owner='peer') == completed
        assert browser.sends == 1
    finally:
        ledger.close()


async def test_cancelled_sender_remains_reserved(tmp_path):
    started = asyncio.Event()

    class HangingBrowser(BrowserFixture):
        async def send(self, submission):
            self.sends += 1
            started.set()
            await asyncio.Event().wait()

    ledger = Ledger(tmp_path)
    browser = HangingBrowser()
    service = Subchats(SubchatSubmissions(ledger.connection), browser)
    try:
        task = asyncio.create_task(service.send('d' * 32, 'prompt', 'model', 'effort', owner=None))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        duplicate = await service.send('d' * 32, 'prompt', 'model', 'effort', owner=None)
        assert duplicate.state == 'sending'
        assert browser.sends == 1
    finally:
        ledger.close()


async def test_wrong_answer_identity_does_not_complete_submission(tmp_path):
    browser = BrowserFixture()
    ledger = Ledger(tmp_path)
    service = Subchats(SubchatSubmissions(ledger.connection), browser)
    operation = 'e' * 32
    try:
        with pytest.raises(SubchatOutcomeUnknown):
            await service.send(operation, 'prompt', 'model', 'effort', owner=None)
        await service.recover(operation, owner=None)
        browser.receipt = browser.receipt.model_copy(update={'user_message_id': 'other'})
        browser.thinking = False
        with pytest.raises(ValueError, match='message does not match'):
            await service.recover(operation, owner=None)
        assert service.store.get(operation, owner=None).state == 'submitted'
        assert browser.sends == 1
    finally:
        ledger.close()


async def test_preflight_failure_can_retry_without_uncertain_send(tmp_path):
    class UnavailableBrowser(BrowserFixture):
        async def prepare(self, submission):
            raise ValueError('Requested model unavailable')

    ledger = Ledger(tmp_path)
    browser = UnavailableBrowser()
    operation = '9' * 32
    service = Subchats(SubchatSubmissions(ledger.connection), browser)
    try:
        with pytest.raises(ValueError, match='model unavailable'):
            await service.send(operation, 'prompt', 'model', 'effort', owner=None)
        assert service.store.get(operation, owner=None).state == 'prepared'
        assert browser.sends == 0
        service.backend = BrowserFixture()
        with pytest.raises(SubchatOutcomeUnknown):
            await service.send(operation, 'prompt', 'model', 'effort', owner=None)
        assert service.store.get(operation, owner=None).state == 'sending'
        assert service.backend.sends == 1
    finally:
        ledger.close()


@pytest.mark.parametrize('failure', ['closed', 'authentication', 'access'])
@pytest.mark.parametrize('phase', ['prepare', 'send'])
async def test_access_diagnostics_preserved_only_before_dispatch(tmp_path, failure, phase):
    from anywhere_computer.subchat import SubchatAccessError, SubchatBrowserClosed

    error = (SubchatBrowserClosed('closed') if failure == 'closed'
             else SubchatAccessError(401 if failure == 'authentication' else 403))

    class FailingBrowser(BrowserFixture):
        async def prepare(self, submission):
            if phase == 'prepare':
                raise error
            return ()

        async def send(self, submission):
            self.sends += 1
            raise error

    ledger = Ledger(tmp_path)
    browser = FailingBrowser()
    service = Subchats(SubchatSubmissions(ledger.connection), browser)
    try:
        expected = type(error) if phase == 'prepare' else SubchatOutcomeUnknown
        with pytest.raises(expected):
            await service.send('8' * 32, 'prompt', 'model', 'effort', owner=None)
        assert service.store.get('8' * 32, owner=None).state == (
            'prepared' if phase == 'prepare' else 'sending')
        assert browser.sends == (0 if phase == 'prepare' else 1)
        if phase == 'send':
            await service.send('8' * 32, 'prompt', 'model', 'effort', owner=None)
            assert browser.sends == 1
    finally:
        ledger.close()


async def test_parallel_pending_observations_are_call_scoped_and_identity_checked(tmp_path):
    from anywhere_computer.subchat import SubchatPendingObservation

    ledger = Ledger(tmp_path)
    store = SubchatSubmissions(ledger.connection)
    identifiers = ('a' * 32, 'b' * 32)
    for operation in identifiers:
        store.prepare(operation, 'input', 'model', 'effort', owner=None)
        store.begin_send(operation, owner=None)
        store.submitted(operation, 'chat-' + operation, 'input-' + operation, owner=None)
    arrived = 0
    both = asyncio.Event()

    class Backend(BrowserFixture):
        wrong_identity = False

        async def read_answer(self, submission):
            nonlocal arrived
            arrived += 1
            if arrived >= 2:
                both.set()
            await both.wait()
            return SubchatPendingObservation(
                operation_id=identifiers[1] if self.wrong_identity else submission.operation_id,
                reason='final_not_observed' if submission.operation_id == identifiers[0]
                    else 'correlation_unavailable')

    backend = Backend()
    service = Subchats(store, backend)
    try:
        results = await asyncio.gather(*(service.recover(op, owner=None) for op in identifiers))
        assert [result.observation.operation_id for result in results] == list(identifiers)
        assert [result.observation.reason for result in results] == [
            'final_not_observed', 'correlation_unavailable']
        backend.wrong_identity = True
        with pytest.raises(ValueError, match='different subchat'):
            await service.recover(identifiers[0], owner=None)
        assert all(store.get(op, owner=None).state == 'submitted' for op in identifiers)
        assert backend.sends == 0
    finally:
        ledger.close()
