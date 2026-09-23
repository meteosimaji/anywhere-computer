"""Local HTTP acceptance for the unintegrated one-shot generation transport."""
import asyncio
import json

import httpx
import pytest

from anywhere_computer.state import Ledger
from anywhere_computer.subchat import SubchatAccessError
from anywhere_computer.subchat_http_sender import HTTPGenerationPlan, post_once
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
        plan = HTTPGenerationPlan.from_reserved(saved)
        payload = (f'data: {{"conversation_id":"{CHAT}"}}\r\n\r\n'
                   'data: [DONE]\n\n').encode()
        async with LocalGeneration(('200 OK', [payload[:7], payload[7:]], len(payload))) as api:
            observed = await post_once(plan, url=api.url, account_id='fixture-account',
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
            assert body.get('parent_message_id') == ('parent-input' if followup else None)
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
                await post_once(HTTPGenerationPlan.from_reserved(saved), url=api.url,
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
                await post_once(HTTPGenerationPlan.from_reserved(saved), url=api.url,
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
                await post_once(HTTPGenerationPlan.from_reserved(saved), url=api.url,
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
                await post_once(HTTPGenerationPlan.from_reserved(saved), url=api.url,
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
