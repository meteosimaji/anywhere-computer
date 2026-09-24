"""Local HTTP acceptance for the unintegrated one-shot generation transport."""
import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import httpx
import pytest

from anywhere_computer.state import Ledger
from anywhere_computer.subchat import SubchatAccessError, SubchatOutcomeUnknown
from anywhere_computer.subchat_http_sender import (
    HTTPFollowupParent,
    HTTPGenerationPlan,
    post_once,
)
from anywhere_computer.subchat_state import SubchatHTTPSelection, SubchatSubmissions

CHAT = '00000000-0000-0000-0000-000000000001'
SELECTION = SubchatHTTPSelection(version_id='observed', preset_id=1,
                                 model_slug='fixture-model', thinking_effort='fixture-effort')


class LocalGeneration:
    def __init__(self, response):
        self.response = response
        self.requests = []

    async def __aenter__(self):
        self.server = await asyncio.start_server(self.handle, '127.0.0.1', 0)
        self.url = ('http://127.0.0.1:' + str(self.server.sockets[0].getsockname()[1])
                    + '/backend-api/f/conversation')
        return self

    async def __aexit__(self, *_):
        self.server.close()
        await self.server.wait_closed()

    async def handle(self, reader, writer):
        try:
            head = await reader.readuntil(b'\r\n\r\n')
            lines = head.decode('ascii').split('\r\n')
            assert lines[0] == 'POST /backend-api/f/conversation HTTP/1.1'
            headers = dict(line.lower().split(': ', 1) for line in lines[1:] if ': ' in line)
            body = await reader.readexactly(int(headers['content-length']))
            self.requests.append((headers, json.loads(body)))
            status, chunks, declared_size = self.response
            writer.write(f'HTTP/1.1 {status}\r\nContent-Type: text/event-stream\r\n'
                         f'Content-Length: {declared_size}\r\nConnection: close\r\n\r\n'.encode())
            for chunk in chunks:
                writer.write(chunk)
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()


def reserved(store, operation, prompt, *, conversation=None, predecessor=None):
    store.prepare(operation, prompt, 'fixture model', 'fixture effort', owner=None,
                  conversation_id=conversation, http_selection=SELECTION)
    return store.begin_send(operation, owner=None, user_message_id='input-' + operation,
                            provider_account_id='fixture-account',
                            baseline_message_ids=(predecessor,) if predecessor else ())


@pytest.mark.parametrize('followup', [False, True])
async def test_one_post_preserves_reserved_input_account_and_candidate(tmp_path, followup):
    ledger = Ledger(tmp_path)
    try:
        store = SubchatSubmissions(ledger.connection)
        operation = ('b' if followup else 'a') * 32
        prompt = '日本語🙂 exact input'
        if followup:
            parent = 'c' * 32
            store.prepare(parent, 'parent', 'fixture model', 'fixture effort', owner=None,
                          conversation_id=CHAT, http_selection=SELECTION)
            store.begin_send(parent, owner=None, user_message_id='parent-input',
                             provider_account_id='fixture-account')
            store.submitted(parent, CHAT, 'parent-input', owner=None)
            store.complete(parent, 'parent-answer', 'done', owner=None)
            store.prepare(operation, prompt, 'fixture model', 'fixture effort', owner=None,
                          conversation_id=CHAT, after_operation_id=parent,
                          http_selection=SELECTION)
            saved = store.begin_send(operation, owner=None,
                                    user_message_id='input-' + operation,
                                    provider_account_id='fixture-account',
                                    baseline_message_ids=('parent-input',))
        else:
            saved = reserved(store, operation, prompt)
        if followup:
            with pytest.raises(ValueError, match='history-verified'):
                HTTPGenerationPlan.from_reserved(saved)
            with pytest.raises(ValueError, match='history-verified'):
                HTTPGenerationPlan.from_reserved(saved, followup_parent=HTTPFollowupParent(
                    CHAT, 'other-user', 'parent-answer'))
            with pytest.raises(ValueError, match='history-verified'):
                HTTPGenerationPlan.from_reserved(saved, followup_parent=HTTPFollowupParent(
                    CHAT, 'parent-input', 'parent-input'))
            plan = HTTPGenerationPlan.from_reserved(saved, followup_parent=HTTPFollowupParent(
                CHAT, 'parent-input', 'parent-answer'))
        else:
            plan = HTTPGenerationPlan.from_reserved(saved)
        payload = (f'data: {{"conversation_id":"{CHAT}"}}\r\n\r\n'
                   'data: [DONE]\n\n').encode()
        async with LocalGeneration(('200 OK', [payload[:7], payload[7:]], len(payload))) as api:
            observed = await post_once(plan, store=store, owner=None, url=api.url,
                                       account_id='fixture-account',
                on_conversation=lambda identity: store.observe_conversation(
                    operation, saved.user_message_id, identity, owner=None,
                    provider_account_id='fixture-account'),
                on_rejection=lambda _: pytest.fail('Accepted stream cannot be rejected'))
            assert len(api.requests) == 1
            headers, body = api.requests[0]
            assert headers['chatgpt-account-id'] == 'fixture-account'
            assert body['messages'][0]['id'] == saved.user_message_id
            assert body['messages'][0]['content']['parts'] == [prompt]
            assert body['model'] == SELECTION.model_slug
            assert body['thinking_effort'] == SELECTION.thinking_effort
            assert body.get('conversation_id') == (CHAT if followup else None)
            assert body.get('parent_message_id') == ('parent-answer' if followup else None)
            if not followup:
                assert 'conversation_id' not in body and 'parent_message_id' not in body
        assert observed.conversation_id == CHAT and observed.done_marker
        assert store.get(operation, owner=None).conversation_id == CHAT
        assert store.get(operation, owner=None).state == 'sending'
        with pytest.raises(ValueError, match='without resending'):
            store.begin_send(operation, owner=None)
    finally:
        ledger.close()


@pytest.mark.parametrize('status', ['403 Forbidden', '307 Temporary Redirect'])
async def test_refusal_or_redirect_never_retries_or_confirms(tmp_path, status):
    ledger = Ledger(tmp_path)
    try:
        store = SubchatSubmissions(ledger.connection)
        saved = reserved(store, 'd' * 32, 'one try')
        async with LocalGeneration((status, [], 0)) as api:
            expected = SubchatAccessError if status.startswith('403') else ConnectionError
            with pytest.raises(expected):
                await post_once(HTTPGenerationPlan.from_reserved(saved),
                                store=store, owner=None, url=api.url,
                                account_id='fixture-account', on_conversation=lambda _: None,
                                on_rejection=lambda code: store.observe_rejection(
                                    saved.operation_id, saved.user_message_id, code, owner=None,
                                    provider_account_id='fixture-account'))
            assert len(api.requests) == 1
        assert store.get(saved.operation_id, owner=None).state == 'sending'
        assert store.get(saved.operation_id, owner=None).generation_http_status == (
            403 if status.startswith('403') else None)
    finally:
        ledger.close()


async def test_lost_stream_keeps_candidate_without_receipt_or_retry(tmp_path):
    ledger = Ledger(tmp_path)
    try:
        store = SubchatSubmissions(ledger.connection)
        saved = reserved(store, 'e' * 32, 'stream loss')
        chunk = f'data: {{"conversation_id":"{CHAT}"}}\n\n'.encode()
        async with LocalGeneration(('200 OK', [chunk], len(chunk) + 100)) as api:
            with pytest.raises(httpx.RemoteProtocolError):
                await post_once(HTTPGenerationPlan.from_reserved(saved),
                                store=store, owner=None, url=api.url,
                                account_id='fixture-account',
                                on_conversation=lambda identity: store.observe_conversation(
                                    saved.operation_id, saved.user_message_id, identity,
                                    owner=None, provider_account_id='fixture-account'),
                                on_rejection=lambda _: pytest.fail('Accepted stream cannot reject'))
            assert len(api.requests) == 1
        current = store.get(saved.operation_id, owner=None)
        assert current.conversation_id == CHAT and current.state == 'sending'
        assert current.answer is None
    finally:
        ledger.close()


async def test_account_mismatch_and_unreserved_input_make_zero_requests(tmp_path):
    ledger = Ledger(tmp_path)
    try:
        store = SubchatSubmissions(ledger.connection)
        prepared = store.prepare('f' * 32, 'prompt', 'model', 'effort', owner=None,
                                 http_selection=SELECTION)
        with pytest.raises(ValueError, match='reserved'):
            HTTPGenerationPlan.from_reserved(prepared)
        saved = store.begin_send(prepared.operation_id, owner=None,
                                user_message_id='input', provider_account_id='fixture-account')
        async with LocalGeneration(('200 OK', [], 0)) as api:
            with pytest.raises(ValueError, match='account'):
                await post_once(HTTPGenerationPlan.from_reserved(saved),
                                store=store, owner=None, url=api.url,
                                account_id='other-account', on_conversation=lambda _: None,
                                on_rejection=lambda _: None)
            assert api.requests == []
    finally:
        ledger.close()


async def test_conflicting_conversation_candidates_stop_with_first_checkpoint(tmp_path):
    ledger = Ledger(tmp_path)
    try:
        store = SubchatSubmissions(ledger.connection)
        saved = reserved(store, '1' * 32, 'candidate conflict')
        other = '00000000-0000-0000-0000-000000000002'
        payload = (f'data: {{"conversation_id":"{CHAT}"}}\n\n'
                   f'data: {{"conversation_id":"{other}"}}\n\n').encode()
        async with LocalGeneration(('200 OK', [payload], len(payload))) as api:
            with pytest.raises(ValueError, match='Conflicting'):
                await post_once(HTTPGenerationPlan.from_reserved(saved),
                                store=store, owner=None, url=api.url,
                    account_id='fixture-account',
                    on_conversation=lambda identity: store.observe_conversation(
                        saved.operation_id, saved.user_message_id, identity, owner=None,
                        provider_account_id='fixture-account'),
                    on_rejection=lambda _: pytest.fail('Accepted stream cannot reject'))
            assert len(api.requests) == 1
        current = store.get(saved.operation_id, owner=None)
        assert current.state == 'sending' and current.conversation_id == CHAT
    finally:
        ledger.close()


async def test_separate_connections_compete_for_one_http_post(tmp_path):
    first = Ledger(tmp_path)
    second = Ledger(tmp_path)
    try:
        stores = (SubchatSubmissions(first.connection), SubchatSubmissions(second.connection))
        saved = reserved(stores[0], '2' * 32, 'one network request')
        plan = HTTPGenerationPlan.from_reserved(saved)
        payload = b'data: [DONE]\n\n'
        async with LocalGeneration(('200 OK', [payload], len(payload))) as api:
            results = await asyncio.gather(*(post_once(
                plan, store=store, owner=None, url=api.url, account_id='fixture-account',
                on_conversation=lambda _: None, on_rejection=lambda _: None)
                for store in stores), return_exceptions=True)
            assert len(api.requests) == 1
            assert sum(isinstance(result, SubchatOutcomeUnknown) for result in results) == 1
            assert sum(not isinstance(result, Exception) for result in results) == 1
        claim = first.connection.execute(
            'SELECT owner, user_message_id, account_id '
            'FROM subchat_http_dispatch_claims WHERE operation_id=?',
            (saved.operation_id,),
        ).fetchone()
        assert claim == (None, saved.user_message_id, 'fixture-account')
    finally:
        second.close()
        first.close()


async def test_committed_claim_survives_restart_without_post(tmp_path):
    first = Ledger(tmp_path)
    store = SubchatSubmissions(first.connection)
    saved = reserved(store, '3' * 32, 'crash window')
    plan = HTTPGenerationPlan.from_reserved(saved)
    assert store.claim_http_dispatch(
        plan.operation_id, owner=None, user_message_id=plan.user_message_id,
        provider_account_id=plan.account_id, prompt=plan.prompt, model_slug=plan.model_slug,
        thinking_effort=plan.thinking_effort, conversation_id=None, predecessor_id=None)
    first.close()  # Simulate exit immediately after claim, before network I/O.
    restarted = Ledger(tmp_path)
    try:
        store = SubchatSubmissions(restarted.connection)
        async with LocalGeneration(('200 OK', [], 0)) as api:
            with pytest.raises(SubchatOutcomeUnknown):
                await post_once(plan, store=store, owner=None, url=api.url,
                                account_id='fixture-account', on_conversation=lambda _: None,
                                on_rejection=lambda _: None)
            assert api.requests == []
        assert store.get(plan.operation_id, owner=None).state == 'sending'
    finally:
        restarted.close()


async def test_mismatched_plan_cannot_claim_or_post(tmp_path):
    ledger = Ledger(tmp_path)
    try:
        store = SubchatSubmissions(ledger.connection)
        saved = reserved(store, '4' * 32, 'saved prompt')
        plan = HTTPGenerationPlan.from_reserved(saved)
        async with LocalGeneration(('200 OK', [], 0)) as api:
            for changed in (replace(plan, prompt='different prompt'),
                            replace(plan, user_message_id='other input'),
                            replace(plan, model_slug='other model')):
                with pytest.raises(ValueError, match='identity'):
                    await post_once(changed, store=store, owner=None, url=api.url,
                                    account_id='fixture-account',
                                    on_conversation=lambda _: None,
                                    on_rejection=lambda _: None)
            assert api.requests == []
        assert ledger.connection.execute(
            'SELECT COUNT(*) FROM subchat_http_dispatch_claims').fetchone()[0] == 0
    finally:
        ledger.close()


def test_atomic_claim_from_concurrent_ledger_connections(tmp_path):
    setup = Ledger(tmp_path)
    saved = reserved(SubchatSubmissions(setup.connection), '5' * 32, 'concurrent claim')
    plan = HTTPGenerationPlan.from_reserved(saved)
    setup.close()
    barrier = threading.Barrier(2)

    def contend():
        ledger = Ledger(tmp_path)
        try:
            store = SubchatSubmissions(ledger.connection)
            barrier.wait(timeout=5)
            return store.claim_http_dispatch(
                plan.operation_id, owner=None, user_message_id=plan.user_message_id,
                provider_account_id=plan.account_id, prompt=plan.prompt,
                model_slug=plan.model_slug, thinking_effort=plan.thinking_effort,
                conversation_id=None, predecessor_id=None)
        finally:
            ledger.close()

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(lambda _: contend(), range(2)))
    assert sorted(results) == [False, True]


@pytest.mark.parametrize('claim_first', [False, True])
def test_preflight_failure_and_generation_claim_are_exclusive(tmp_path, claim_first):
    ledger = Ledger(tmp_path)
    try:
        store = SubchatSubmissions(ledger.connection)
        saved = reserved(store, 'b' * 32, 'claim boundary')
        plan = HTTPGenerationPlan.from_reserved(saved)

        def claim():
            return store.claim_http_dispatch(
                plan.operation_id, owner=None, user_message_id=plan.user_message_id,
                provider_account_id=plan.account_id, prompt=plan.prompt,
                model_slug=plan.model_slug, thinking_effort=plan.thinking_effort,
                conversation_id=None, predecessor_id=None)

        if claim_first:
            assert claim()
            assert not store.fail_http_before_dispatch(plan.operation_id, owner=None)
            assert store.get(plan.operation_id, owner=None).state == 'sending'
        else:
            assert store.fail_http_before_dispatch(plan.operation_id, owner=None)
            assert not store.fail_http_before_dispatch(plan.operation_id, owner=None)
            assert store.get(plan.operation_id, owner=None).state == 'preflight_failed'
            with pytest.raises(ValueError, match='identity'):
                claim()
    finally:
        ledger.close()


def test_existing_submission_database_adds_claim_table_without_reset(tmp_path):
    old = Ledger(tmp_path)
    saved = reserved(SubchatSubmissions(old.connection), '6' * 32, 'legacy submission')
    old.connection.execute('DROP TABLE subchat_http_dispatch_claims')
    old.close()
    reopened = Ledger(tmp_path)
    try:
        store = SubchatSubmissions(reopened.connection)
        assert store.get(saved.operation_id, owner=None) == saved
        assert reopened.connection.execute(
            'SELECT COUNT(*) FROM subchat_http_dispatch_claims').fetchone()[0] == 0
    finally:
        reopened.close()
