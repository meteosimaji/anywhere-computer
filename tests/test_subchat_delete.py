"""Real loopback HTTP checks for the bound, one-shot Chat deletion path."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest
from playwright.async_api import async_playwright

from anywhere_computer.state import Ledger
from anywhere_computer.subchat import Subchats
from anywhere_computer.subchat_browser.http_reader import ChatHTTPReader
from anywhere_computer.subchat_cli import DeleteCommand, dispatch
from anywhere_computer.subchat_delete import (
    DeleteRequest,
    DeletionLedger,
    SubchatDeletionUnknown,
    delete_saved,
)
from anywhere_computer.subchat_http import HTTPOnlySubchatBackend
from anywhere_computer.subchat_http_session import ObservedHTTPSession
from anywhere_computer.subchat_mcp import session as mcp_session
from anywhere_computer.subchat_state import SubchatAccountMismatch, SubchatSubmissions

CHAT = '00000000-0000-0000-0000-000000000001'
OP = 'a' * 32
USER = 'fixture-input'
ACCOUNT = 'fixture-account'
SECRET = 'Bearer fixture-secret'
PROMPT = 'disposable local test'


class Server(ThreadingHTTPServer):
    def __init__(self, *, mismatch=False, patch_status=200, post_status=404):
        self.calls = []
        self.mismatch = mismatch
        self.patch_status = patch_status
        self.post_status = post_status
        self.deleted = False
        super().__init__(('127.0.0.1', 0), Handler)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def _reply(self, status, data):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.server.calls.append(('GET', self.path))
        assert self.path == '/backend-api/conversations/' + CHAT
        assert self.headers['Authorization'] == SECRET
        assert self.headers['Chatgpt-Account-Id'] == ACCOUNT
        if self.server.deleted:
            self._reply(self.server.post_status, {'detail': 'not found'})
        else:
            self._reply(200, {'conversation_id': CHAT, 'messages': [{
                'id': USER, 'author': {'role': 'user'},
                'content': {'content_type': 'text',
                            'parts': ['wrong' if self.server.mismatch else PROMPT]},
                'metadata': {}, 'status': 'finished_successfully'}]})

    def do_PATCH(self):
        self.server.calls.append(('PATCH', self.path))
        assert self.path == '/backend-api/conversation/' + CHAT
        assert self.headers['Authorization'] == SECRET
        assert self.headers['Chatgpt-Account-Id'] == ACCOUNT
        assert self.headers['Content-Type'] == 'application/json'
        assert json.loads(self.rfile.read(int(self.headers['Content-Length']))) == {
            'is_visible': False}
        if self.server.patch_status == 200:
            self.server.deleted = True
            self._reply(200, {'success': True})
        else:
            self._reply(self.server.patch_status, {'success': False})


def saved_service(tmp_path, server, request_factory, *, session_account=ACCOUNT):
    ledger = Ledger(tmp_path)
    store = SubchatSubmissions(ledger.connection)
    store.prepare(OP, PROMPT, 'fixture model', 'fixture effort', owner=None)
    store.begin_send(OP, owner=None, user_message_id=USER, provider_account_id=ACCOUNT)
    store.submitted(OP, CHAT, USER, owner=None)
    store.complete(OP, 'answer', 'done', owner=None)
    session = ObservedHTTPSession(authorization=SECRET, account_id=session_account,
        catalog_url='https://chatgpt.com/backend-api/models')
    backend = HTTPOnlySubchatBackend(request_factory, session)
    backend._http_reader = ChatHTTPReader(request_factory, browser_free=True,
        session=session, test_origin=f'http://127.0.0.1:{server.server_port}')
    return ledger, Subchats(store, backend)


@pytest.mark.parametrize('mode', ['success', 'account', 'active', 'cross_owner', 'prompt',
                                  'patch_403', 'visible'])
async def test_loopback_delete_identity_status_and_no_replay(tmp_path, mode):
    server = Server(mismatch=mode == 'prompt', patch_status=403 if mode == 'patch_403' else 200,
                    post_status=200 if mode == 'visible' else 404)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        async with async_playwright() as playwright:
            request = await playwright.request.new_context()
            try:
                async def request_factory():
                    return request
                ledger, service = saved_service(tmp_path, server, request_factory,
                    session_account='other-account' if mode == 'account' else ACCOUNT)
                try:
                    target = DeleteRequest(operation_id=OP, conversation_id=CHAT)
                    if mode == 'account':
                        with pytest.raises(SubchatAccountMismatch):
                            await delete_saved(service, target, owner=None)
                        assert server.calls == []
                    elif mode in {'active', 'cross_owner'}:
                        peer_owner = 'other-owner' if mode == 'cross_owner' else None
                        service.store.prepare('b' * 32, 'queued follow-up',
                            'fixture model', 'fixture effort', owner=peer_owner,
                            conversation_id=CHAT,
                            after_operation_id=OP if peer_owner is None else None)
                        with pytest.raises(ValueError, match='active saved submission'):
                            await delete_saved(service, target, owner=None)
                        assert server.calls == []
                    elif mode == 'prompt':
                        with pytest.raises(ValueError, match='does not match'):
                            await delete_saved(service, target, owner=None)
                        assert [method for method, _ in server.calls] == ['GET']
                    elif mode == 'success':
                        first = await delete_saved(service, target, owner=None)
                        assert first.state == 'deleted' and first.automatic_retry is False
                        assert ACCOUNT not in first.model_dump_json()
                        assert [method for method, _ in server.calls] == ['GET', 'PATCH', 'GET']
                        assert (await delete_saved(service, target, owner=None)) == first
                        assert len(server.calls) == 3
                        assert DeletionLedger(ledger.connection).get(
                            target, account_id=ACCOUNT, owner=None) == first
                        service.store.prepare('c' * 32, 'later', 'fixture model',
                            'fixture effort', owner=None, conversation_id=CHAT)
                        with pytest.raises(ValueError, match='pending or confirmed deletion'):
                            service.store.begin_send('c' * 32, owner=None,
                                user_message_id='later-input', provider_account_id=ACCOUNT)
                    else:
                        with pytest.raises(SubchatDeletionUnknown):
                            await delete_saved(service, target, owner=None)
                        assert [method for method, _ in server.calls] == (
                            ['GET', 'PATCH'] if mode == 'patch_403' else ['GET', 'PATCH', 'GET'])
                        assert (await delete_saved(service, target, owner=None)).state == 'unknown'
                        assert len(server.calls) == (2 if mode == 'patch_403' else 3)
                finally:
                    ledger.close()
            finally:
                await request.dispose()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


async def test_cli_and_mcp_delete_entrypoints_use_bound_service(tmp_path):
    server = Server()
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        async with async_playwright() as playwright:
            request = await playwright.request.new_context()
            try:
                async def request_factory():
                    return request
                ledger, service = saved_service(tmp_path, server, request_factory)
                try:
                    command = DeleteCommand(action='delete', operation_id=OP,
                        conversation_id=CHAT)
                    result = json.loads(await dispatch(service, command))
                    assert result == {'operation_id': OP, 'conversation_id': CHAT,
                                      'state': 'deleted', 'automatic_retry': False}
                    mcp = mcp_session(service)
                    try:
                        names = {item['name'] for item in await mcp.catalog()}
                        assert 'subchat_delete' in names
                        tool = next(item for item in await mcp.catalog()
                                    if item['name'] == 'subchat_delete')
                        assert tool['annotations']['destructiveHint'] is True
                        from anywhere_computer.models import Request

                        reply = await mcp.execute(Request(operation_id='b' * 32,
                            tool='subchat_delete', arguments={
                                'operation_id': OP, 'conversation_id': CHAT}))
                        assert reply.state == 'completed'
                        assert reply.data == result
                        assert len(server.calls) == 3
                    finally:
                        await mcp.close()
                finally:
                    ledger.close()
            finally:
                await request.dispose()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
