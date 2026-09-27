"""Queued resource replacement preserves the unsent and owner boundaries."""

import pytest
from test_subchat_lifecycle import BrowserFixture

from anywhere_computer.models import Request
from anywhere_computer.state import Ledger
from anywhere_computer.subchat import SubchatOutcomeUnknown, Subchats
from anywhere_computer.subchat_content import SubchatResources
from anywhere_computer.subchat_mcp import session
from anywhere_computer.subchat_state import SubchatSubmissions


async def test_resources_change_cas_owner_and_send_boundary(tmp_path):
    ledger = Ledger(tmp_path)
    store = SubchatSubmissions(ledger.connection)
    browser = BrowserFixture()
    parent, child = 'a' * 32, 'b' * 32
    store.prepare(parent, 'parent', 'model', 'effort', owner='peer',
                  conversation_id='conversation')
    store.begin_send(parent, owner='peer', user_message_id='user',
                     provider_account_id='account')
    store.submitted(parent, 'conversation', 'user', owner='peer')
    store.complete(parent, 'answer', 'done', owner='peer')
    store.prepare(child, 'child', 'model', 'effort', owner='peer',
                  conversation_id='conversation', after_operation_id=parent)
    server = session(Subchats(store, browser), owner='peer')
    other = session(Subchats(store, browser), owner='other')
    refs = {'attachments': [{'id': 'file_uploaded', 'name': 'note.txt',
                             'mime_type': 'text/plain', 'size': 4}]}
    try:
        async def change(s, request_id, revision, resources):
            return await s.execute(Request(operation_id=request_id, tool=
                'subchat_queue_resources_change', arguments={
                    'operation_id': child, 'expected_revision': revision,
                    'resources': resources}))

        forbidden = await change(other, '1' * 32, 0, refs)
        assert forbidden.state == 'failed'
        bad_path = await change(server, '2' * 32, 0, {'attachments': [
            {'id': '/tmp/note.txt', 'name': 'note.txt',
             'mime_type': 'text/plain', 'size': 4}]})
        assert bad_path.state == 'failed'
        assert store.queue_revision(child, owner='peer') == 0
        changed = await change(server, '3' * 32, 0, refs)
        assert changed.state == 'completed'
        assert changed.data['queue_revision'] == 1
        assert store.get(child, owner='peer').resources.attachments[0].id == 'file_uploaded'
        # A lost response to the original queue request is safe to retry after
        # an explicit resource edit under the same operation ID.
        retried = store.prepare(child, 'child', 'model', 'effort', owner='peer',
                                conversation_id='conversation',
                                after_operation_id=parent)
        assert retried.resources.attachments[0].id == 'file_uploaded'
        assert browser.sends == 0
        stale = await change(server, '4' * 32, 0, {})
        assert stale.data['error_code'] == 'queue_revision_conflict'
        cleared = await change(server, '5' * 32, 1, {})
        assert cleared.state == 'completed' and cleared.data['queue_revision'] == 2
        assert store.get(child, owner='peer').resources is None
        store.cancel(child, owner='peer')
        after_cancel = await change(server, '6' * 32, 2, refs)
        assert after_cancel.state == 'failed'
        assert browser.sends == 0
    finally:
        await server.close()
        await other.close()
        ledger.close()


async def test_cleared_queue_uses_plain_send_path(tmp_path):
    class PlainOnlyBrowser(BrowserFixture):
        async def prepare(self, submission):
            if submission.resources is not None:
                raise ValueError('Resource sends require HTTP history verification')
            return ()

    ledger = Ledger(tmp_path)
    store = SubchatSubmissions(ledger.connection)
    browser = PlainOnlyBrowser()
    parent, child = 'c' * 32, 'd' * 32
    try:
        store.prepare(parent, 'parent', 'model', 'effort', owner='peer',
                      conversation_id='conversation')
        store.begin_send(parent, owner='peer', user_message_id='user',
                         provider_account_id='account')
        store.submitted(parent, 'conversation', 'user', owner='peer')
        store.complete(parent, 'answer', 'done', owner='peer')
        store.prepare(child, 'child', 'model', 'effort', owner='peer',
                      conversation_id='conversation', after_operation_id=parent)
        refs = SubchatResources.model_validate({'attachments': [{
            'id': 'file_uploaded', 'name': 'note.txt', 'mime_type': 'text/plain', 'size': 4}]})
        store.change_queued_resources(child, owner='peer', expected_revision=0,
                                      resources=refs)
        store.change_queued_resources(child, owner='peer', expected_revision=1,
                                      resources=SubchatResources())
        with pytest.raises(SubchatOutcomeUnknown):
            await Subchats(store, browser).recover(child, owner='peer')
        assert browser.sends == 1
        assert store.get(child, owner='peer').resources is None
    finally:
        ledger.close()
