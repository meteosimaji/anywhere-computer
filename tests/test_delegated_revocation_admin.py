"""Password-authenticated owner recovery and actual loopback MCP rejection."""

import json
import sqlite3
import uuid
from contextlib import closing

import httpx
import pytest
from test_client_tokens import MemoryVault
from test_http_service import REDIRECT, authenticate, initialize, setup

from anywhere_computer import cli
from anywhere_computer.authorization import AuthorizationStore
from anywhere_computer.delegated_tasks import DelegatedTaskStore
from anywhere_computer.delegation_admin import manage_delegation
from anywhere_computer.http_service import http_service
from anywhere_computer.models import Reply, Request
from anywhere_computer.owner_credentials import OwnerCredentials
from anywhere_computer.remote_bridge import RemoteAgent


async def test_owner_can_confirm_pending_revoke_and_recover_original_local_receipt(
    tmp_path, unused_tcp_port, monkeypatch, capsys,
):
    scopes = frozenset({'files_read', 'files_write', 'operations_get'})
    config = await setup(tmp_path, unused_tcp_port, scopes=scopes)
    owner = OwnerCredentials(tmp_path, resource=config.resource, owner=config.owner,
                             vault=MemoryVault())
    owner.initialize('synthetic owner password')
    selected = tmp_path / 'selected.txt'
    selected.write_text('synthetic content', encoding='utf-8')
    async with http_service(tmp_path, credentials=owner):
        async with httpx.AsyncClient(base_url=f'http://127.0.0.1:{config.port}',
                                     trust_env=False) as http:
            token = await authenticate(http, redirect=REDIRECT, scopes=scopes)
            authority = AuthorizationStore(tmp_path / 'http-server' / 'authorization',
                                           resource=config.resource, known_tools=scopes)
            try:
                parent = authority.verify(token, resource=config.resource)
                assert parent is not None
                issued = manage_delegation(tmp_path, action='issue',
                    password='synthetic owner password', parent_grant_id=parent.grant_id,
                    tools=frozenset({'files_read'}), read_files=(str(selected.resolve()),),
                    credentials=owner)
                child = issued['child_id']
                headers = await initialize(http, issued['bearer'])
                delegated = DelegatedTaskStore(tmp_path / 'http-server' / 'delegated-tasks',
                                               authority)
                try:
                    operation = uuid.uuid4().hex
                    internal = RemoteAgent.internal_id('delegated-child:' + child, operation)
                    # Seed an already admitted receipt in the real owner ledger;
                    # the worker/observer test separately verifies actual I/O.
                    request = Request(operation_id=internal, tool='files_read',
                                      arguments={'path': str(selected)})
                    assert delegated.ledger.claim(request) is None
                    delegated.ledger.finish(Reply(operation_id=internal, state='completed',
                                                  data={'text': 'saved original result'}))
                    with closing(sqlite3.connect(delegated.database)) as reservation:
                        reservation.execute('BEGIN IMMEDIATE')
                        revoked = manage_delegation(tmp_path, action='revoke',
                            password='synthetic owner password', child_id=child, credentials=owner)
                        assert revoked['revocation_accepted'] is True
                        assert revoked['revocation_state'] == 'pending'
                        assert revoked['revoked'] is False
                        listed = manage_delegation(tmp_path, action='list',
                            password='synthetic owner password', credentials=owner)
                        entry = next(item for item in listed['children']
                                     if item['child_id'] == child)
                        assert entry['active'] is False and entry['revocation_state'] == 'pending'
                        denied = await http.post('/mcp', headers=headers, json={
                            'jsonrpc': '2.0', 'id': uuid.uuid4().hex, 'method': 'tools/call',
                            'params': {'name': 'files_read',
                                       'arguments': {'path': str(selected)}}})
                        assert denied.status_code == 401
                        recovered = manage_delegation(tmp_path, action='receipt',
                            password='synthetic owner password', child_id=child,
                            operation_id=operation, credentials=owner)
                        assert recovered['receipt']['operation_id'] == operation
                        assert recovered['receipt']['state'] == 'completed'
                        assert recovered['receipt']['data']['text'] == 'saved original result'
                        with pytest.raises(ValueError, match='Owner authentication failed'):
                            manage_delegation(tmp_path, action='receipt', password='wrong password',
                                child_id=child, operation_id=operation, credentials=owner)
                        with pytest.raises(ValueError, match='not found for this owner'):
                            manage_delegation(tmp_path, action='receipt',
                                password='synthetic owner password', child_id=uuid.uuid4().hex,
                                operation_id=operation, credentials=owner)
                        reservation.rollback()
                    entry = manage_delegation(tmp_path, action='list',
                        password='synthetic owner password', credentials=owner)['children'][0]
                    assert entry['revocation_state'] == 'revoked' and entry['active'] is False
                finally:
                    delegated.close()
            finally:
                authority.close()
    # The CLI forwards only the original IDs through the same owner boundary.
    original = manage_delegation
    monkeypatch.setattr('anywhere_computer.delegation_admin.manage_delegation',
        lambda *a, **k: original(*a, **k, credentials=owner))
    monkeypatch.setattr(cli, 'has_interactive_input', lambda: True)
    monkeypatch.setattr('anywhere_computer.cli.getpass.getpass',
                        lambda _: 'synthetic owner password')
    monkeypatch.setattr('sys.argv', ['anywhere', 'http-delegate-receipt', '--state-dir',
                                   str(tmp_path), '--child-id', child, '--operation-id', operation])
    cli.main()
    output = json.loads(capsys.readouterr().out)
    assert output['receipt']['state'] == 'completed'
    assert output['receipt']['operation_id'] == operation


@pytest.mark.parametrize('arguments', [
    ['http-delegate-receipt', '--child-id', 'a' * 32],
    ['http-delegate-receipt', '--operation-id', 'b' * 32],
    ['http-delegate-receipt', '--child-id', 'a' * 32, '--operation-id', 'b' * 32,
     '--read-root', '/'],
    ['http-delegate-revoke', '--child-id', 'a' * 32, '--operation-id', 'b' * 32],
    ['status', '--operation-id', 'b' * 32],
])
def test_receipt_options_rejected_before_owner_prompt(monkeypatch, arguments):
    monkeypatch.setattr('sys.argv', ['anywhere', *arguments])
    monkeypatch.setattr('anywhere_computer.cli.getpass.getpass',
                        lambda _: pytest.fail('Unexpected owner prompt'))
    with pytest.raises(SystemExit) as result:
        cli.main()
    assert result.value.code == 2
