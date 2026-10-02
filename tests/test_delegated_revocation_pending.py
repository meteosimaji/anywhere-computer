"""Real SQLite/worker revocation; only the bounded file-I/O delay is synthetic."""

import asyncio
import threading
import time
import uuid

import pytest

from anywhere_computer.authorization import AuthorizationStore, pkce_s256
from anywhere_computer.authorized_http import AuthorizedDeviceMCP
from anywhere_computer.delegated_tasks import DelegatedTaskGrant, DelegatedTaskStore
from anywhere_computer.engine import Engine
from anywhere_computer.models import Request
from anywhere_computer.remote_bridge import RemoteAgent


@pytest.mark.parametrize('revocation', ['child', 'parent'])
@pytest.mark.parametrize('tool', ['files_read', 'files_write'])
async def test_long_file_worker_retains_pending_revocation_and_original_receipt(
    tmp_path, monkeypatch, revocation, tool,
):
    import anywhere_computer.authorized_http as authorized_http

    directory = tmp_path.resolve()
    engine = Engine(directory / 'engine')
    authority_directory = directory / 'authority'
    authority = AuthorizationStore(authority_directory, resource='https://fixture.example/mcp',
                                   known_tools=frozenset(engine.tools))
    scopes = frozenset({'files_read', 'files_write', 'operations_get'})
    authority.register_client('fixture', frozenset({'https://fixture.example/callback'}))
    authority.enroll_device('owner', 'gateway', scopes)
    code = authority.approve(owner='owner', device='gateway', client='fixture',
        redirect='https://fixture.example/callback', resource=authority.resource,
        tools=scopes, challenge=pkce_s256('v' * 43))
    token = authority.exchange_code(code=code, verifier='v' * 43, client='fixture',
        redirect='https://fixture.example/callback', resource=authority.resource).value
    parent = authority.verify(token, resource=authority.resource)
    assert parent is not None
    delegation_directory = directory / 'delegation'
    delegation = DelegatedTaskStore(delegation_directory, authority)
    allowed = directory / 'allowed'
    allowed.mkdir()
    source = allowed / 'source.txt'
    source.write_text('synthetic source', encoding='utf-8')
    destination = allowed / 'result.txt'
    child = uuid.uuid4().hex
    delegation.issue(DelegatedTaskGrant(owner='owner', child_id=child,
        parent_grant_id=parent.grant_id, device_id='local', tools=scopes,
        read_files=(str(source),), write_roots=(str(allowed),), expires_at=time.time() + 600))
    backend = AuthorizedDeviceMCP(authority, engine, owner='owner', device='gateway',
                                  delegated_tasks=delegation)
    session = backend.session('child:' + child)
    arguments = ({'path': str(source)} if tool == 'files_read' else
                 {'path': str(destination), 'text': 'one admitted write'})
    request = Request(operation_id=uuid.uuid4().hex, tool=tool, arguments=arguments)
    entered, release = threading.Event(), threading.Event()
    calls = []
    executor = (authorized_http.delegated_read if tool == 'files_read'
                else authorized_http.delegated_write)

    def held_file(*args, **kwargs):
        calls.append(tool)
        entered.set()
        assert release.wait(30), 'Bounded synthetic I/O delay expired'
        return executor(*args, **kwargs)

    monkeypatch.setattr(authorized_http, 'delegated_read' if tool == 'files_read'
                        else 'delegated_write', held_file)
    worker = None
    try:
        observer = asyncio.create_task(session.execute(request))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            worker, = backend._delegated_file_tasks
            observer.cancel()
            await asyncio.gather(observer, return_exceptions=True)
            assert not worker.done()
            internal = RemoteAgent.internal_id('delegated-child:' + child, request.operation_id)
            assert delegation.ledger.get(internal).state == 'running'
            state = (delegation.revoke(child) if revocation == 'child' else
                     authority.revoke(owner='owner', grant=parent.grant_id))
            assert state == 'pending'
            assert delegation.current(child) is None
            denied = await session.execute(request.model_copy(update={
                'operation_id': uuid.uuid4().hex}))
            assert denied.state == 'failed' and denied.data['dispatched'] is False
            assert calls == [tool] and not worker.done()
            # Owner administration must reopen while the existing file guard is
            # still held, without trying a schema write on the reserved database.
            reopened = AuthorizationStore(authority_directory, resource=authority.resource,
                                          known_tools=frozenset(engine.tools))
            try:
                if revocation == 'parent':
                    assert reopened.current_grant(parent.grant_id) is None
            finally:
                reopened.close()
            await asyncio.sleep(10.1)  # Exceed the real store's ten-second busy timeout.
            assert not worker.done()
        finally:
            release.set()
        original = await asyncio.wait_for(worker, 5)
        assert original.state == 'completed'
        assert delegation.ledger.get(internal).state == 'completed'
        assert calls == [tool]
        state = (delegation.revoke(child) if revocation == 'child' else
                 authority.revoke(owner='owner', grant=parent.grant_id))
        assert state == 'revoked'
        assert delegation.current(child) is None
        if tool == 'files_write':
            assert destination.read_text('utf-8') == 'one admitted write'
    finally:
        release.set()
        await backend.close()
        delegation.close()
        authority.close()
        await engine.close()

    # Reconnect/restart preserves the revocation and the exact completed receipt.
    authority = AuthorizationStore(authority_directory, resource='https://fixture.example/mcp',
                                   known_tools=frozenset(engine.tools))
    delegation = DelegatedTaskStore(delegation_directory, authority)
    try:
        assert delegation.current(child) is None
        assert delegation.ledger.get(internal).state == 'completed'
    finally:
        delegation.close()
        authority.close()
