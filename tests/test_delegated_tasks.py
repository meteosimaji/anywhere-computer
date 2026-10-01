"""A child bearer never inherits the parent's broad execution scope."""

import asyncio
import os
import sqlite3
import threading
import time
import uuid
from unittest.mock import patch

import httpx
import pytest
from test_client_tokens import MemoryVault
from test_http_service import authenticate, initialize, setup

from anywhere_computer.authorization import AuthorizationStore, pkce_s256
from anywhere_computer.authorized_http import AuthorizedDeviceMCP, delegated_write
from anywhere_computer.delegated_routes import DelegatedRouteStore
from anywhere_computer.delegated_tasks import DelegatedTaskGrant, DelegatedTaskStore
from anywhere_computer.delegation_admin import manage_delegation
from anywhere_computer.devices import DeviceStore
from anywhere_computer.engine import Engine
from anywhere_computer.engine_selection import EngineSelection
from anywhere_computer.http_client import ChildBearerTokens, HTTPBackend, HTTPResponse
from anywhere_computer.http_mcp import HTTPMCP
from anywhere_computer.http_service import http_service
from anywhere_computer.models import Reply, Request, RuntimeSettings
from anywhere_computer.owner_credentials import OwnerCredentials
from anywhere_computer.remote_bridge import RemoteAgent
from anywhere_computer.state import Ledger


async def test_ssh_child_shutdown_drains_cancelled_observers_before_closing_stores(
    tmp_path, unused_tcp_port, monkeypatch,
):
    from anywhere_computer import ssh_child
    from anywhere_computer.http_service import _http_authority

    tools = frozenset({'files_write', 'operations_get'})
    config = await setup(tmp_path, unused_tcp_port, scopes=tools)
    allowed = tmp_path / 'allowed'
    allowed.mkdir()
    child_id = uuid.uuid4().hex
    delegation_directory = tmp_path / 'http-server' / 'delegated-tasks'
    with _http_authority(tmp_path) as (_, authority):
        redirect = next(iter(config.redirects))
        code = authority.approve(
            owner=config.owner, device=config.device, client=config.client,
            redirect=redirect, resource=config.resource, tools=tools,
            challenge=pkce_s256('v' * 43),
        )
        token = authority.exchange_code(
            code=code, verifier='v' * 43, client=config.client,
            redirect=redirect, resource=config.resource,
        ).value
        parent = authority.verify(token, resource=config.resource)
        assert parent is not None
        delegation = DelegatedTaskStore(delegation_directory, authority)
        try:
            bearer = delegation.issue(DelegatedTaskGrant(
                owner=config.owner, child_id=child_id, parent_grant_id=parent.grant_id,
                device_id='local', tools=tools, write_roots=(str(allowed.resolve()),),
                expires_at=time.time() + 600,
            ))
        finally:
            delegation.close()

    entered, release = threading.Event(), threading.Event()
    draining = asyncio.Event()
    workers = []
    request = Request(operation_id=uuid.uuid4().hex, tool='files_write', arguments={
        'path': str(allowed / 'after-disconnect.txt'), 'text': 'write once',
    })
    writes = 0

    def delayed_write(*args):
        nonlocal writes
        entered.set()
        assert release.wait(10)
        writes += 1
        return delegated_write(*args)

    original_close = AuthorizedDeviceMCP.close

    async def close_backend(backend):
        draining.set()
        await original_close(backend)

    async def disconnected_stdio(session, source, destination):
        authenticated = await session.handle({
            'jsonrpc': '2.0', 'id': 'auth', 'method': ssh_child.AUTH_METHOD,
            'params': {'bearer': bearer},
        })
        assert authenticated['result']['authenticated'] is True
        observer = asyncio.create_task(session.session.execute(request))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            workers.extend(session.backend._delegated_file_tasks)
        finally:
            observer.cancel()
            await asyncio.gather(observer, return_exceptions=True)

    monkeypatch.setattr('anywhere_computer.authorized_http.delegated_write', delayed_write)
    monkeypatch.setattr(AuthorizedDeviceMCP, 'close', close_backend)
    monkeypatch.setattr(ssh_child, 'serve_stdio', disconnected_stdio)
    runner = asyncio.create_task(ssh_child.run_ssh_child_mcp(tmp_path))
    waiting = asyncio.create_task(draining.wait())
    try:
        done, _ = await asyncio.wait({runner, waiting}, timeout=5,
                                    return_when=asyncio.FIRST_COMPLETED)
        assert waiting in done, 'SSH child exited without draining its admitted file worker'
        assert not runner.done()
        release.set()
        await asyncio.wait_for(runner, 5)
        assert len(workers) == 1 and workers[0].done()
        assert workers[0].result().state == 'completed'
        assert (allowed / 'after-disconnect.txt').read_text() == 'write once'
        assert writes == 1
        # Reopen the durable receipt after all runner-owned stores were closed.
        ledger = Ledger(delegation_directory / 'delegated-operations')
        try:
            internal_id = RemoteAgent.internal_id('delegated-child:' + child_id,
                                                   request.operation_id)
            assert ledger.get(internal_id).state == 'completed'
        finally:
            ledger.close()
    finally:
        release.set()
        waiting.cancel()
        await asyncio.gather(waiting, runner, *workers, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('root_grant', [False, True])
async def test_shared_delegated_files_use_selected_engine_limits(tmp_path, root_grant):
    control = tmp_path / "control"
    old_engine = Engine(control)
    selected_directory = control / "engines" / "selected"
    selected = Engine(selected_directory, file_locks=control / "file-locks")
    (control / "engine-selection.json").write_text(
        EngineSelection(directory=str(selected_directory)).model_dump_json(),
        encoding="utf-8",
    )
    with selected.ledger.connection:
        selected.ledger.connection.execute(
            "INSERT OR REPLACE INTO runtime_settings(id,value) VALUES(1,?)",
            (RuntimeSettings(file_read_line_limit=1,
                             file_write_line_limit=1).model_dump_json(),),
        )
    authority = AuthorizationStore(tmp_path / "authority", resource="https://fixture.example/mcp",
                                   known_tools=frozenset(selected.tools))
    authority.register_client("chat", frozenset({"https://chat.example/callback"}))
    tools = frozenset({"files_read", "files_write", "operations_get"})
    authority.enroll_device("owner", "gateway", tools)
    code = authority.approve(
        owner="owner", device="gateway", client="chat",
        redirect="https://chat.example/callback", resource=authority.resource,
        tools=tools, challenge=pkce_s256("v" * 43),
    )
    token = authority.exchange_code(
        code=code, verifier="v" * 43, client="chat",
        redirect="https://chat.example/callback", resource=authority.resource,
    ).value
    delegation = DelegatedTaskStore(tmp_path / "delegation", authority)
    backend = AuthorizedDeviceMCP(authority, agent_directory=control,
                                  owner="owner", device="gateway", delegated_tasks=delegation)
    parent_id = await backend.authenticate(token)
    assert parent_id is not None
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    source = allowed / "source.txt"
    source.write_bytes(b"one\ntwo\n")
    child_id = uuid.uuid4().hex
    delegation.issue(DelegatedTaskGrant(
        owner="owner", child_id=child_id, parent_grant_id=parent_id,
        device_id="local", tools=tools,
        read_roots=("/", str(allowed.resolve())) if root_grant and os.name != 'nt'
        else (str(allowed.resolve()),),
        write_roots=("/", str(allowed.resolve())) if root_grant and os.name != 'nt'
        else (str(allowed.resolve()),),
        expires_at=time.time() + 600,
    ))
    child = backend.session("child:" + child_id)
    try:
        read = await child.execute(Request(
            operation_id=uuid.uuid4().hex, tool="files_read",
            arguments={"path": str(source), "limit": 2},
        ))
        assert read.state == "completed" and read.data["text"] == "one\n"
        valid = await child.execute(Request(
            operation_id=uuid.uuid4().hex, tool="files_write",
            arguments={"path": str(allowed / "valid.txt"), "text": "one"}))
        assert valid.state == 'completed'
        assert (allowed / 'valid.txt').read_text() == 'one'
        target = allowed / "target.txt"
        write = await child.execute(Request(
            operation_id=uuid.uuid4().hex, tool="files_write",
            arguments={"path": str(target), "text": "one\ntwo\n"},
        ))
        assert write.state == "failed" and not target.exists()
    finally:
        await backend.close()
        delegation.close()
        await selected.close()
        await old_engine.close()


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX dir_fd confinement")
@pytest.mark.asyncio
async def test_delegated_child_read_denial_revocation_and_reconnect(tmp_path):
    engine = Engine(tmp_path / "engine")
    tools = frozenset({"files_read", "files_write", "operations_get", "computer_status",
                       "terminal_start", "gui_click"})
    authority = AuthorizationStore(tmp_path / "authority", resource="https://fixture.example/mcp",
                                   known_tools=frozenset(engine.tools))
    authority.register_client("chat", frozenset({"https://chat.example/callback"}))
    authority.enroll_device("owner", "gateway", tools)
    code = authority.approve(owner="owner", device="gateway", client="chat",
                             redirect="https://chat.example/callback", resource=authority.resource,
                             tools=tools, challenge=pkce_s256("v" * 43))
    parent_token = authority.exchange_code(
        code=code, verifier="v" * 43, client="chat",
        redirect="https://chat.example/callback", resource=authority.resource,
    ).value
    parent_id = await AuthorizedDeviceMCP(
        authority, engine, owner="owner", device="gateway").authenticate(parent_token)
    assert parent_id is not None
    delegation = DelegatedTaskStore(tmp_path / "delegation", authority)
    child_id = uuid.uuid4().hex
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    (allowed / "source.txt").write_text("safe", encoding="utf-8")
    child_token = delegation.issue(DelegatedTaskGrant(
        owner="owner", child_id=child_id, parent_grant_id=parent_id,
        device_id="local", tools=frozenset({"files_read"}),
        read_roots=(str(allowed.resolve()),), expires_at=time.time() + 600,
    ))
    with pytest.raises(ValueError, match="Parent authorization"):
        delegation.issue(DelegatedTaskGrant(
            owner="owner", child_id=uuid.uuid4().hex, parent_grant_id=parent_id,
            device_id=uuid.uuid4().hex, tools=frozenset({"files_read"}),
            read_roots=(str(allowed.resolve()),), expires_at=time.time() + 600,
        ))
    backend = AuthorizedDeviceMCP(authority, engine, owner="owner", device="gateway",
                                  delegated_tasks=delegation)
    assert await backend.authenticate(child_token) == "child:" + child_id
    session = backend.session("child:" + child_id)
    assert {item["name"] for item in await session.catalog()} == {
        "files_read", "delegated_identity"}
    identified = await session.execute(Request(
        operation_id=uuid.uuid4().hex, tool='delegated_identity', arguments={}))
    assert identified.state == 'completed'
    assert identified.data == {'child_id': child_id, 'device_id': 'local'}

    adapter = HTTPMCP(backend.authenticate, backend.session)
    port = await adapter.start()
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as http:
            headers = await initialize(http, child_token)
            response = await http.post("/mcp", headers=headers, json={
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "files_read", "arguments": {
                    "path": str(allowed / "source.txt"), "request_id": uuid.uuid4().hex,
                }},
            })
            assert response.status_code == 200
            assert response.json()["result"]["structuredContent"]["state"] == "completed"
    finally:
        await adapter.close()

    async def call(tool, arguments):
        return await session.execute(Request(operation_id=uuid.uuid4().hex,
                                             tool=tool, arguments=arguments))

    assert (await call("files_read", {"path": str(allowed / "source.txt")})).state == "completed"
    outside = tmp_path / "outside.txt"
    outside.write_text("private", encoding="utf-8")
    denied = await call("files_read", {"path": str(outside)})
    assert denied.state == "failed" and denied.data["reason"] == "path_out_of_scope"
    (allowed / "escape.txt").symlink_to(outside)
    escaped = await call("files_read", {"path": str(allowed / "escape.txt")})
    assert escaped.state == "failed" and escaped.data["reason"] == "path_out_of_scope"
    alias = tmp_path / "allowed-alias"
    alias.symlink_to(allowed, target_is_directory=True)
    aliased = await call("files_read", {"path": str(alias / "source.txt")})
    assert aliased.state == "failed"
    assert aliased.data == {"dispatched": False, "reason": "path_out_of_scope"}
    denial_id = RemoteAgent.internal_id("delegated-child:" + child_id,
                                        denied.operation_id)
    assert delegation.ledger.get(denial_id).state == "failed"
    for tool, args in (("files_write", {"path": str(allowed / "new.txt"), "text": "bad"}),
                       ("terminal_start", {}), ("gui_click", {})):
        rejected = await call(tool, args)
        assert rejected.state == "failed" and rejected.data["dispatched"] is False
    assert not (allowed / "new.txt").exists()
    count = delegation.db.execute(
        "SELECT count(*) FROM audit WHERE decision='denied'"
    ).fetchone()[0]
    assert count == 6
    nested = allowed / "nested"
    nested.mkdir()
    outside_dir = tmp_path / "outside-dir"
    outside_dir.mkdir()
    (outside_dir / "secret.txt").write_text("private", encoding="utf-8")
    def swap_after_precheck(_path, _roots):
        nested.rename(allowed / "former-nested")
        nested.symlink_to(outside_dir, target_is_directory=True)
        return True
    with patch("anywhere_computer.delegated_tasks._inside",
               side_effect=swap_after_precheck):
        swapped = await call("files_read", {"path": str(nested / "secret.txt")})
    assert swapped.state == "failed"
    assert "private" not in str(swapped.model_dump())
    delegation.close()
    delegation = DelegatedTaskStore(tmp_path / "delegation", authority)
    backend = AuthorizedDeviceMCP(authority, engine, owner="owner", device="gateway",
                                  delegated_tasks=delegation)
    assert await backend.authenticate(child_token) == "child:" + child_id
    session = backend.session("child:" + child_id)
    other_child = uuid.uuid4().hex
    other_expires = time.time() + 600
    other_token = delegation.issue(DelegatedTaskGrant(
        owner="owner", child_id=other_child, parent_grant_id=parent_id,
        device_id="local", tools=frozenset({"files_read", "files_write", "operations_get",
                                             "computer_status"}),
        read_roots=(str(allowed.resolve()),), write_roots=(str(allowed.resolve()),),
        expires_at=other_expires,
    ))
    writer = backend.session("child:" + other_child)
    status_then_write_id = uuid.uuid4().hex
    assert (await writer.execute(Request(
        operation_id=status_then_write_id, tool="computer_status", arguments={},
    ))).state == "completed"
    status_alias_path = allowed / "reused-after-status.txt"
    status_alias = await writer.execute(Request(
        operation_id=status_then_write_id, tool="files_write",
        arguments={"path": str(status_alias_path), "text": "must not write"},
    ))
    assert status_alias.data["reason"] == "operation_id_conflict"
    assert not status_alias_path.exists()
    # The denied and dispatched paths must share one request-ID identity.
    denied_then_write_id = uuid.uuid4().hex
    denied_then_write = await writer.execute(Request(
        operation_id=denied_then_write_id, tool="files_write",
        arguments={"path": str(outside), "text": "denied"},
    ))
    assert denied_then_write.data["reason"] == "path_out_of_scope"
    refused_path = allowed / "reused-after-denial.txt"
    reused_after_denial = await writer.execute(Request(
        operation_id=denied_then_write_id, tool="files_write",
        arguments={"path": str(refused_path), "text": "must not write"},
    ))
    assert reused_after_denial.data["reason"] == "operation_id_conflict"
    assert not refused_path.exists()
    denied_receipt = await writer.execute(Request(
        operation_id=uuid.uuid4().hex, tool="operations_get",
        arguments={"operation_id": denied_then_write_id},
    ))
    assert denied_receipt.data["data"]["reason"] == "path_out_of_scope"
    write_then_denied_id = uuid.uuid4().hex
    first_path = allowed / "written-before-denial.txt"
    first_write = await writer.execute(Request(
        operation_id=write_then_denied_id, tool="files_write",
        arguments={"path": str(first_path), "text": "original"},
    ))
    assert first_write.state == "completed"
    reused_after_write = await writer.execute(Request(
        operation_id=write_then_denied_id, tool="files_write",
        arguments={"path": str(outside), "text": "must not write"},
    ))
    assert reused_after_write.data["reason"] == "operation_id_conflict"
    assert first_path.read_text(encoding="utf-8") == "original"
    write_receipt = await writer.execute(Request(
        operation_id=uuid.uuid4().hex, tool="operations_get",
        arguments={"operation_id": write_then_denied_id},
    ))
    assert write_receipt.data["state"] == "completed"
    write_alias = await writer.execute(Request(
        operation_id=write_then_denied_id, tool="computer_status", arguments={},
    ))
    assert write_alias.data["reason"] == "operation_id_conflict"
    legacy_duplicate_id = uuid.uuid4().hex
    for prefix, state in (("delegated-denial:", "failed"),
                          ("delegated-child:", "completed")):
        internal_id = RemoteAgent.internal_id(prefix + other_child, legacy_duplicate_id)
        assert delegation.ledger.claim(Request(
            operation_id=internal_id, tool="files_write", arguments={})) is None
        delegation.ledger.finish(Reply(operation_id=internal_id, state=state))
    ambiguous_receipt = await writer.execute(Request(
        operation_id=uuid.uuid4().hex, tool="operations_get",
        arguments={"operation_id": legacy_duplicate_id},
    ))
    assert ambiguous_receipt.state == "failed"
    assert ambiguous_receipt.data["reason"] == "operation_id_ambiguous"
    write_id = uuid.uuid4().hex
    written = await writer.execute(Request(
        operation_id=write_id, tool="files_write",
        arguments={"path": str(allowed / "written.txt"), "text": "bounded"},
    ))
    assert written.state == "completed"
    recovered = await writer.execute(Request(
        operation_id=uuid.uuid4().hex, tool="operations_get",
        arguments={"operation_id": write_id},
    ))
    assert recovered.state == "completed"
    assert recovered.data["operation_id"] == write_id
    assert (allowed / "written.txt").read_text(encoding="utf-8") == "bounded"
    entered_write, release_write = threading.Event(), threading.Event()

    def delayed_write(*args):
        entered_write.set()
        assert release_write.wait(5)
        return delegated_write(*args)

    cancelled_id = uuid.uuid4().hex
    cancelled_path = allowed / "cancelled-request.txt"
    with patch("anywhere_computer.authorized_http.delegated_write",
               side_effect=delayed_write):
        cancelled_task = asyncio.create_task(writer.execute(Request(
            operation_id=cancelled_id, tool="files_write",
            arguments={"path": str(cancelled_path), "text": "still record result"},
        )))
        try:
            assert await asyncio.to_thread(entered_write.wait, 5)
            cancelled_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await cancelled_task
        finally:
            release_write.set()
        await backend.close()
    assert cancelled_path.read_text(encoding="utf-8") == "still record result"
    cancelled_internal = RemoteAgent.internal_id("delegated-child:" + other_child,
                                                  cancelled_id)
    assert delegation.ledger.get(cancelled_internal).state == "completed"
    assert (await writer.execute(Request(
        operation_id=uuid.uuid4().hex, tool="operations_get",
        arguments={"operation_id": cancelled_id},
    ))).data["state"] == "completed"
    with engine.ledger.connection:
        engine.ledger.connection.execute(
            "INSERT OR REPLACE INTO runtime_settings(id,value) VALUES(1,?)",
            (RuntimeSettings(file_read_line_limit=1,
                             file_write_line_limit=1).model_dump_json(),),
        )
    (allowed / "lines.txt").write_text("one\ntwo\n", encoding="utf-8")
    limited = await writer.execute(Request(
        operation_id=uuid.uuid4().hex, tool="files_read",
        arguments={"path": str(allowed / "lines.txt"), "limit": 200},
    ))
    assert limited.state == "completed" and limited.data["text"] == "one\n"
    too_many = await writer.execute(Request(
        operation_id=uuid.uuid4().hex, tool="files_write",
        arguments={"path": str(allowed / "too-many.txt"), "text": "one\ntwo\n"},
    ))
    assert too_many.state == "failed" and not (allowed / "too-many.txt").exists()
    parent_operation = uuid.uuid4().hex
    assert (await engine.execute(Request(
        operation_id=parent_operation, tool="files_read",
        arguments={"path": str(allowed / "source.txt")},
    ))).state == "completed"
    hidden = await writer.execute(Request(
        operation_id=uuid.uuid4().hex, tool="operations_get",
        arguments={"operation_id": parent_operation},
    ))
    assert hidden.state == "failed"
    with patch("anywhere_computer.delegated_tasks.time.time", return_value=other_expires + 1):
        assert await backend.authenticate(other_token) is None
    wrong_child = delegation.check(uuid.uuid4().hex, "local", Request(
        operation_id=uuid.uuid4().hex, tool="files_read",
        arguments={"path": str(allowed / "source.txt")},
    ))
    assert wrong_child is not None and wrong_child.data["dispatched"] is False
    wrong_device = delegation.check(other_child, uuid.uuid4().hex, Request(
        operation_id=uuid.uuid4().hex, tool="files_read",
        arguments={"path": str(allowed / "source.txt")},
    ))
    assert wrong_device is not None and wrong_device.data["reason"] == "device_out_of_scope"
    raced_child = uuid.uuid4().hex
    delegation.issue(DelegatedTaskGrant(
        owner="owner", child_id=raced_child, parent_grant_id=parent_id,
        device_id="local", tools=frozenset({"files_write", "operations_get"}),
        write_roots=(str(allowed.resolve()),), expires_at=time.time() + 600,
    ))
    raced_session = backend.session("child:" + raced_child)
    entered, release = threading.Event(), threading.Event()
    original_guard = delegation.local_file_guard

    def delayed_guard(identity, request):
        if identity == raced_child:
            entered.set()
            assert release.wait(5)
        return original_guard(identity, request)

    raced_path = allowed / "revoked-before-write.txt"
    with patch.object(delegation, "local_file_guard", side_effect=delayed_guard):
        task = asyncio.create_task(raced_session.execute(Request(
            operation_id=uuid.uuid4().hex, tool="files_write",
            arguments={"path": str(raced_path), "text": "must not publish"},
        )))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            delegation.revoke(raced_child)
        finally:
            release.set()
        raced = await task
    assert raced.state == "failed" and raced.data["dispatched"] is False
    assert raced.data["reason"] == "authorization_unavailable"
    assert not raced_path.exists()
    assert delegation.db.execute(
        "SELECT reason FROM audit WHERE child_id=? AND operation_id=?",
        (raced_child, raced.operation_id),
    ).fetchone() == ("authorization_unavailable",)
    scope_child = uuid.uuid4().hex
    delegation.issue(DelegatedTaskGrant(
        owner="owner", child_id=scope_child, parent_grant_id=parent_id,
        device_id="local", tools=frozenset({"files_write", "operations_get"}),
        write_roots=(str(allowed.resolve()),), expires_at=time.time() + 600,
    ))
    scope_session = backend.session("child:" + scope_child)
    scope_entered, scope_release = threading.Event(), threading.Event()

    def delayed_scope_guard(identity, request):
        if identity == scope_child:
            scope_entered.set()
            assert scope_release.wait(5)
        return original_guard(identity, request)

    scope_path = allowed / "scope-removed-before-write.txt"
    with patch.object(delegation, "local_file_guard", side_effect=delayed_scope_guard):
        scope_task = asyncio.create_task(scope_session.execute(Request(
            operation_id=uuid.uuid4().hex, tool="files_write",
            arguments={"path": str(scope_path), "text": "must not publish"},
        )))
        try:
            assert await asyncio.to_thread(scope_entered.wait, 5)
            current_scope = delegation.current(scope_child)
            assert current_scope is not None
            with delegation.db:
                delegation.db.execute("UPDATE grants SET body=? WHERE child_id=?", (
                    current_scope.model_copy(update={
                        "tools": frozenset({"files_write"}),
                    }).model_dump_json(), scope_child,
                ))
        finally:
            scope_release.set()
        scope_denied = await scope_task
    assert scope_denied.state == "failed"
    assert scope_denied.data["reason"] == "authorization_unavailable"
    assert not scope_path.exists()
    delegation.revoke(child_id)
    assert await backend.authenticate(child_token) is None
    again = await session.execute(Request(
        operation_id=uuid.uuid4().hex, tool="files_read",
        arguments={"path": str(allowed / "source.txt")},
    ))
    assert again.state == "failed"
    parent_entered, parent_release = threading.Event(), threading.Event()

    def delayed_parent_guard(identity, request):
        if identity == other_child:
            parent_entered.set()
            assert parent_release.wait(5)
        return original_guard(identity, request)

    parent_raced_path = allowed / "parent-revoked-before-write.txt"
    with patch.object(delegation, "local_file_guard", side_effect=delayed_parent_guard):
        parent_raced_task = asyncio.create_task(writer.execute(Request(
            operation_id=uuid.uuid4().hex, tool="files_write",
            arguments={"path": str(parent_raced_path), "text": "must not publish"},
        )))
        try:
            assert await asyncio.to_thread(parent_entered.wait, 5)
            authority.revoke(owner="owner", grant=parent_id)
        finally:
            parent_release.set()
        parent_raced = await parent_raced_task
    assert parent_raced.state == "failed"
    assert parent_raced.data["reason"] == "authorization_unavailable"
    assert not parent_raced_path.exists()
    assert await backend.authenticate(other_token) is None
    delegation.close()
    authority.close()
    await engine.close()


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="requires POSIX dir_fd confinement")
async def test_production_http_service_accepts_scoped_child_bearer(tmp_path, unused_tcp_port):
    config = await setup(tmp_path, unused_tcp_port)
    owner = OwnerCredentials(tmp_path, resource=config.resource, owner=config.owner,
                             vault=MemoryVault())
    owner.initialize("synthetic owner password")
    allowed = tmp_path / "work"
    allowed.mkdir()
    source = allowed / "source.txt"
    source.write_text("visible", encoding="utf-8")
    async with http_service(tmp_path, credentials=owner):
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{config.port}",
                                     trust_env=False) as http:
            parent_token = await authenticate(http)
            authority = AuthorizationStore(
                tmp_path / "http-server" / "authorization", resource=config.resource,
                known_tools=config.scopes,
            )
            try:
                parent = authority.verify(parent_token, resource=config.resource)
                assert parent is not None
                child_id = uuid.uuid4().hex
                delegated = DelegatedTaskStore(tmp_path / "http-server" / "delegated-tasks",
                                               authority)
                try:
                    child_token = delegated.issue(DelegatedTaskGrant(
                        owner=config.owner, child_id=child_id,
                        parent_grant_id=parent.grant_id, device_id="local",
                        tools=frozenset({"files_read", "files_write", "operations_get"}),
                        read_roots=(str(allowed.resolve()),),
                        write_roots=(str(allowed.resolve()),),
                        expires_at=time.time() + 600,
                    ))
                finally:
                    delegated.close()
            finally:
                authority.close()
            headers = await initialize(http, child_token)
            response = await http.post("/mcp", headers=headers, json={
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "files_read", "arguments": {
                    "path": str(source), "request_id": uuid.uuid4().hex,
                }},
            })
            assert response.status_code == 200
            assert response.json()["result"]["structuredContent"]["state"] == "completed"
            response = await http.post("/mcp", headers=headers, json={
                "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "files_write", "arguments": {
                    "path": str(allowed / "created.txt"), "text": "created",
                    "request_id": uuid.uuid4().hex,
                }},
            })
            assert response.status_code == 200
            assert response.json()["result"]["structuredContent"]["state"] == "completed"
            assert (allowed / "created.txt").read_text() == "created"

            def child_wire(resource, method, packet, headers):
                assert resource == config.resource
                response = httpx.request(
                    method, f'http://127.0.0.1:{config.port}/mcp', headers=headers,
                    json=packet, timeout=5, trust_env=False)
                return HTTPResponse(response.status_code,
                                    {key.lower(): value for key, value in response.headers.items()},
                                    response.json() if response.content else None)

            backend = HTTPBackend(ChildBearerTokens(config.resource, child_token),
                                  wire=child_wire)
            try:
                assert {item['name'] for item in await backend.catalog()} == {
                    'files_read', 'files_write', 'operations_get',
                    'delegated_identity'}
                identified = await backend.execute(Request(
                    operation_id=uuid.uuid4().hex, tool='delegated_identity',
                    arguments={}))
                assert identified.state == 'completed'
                assert identified.data == {'child_id': child_id, 'device_id': 'local'}
                in_scope = await backend.execute(Request(
                    operation_id=uuid.uuid4().hex, tool='files_read',
                    arguments={'path': str(source)}))
                assert in_scope.state == 'completed'
                outside = tmp_path / 'outside.txt'
                outside.write_text('private')
                denied = await backend.execute(Request(
                    operation_id=uuid.uuid4().hex, tool='files_read',
                    arguments={'path': str(outside)}))
                assert denied.state == 'failed'
                assert denied.data['reason'] == 'path_out_of_scope'
                assert repr(backend.tokens) == 'ChildBearerTokens(<redacted>)'
                authority = AuthorizationStore(
                    tmp_path / 'http-server' / 'authorization', resource=config.resource,
                    known_tools=config.scopes)
                try:
                    delegated = DelegatedTaskStore(
                        tmp_path / 'http-server' / 'delegated-tasks', authority)
                    try:
                        delegated.revoke(child_id)
                    finally:
                        delegated.close()
                finally:
                    authority.close()
                revoked = await backend.execute(Request(
                    operation_id=uuid.uuid4().hex, tool='files_read',
                    arguments={'path': str(source)}))
                assert revoked.state == 'failed'
            finally:
                await backend.close()


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="requires POSIX dir_fd confinement")
async def test_remote_child_call_uses_target_child_grant_not_parent_route(tmp_path):
    def parent_grant(authority, owner, device, tools):
        authority.register_client('client', frozenset({'https://client.example/callback'}))
        authority.enroll_device(owner, device, tools)
        code = authority.approve(
            owner=owner, device=device, client='client',
            redirect='https://client.example/callback', resource=authority.resource,
            tools=tools, challenge=pkce_s256('v' * 43))
        token = authority.exchange_code(
            code=code, verifier='v' * 43, client='client',
            redirect='https://client.example/callback', resource=authority.resource).value
        grant = authority.verify(token, resource=authority.resource)
        assert grant is not None
        return grant

    target_engine = Engine(tmp_path / 'target-engine')
    target_authority = AuthorizationStore(
        tmp_path / 'target-authority', resource='https://target.example/mcp',
        known_tools=frozenset(target_engine.tools))
    target_parent = parent_grant(target_authority, 'target-owner', 'target-device',
                                 frozenset({'files_read', 'files_write',
                                            'operations_get', 'computer_status'}))
    target_delegation = DelegatedTaskStore(tmp_path / 'target-delegation', target_authority)
    target_child_id = uuid.uuid4().hex
    allowed = tmp_path / 'target-allowed'
    allowed.mkdir()
    (allowed / 'source.txt').write_text('visible')
    target_bearer = target_delegation.issue(DelegatedTaskGrant(
        owner='target-owner', child_id=target_child_id,
        parent_grant_id=target_parent.grant_id, device_id='local',
        tools=frozenset({'files_read', 'files_write', 'operations_get', 'computer_status'}),
        read_roots=(str(allowed.resolve()),), write_roots=(str(allowed.resolve()),),
        expires_at=time.time() + 600))
    target_binding = AuthorizedDeviceMCP(
        target_authority, target_engine, owner='target-owner', device='target-device',
        delegated_tasks=target_delegation)
    target_server = HTTPMCP(target_binding.authenticate, target_binding.session)
    port = await target_server.start()
    source_engine = Engine(tmp_path / 'source-engine')
    source_authority = AuthorizationStore(
        tmp_path / 'source-authority', resource='https://source.example/mcp',
        known_tools=frozenset(source_engine.tools) | frozenset({'devices_call'}))
    source_parent = parent_grant(source_authority, 'source-owner', 'source-device',
                                 frozenset({'devices_call'}))
    source_directory = tmp_path / 'source-devices'
    devices = DeviceStore(source_directory)
    device_id = devices.add_http('Target', target_authority.resource,
                                 'client', 'profile')['device_id']
    devices.close()
    source_delegation = DelegatedTaskStore(tmp_path / 'source-delegation', source_authority)
    source_child_id = uuid.uuid4().hex
    with pytest.raises(ValueError, match='Parent authorization'):
        source_delegation.issue(DelegatedTaskGrant(
            owner='source-owner', child_id=uuid.uuid4().hex,
            parent_grant_id=source_parent.grant_id, device_id=device_id,
            tools=frozenset({'files_write'}), expires_at=time.time() + 600))
    source_bearer = source_delegation.issue(DelegatedTaskGrant(
        owner='source-owner', child_id=source_child_id,
        parent_grant_id=source_parent.grant_id, device_id=device_id,
        tools=frozenset({'files_read', 'files_write', 'operations_get', 'computer_status'}),
        expires_at=time.time() + 600))
    vault = MemoryVault()
    routes = DelegatedRouteStore(source_directory / 'delegated-routes', vault=vault)
    routes.bind(source_child_id, device_id, target_authority.resource, target_bearer,
                target_child_id=target_child_id)
    routes.close()

    forwarded_packets = []
    status_received, deliver_status = threading.Event(), threading.Event()

    def wire(resource, method, packet, headers):
        forwarded_packets.append(packet)
        assert resource == target_authority.resource
        response = httpx.request(method, f'http://127.0.0.1:{port}/mcp',
                                 headers=headers, json=packet, timeout=5, trust_env=False)
        if (isinstance(packet, dict) and packet.get('method') == 'tools/call'
                and packet.get('params', {}).get('name') == 'computer_status'):
            status_received.set()
            assert deliver_status.wait(10)
        return HTTPResponse(response.status_code,
                            {key.lower(): value for key, value in response.headers.items()},
                            response.json() if response.content else None)

    real_backend = HTTPBackend
    real_routes = DelegatedRouteStore
    source_binding = AuthorizedDeviceMCP(
        source_authority, source_engine, owner='source-owner', device='source-device',
        device_directory=source_directory, delegated_tasks=source_delegation)
    try:
        with patch('anywhere_computer.authorized_http.DelegatedRouteStore',
                   side_effect=lambda directory: real_routes(directory, vault=vault)), \
             patch('anywhere_computer.authorized_http.HTTPBackend',
                   side_effect=lambda tokens: real_backend(tokens, wire=wire)):
            assert await source_binding.authenticate(source_bearer) == (
                'child:' + source_child_id)
            session = source_binding.session('child:' + source_child_id)

            async def remote(tool, arguments, *, selected_device=device_id,
                             operation_id=None):
                return await session.execute(Request(
                    operation_id=operation_id or uuid.uuid4().hex, tool='devices_call',
                    arguments={'device_id': selected_device, 'tool': tool,
                               'arguments': arguments}))

            status_id = uuid.uuid4().hex
            lost_status = asyncio.create_task(remote('computer_status', {},
                                                     operation_id=status_id))
            try:
                assert await asyncio.to_thread(status_received.wait, 5)
                lost_status.cancel()
                deliver_status.set()
                with pytest.raises(asyncio.CancelledError):
                    await lost_status
            finally:
                deliver_status.set()
            recovered_status = await remote('operations_get', {'operation_id': status_id})
            assert recovered_status.state == 'completed', recovered_status
            assert recovered_status.data['result']['state'] == 'completed'
            assert recovered_status.data['result']['operation_id'] == status_id
            packets_before_status_retry = len(forwarded_packets)
            retry_status = await remote('computer_status', {}, operation_id=status_id)
            assert retry_status.state == 'completed'
            assert len(forwarded_packets) == packets_before_status_retry

            read_id = uuid.uuid4().hex
            read_args = {'path': str(allowed / 'source.txt')}
            read = await remote('files_read', read_args, operation_id=read_id)
            assert read.state == 'completed' and read.data['result']['text'] == 'visible'
            target_without_recovery = target_delegation.current(target_child_id)
            assert target_without_recovery is not None
            direct_target = target_binding.session('child:' + target_child_id)
            with target_delegation.db:
                target_delegation.db.execute(
                    'UPDATE grants SET body=? WHERE child_id=?',
                    (target_without_recovery.model_copy(update={
                        'tools': frozenset({'files_read', 'files_write'})
                    }).model_dump_json(), target_child_id))
            unrecoverable_path = allowed / 'unrecoverable-write.txt'
            direct_denial = await direct_target.execute(Request(
                operation_id=uuid.uuid4().hex, tool='files_write',
                arguments={'path': str(unrecoverable_path),
                           'text': 'must not publish'}))
            assert direct_denial.state == 'failed'
            assert direct_denial.data['reason'] == 'authorization_unavailable'
            unrecoverable = await remote('files_write', {
                'path': str(unrecoverable_path), 'text': 'must not publish'})
            assert unrecoverable.state == 'failed'
            assert not unrecoverable_path.exists()
            with target_delegation.db:
                target_delegation.db.execute(
                    'UPDATE grants SET body=? WHERE child_id=?',
                    (target_without_recovery.model_dump_json(), target_child_id))
            route_database = source_directory / 'delegated-routes' / 'delegated-routes.sqlite3'
            with sqlite3.connect(route_database) as route_db:
                route_db.execute('UPDATE delegated_routes SET target_child_id=? '
                                 'WHERE child_id=?', ('d' * 32, source_child_id))
            mismatched_path = allowed / 'wrong-target-id.txt'
            mismatched = await remote('files_write', {
                'path': str(mismatched_path), 'text': 'must not publish'})
            assert mismatched.state == 'failed'
            assert mismatched.data['result']['reason'] == 'target_identity_unverified'
            assert not mismatched_path.exists()
            with sqlite3.connect(route_database) as route_db:
                route_db.execute('UPDATE delegated_routes SET target_child_id=NULL '
                                 'WHERE child_id=?', (source_child_id,))
            missing_identity_path = allowed / 'missing-target-id.txt'
            missing_identity = await remote('files_write', {
                'path': str(missing_identity_path), 'text': 'must not publish'})
            assert missing_identity.state == 'failed'
            assert missing_identity.data['result']['reason'] == 'target_identity_unverified'
            assert not missing_identity_path.exists()
            with sqlite3.connect(route_database) as route_db:
                route_db.execute('UPDATE delegated_routes SET target_child_id=? '
                                 'WHERE child_id=?', (target_child_id, source_child_id))
            assert await remote('files_read', read_args, operation_id=read_id) == read
            changed = await remote('files_read', {'path': str(allowed / 'other.txt')},
                                   operation_id=read_id)
            assert changed.state == 'failed'
            assert changed.data['reason'] == 'operation_id_conflict'
            recovered = await remote('operations_get', {'operation_id': read_id})
            assert recovered.data['operation_id'] == read_id
            assert recovered.data['state'] == 'completed'
            cancelled_entered, cancelled_release = threading.Event(), threading.Event()
            original_target_guard = target_delegation.local_file_guard

            def held_target_guard(identity, call):
                cancelled_entered.set()
                assert cancelled_release.wait(5)
                return original_target_guard(identity, call)

            cancelled_id = uuid.uuid4().hex
            cancelled_path = allowed / 'completed-after-source-cancel.txt'
            with patch.object(target_delegation, 'local_file_guard',
                              side_effect=held_target_guard):
                cancelled_source = asyncio.create_task(remote('files_write', {
                    'path': str(cancelled_path), 'text': 'recorded once',
                }, operation_id=cancelled_id))
                try:
                    assert await asyncio.to_thread(cancelled_entered.wait, 5)
                    cancelled_source.cancel()
                    cancelled_release.set()
                    with pytest.raises(asyncio.CancelledError):
                        await cancelled_source
                finally:
                    cancelled_release.set()
                await target_binding.close()
            assert cancelled_path.read_text() == 'recorded once'
            source_internal = RemoteAgent.internal_id('delegated-child:' + source_child_id,
                                                      cancelled_id)
            assert source_delegation.ledger.get(source_internal).state in {'running', 'unknown'}
            reopened_source = DelegatedTaskStore(tmp_path / 'source-delegation',
                                                 source_authority)
            try:
                assert reopened_source.remote_operation(source_child_id, cancelled_id) == (
                    device_id, 'files_write', target_child_id,
                    Ledger.request_digest(Request(
                        operation_id=source_internal, tool='files_write',
                        arguments={'path': str(cancelled_path), 'text': 'recorded once'})))
                assert reopened_source.ledger.get(source_internal).state == 'unknown'
            finally:
                reopened_source.close()
            recovered_cancelled = await remote('operations_get',
                                               {'operation_id': cancelled_id})
            assert recovered_cancelled.data['result']['state'] == 'completed'
            assert recovered_cancelled.data['result']['operation_id'] == cancelled_id
            assert source_delegation.ledger.get(source_internal).state == 'completed'
            packets_before_recovery_retry = len(forwarded_packets)
            exact_retry = await remote('files_write', {
                'path': str(cancelled_path), 'text': 'recorded once',
            }, operation_id=cancelled_id)
            assert exact_retry.state == 'completed'
            assert exact_retry.data['result']['path'] == str(cancelled_path)
            assert len(forwarded_packets) == packets_before_recovery_retry
            changed_target_id = uuid.uuid4().hex
            changed_target_args = {'device_id': device_id, 'tool': 'files_write',
                                   'arguments': {'path': str(allowed / 'not-recovered.txt'),
                                                 'text': 'never sent'}}
            changed_source_id = RemoteAgent.internal_id(
                'delegated-child:' + source_child_id, changed_target_id)
            source_delegation.ledger.claim(Request(
                operation_id=changed_source_id, tool='devices_call',
                arguments=changed_target_args))
            source_delegation.bind_remote_operation(
                source_child_id, changed_target_id, device_id, 'files_write',
                target_child_id, Ledger.request_digest(Request(
                    operation_id=changed_source_id, tool='files_write',
                    arguments=changed_target_args['arguments'])))
            with sqlite3.connect(route_database) as route_db:
                route_db.execute('UPDATE delegated_routes SET target_child_id=? '
                                 'WHERE child_id=?', ('d' * 32, source_child_id))
            wrong_target = await remote('operations_get',
                                        {'operation_id': changed_target_id})
            assert wrong_target.state == 'failed'
            assert wrong_target.data['result']['reason'] == 'target_identity_changed'
            assert source_delegation.ledger.get(changed_source_id).state == 'running'
            with sqlite3.connect(route_database) as route_db:
                route_db.execute('UPDATE delegated_routes SET target_child_id=? '
                                 'WHERE child_id=?', (target_child_id, source_child_id))
            collision_id = uuid.uuid4().hex
            collision_internal = RemoteAgent.internal_id(
                'delegated-child:' + source_child_id, collision_id)
            preseed_path = allowed / 'target-preseeded.txt'
            target_session = target_binding.session('child:' + target_child_id)
            preseeded = await target_session.execute(Request(
                operation_id=collision_internal, tool='files_write',
                arguments={'path': str(preseed_path), 'text': 'other request'}))
            assert preseeded.state == 'completed'
            intended_path = allowed / 'intended-but-not-sent.txt'
            intended_arguments = {'path': str(intended_path), 'text': 'must not attribute'}
            source_delegation.ledger.claim(Request(
                operation_id=collision_internal, tool='devices_call', arguments={
                    'device_id': device_id, 'tool': 'files_write',
                    'arguments': intended_arguments}))
            source_delegation.bind_remote_operation(
                source_child_id, collision_id, device_id, 'files_write',
                target_child_id, Ledger.request_digest(Request(
                    operation_id=collision_internal, tool='files_write',
                    arguments=intended_arguments)))
            collision = await remote('operations_get', {'operation_id': collision_id})
            assert collision.state == 'failed'
            assert collision.data['reason'] == 'target_result_mismatch'
            assert source_delegation.ledger.get(collision_internal).state == 'running'
            assert preseed_path.read_text() == 'other request'
            assert not intended_path.exists()
            interrupted_id = uuid.uuid4().hex
            interrupted_args = {'device_id': device_id, 'tool': 'files_write',
                                'arguments': {'path': str(allowed / 'not-created.txt'),
                                              'text': 'one-time'}}
            source_delegation.ledger.claim(Request(
                operation_id=RemoteAgent.internal_id(
                    'delegated-child:' + source_child_id, interrupted_id),
                tool='devices_call', arguments=interrupted_args))
            sent_before_retry = len(forwarded_packets)
            uncertain = await remote('files_write', interrupted_args['arguments'],
                                     operation_id=interrupted_id)
            assert uncertain.state == 'unknown'
            assert len(forwarded_packets) == sent_before_retry
            assert not (allowed / 'not-created.txt').exists()
            outside = tmp_path / 'target-outside.txt'
            outside.write_text('private')
            denied_id = uuid.uuid4().hex
            denied = await remote('files_write', {'path': str(outside), 'text': 'bad'},
                                  operation_id=denied_id)
            assert denied.state == 'failed'
            assert not outside.read_text() == 'bad'
            assert await remote('files_write', {'path': str(outside), 'text': 'bad'},
                                operation_id=denied_id) == denied
            assert source_delegation.db.execute(
                'SELECT decision,reason FROM audit WHERE child_id=? AND operation_id=?',
                (source_child_id, denied_id)).fetchone() == ('denied', 'target_failed')
            assert (await remote('terminal_start', {})).state == 'failed'
            assert (await remote('files_read', {'path': str(allowed / 'source.txt')},
                                 selected_device=uuid.uuid4().hex)).state == 'failed'
            target_entered, target_release = threading.Event(), threading.Event()
            target_guard = target_delegation.local_file_guard

            def delayed_target_guard(identity, request):
                target_entered.set()
                assert target_release.wait(5)
                return target_guard(identity, request)

            raced_target_path = allowed / 'revoked-during-remote-send.txt'
            with patch.object(target_delegation, 'local_file_guard',
                              side_effect=delayed_target_guard):
                raced_task = asyncio.create_task(remote(
                    'files_write', {'path': str(raced_target_path), 'text': 'must not publish'}))
                try:
                    assert await asyncio.to_thread(target_entered.wait, 5)
                    target_delegation.revoke(target_child_id)
                finally:
                    target_release.set()
                raced_target = await raced_task
            assert raced_target.state == 'failed'
            assert not raced_target_path.exists()
            target_revoked = await remote('files_read', {'path': str(allowed / 'source.txt')})
            assert target_revoked.state == 'failed'
            routes = real_routes(source_directory / 'delegated-routes', vault=vault)
            try:
                routes.revoke(source_child_id, device_id)
            finally:
                routes.close()
            without_route = await remote('files_read', {'path': str(allowed / 'source.txt')})
            assert without_route.state == 'failed'
            source_delegation.revoke(source_child_id)
            assert (await remote('files_read', {'path': str(allowed / 'source.txt')})).state == (
                'failed')
    finally:
        source_delegation.close()
        source_authority.close()
        await source_engine.close()
        await target_server.close()
        target_delegation.close()
        target_authority.close()
        await target_engine.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('revoke_source', ['child', 'parent', 'route'])
async def test_ssh_route_uses_only_bound_target_child_bearer(tmp_path, revoke_source):
    source_engine = Engine(tmp_path / 'source-engine')
    source_authority = AuthorizationStore(
        tmp_path / 'source-authority', resource='https://source.example/mcp',
        known_tools=frozenset(source_engine.tools) | {'devices_call'})
    source_authority.register_client('client', frozenset({'https://client.example/callback'}))
    source_authority.enroll_device('owner', 'source', frozenset({'devices_call'}))
    code = source_authority.approve(
        owner='owner', device='source', client='client',
        redirect='https://client.example/callback', resource=source_authority.resource,
        tools=frozenset({'devices_call'}), challenge=pkce_s256('v' * 43))
    token = source_authority.exchange_code(
        code=code, verifier='v' * 43, client='client',
        redirect='https://client.example/callback', resource=source_authority.resource).value
    parent = source_authority.verify(token, resource=source_authority.resource)
    assert parent is not None
    devices_dir = tmp_path / 'devices'
    devices = DeviceStore(devices_dir)
    device_id = devices.add('Target', 'trusted-target')['device_id']
    devices.close()
    delegation = DelegatedTaskStore(tmp_path / 'source-delegation', source_authority)
    child_id = uuid.uuid4().hex
    child_bearer = delegation.issue(DelegatedTaskGrant(
        owner='owner', child_id=child_id, parent_grant_id=parent.grant_id,
        device_id=device_id, tools=frozenset({'files_read'}),
        expires_at=time.time() + 600))
    vault = MemoryVault()
    routes = DelegatedRouteStore(devices_dir / 'delegated-routes', vault=vault)
    routes.bind(child_id, device_id, 'ssh:trusted-target', 'target-secret',
                target_child_id='c' * 32)
    routes.close()
    constructed = []
    executed = []
    catalog_entered = asyncio.Event()
    continue_catalog = asyncio.Event()
    block_catalog = [False]

    class TargetBackend:
        def __init__(self, host, *, child_bearer):
            constructed.append((host, child_bearer))

        async def catalog(self):
            if block_catalog[0]:
                block_catalog[0] = False
                catalog_entered.set()
                await continue_catalog.wait()
            return [{'name': 'files_read', 'inputSchema': {'type': 'object'}}]

        async def execute(self, request):
            if request.tool == 'delegated_identity':
                return Reply(operation_id=request.operation_id, state='completed',
                             data={'child_id': 'c' * 32, 'device_id': 'local'})
            executed.append(request.operation_id)
            return Reply(operation_id=request.operation_id, state='completed',
                         data={'text': 'target child only'})

        async def close(self):
            pass

    backend = AuthorizedDeviceMCP(
        source_authority, source_engine, owner='owner', device='source',
        device_directory=devices_dir, delegated_tasks=delegation)
    try:
        with patch('anywhere_computer.authorized_http.DelegatedRouteStore',
                   side_effect=lambda directory: DelegatedRouteStore(directory, vault=vault)), \
             patch('anywhere_computer.authorized_http.SSHBackend', TargetBackend):
            assert await backend.authenticate(child_bearer) == 'child:' + child_id
            session = backend.session('child:' + child_id)
            result = await session.execute(Request(
                operation_id=uuid.uuid4().hex, tool='devices_call',
                arguments={'device_id': device_id, 'tool': 'files_read',
                           'arguments': {'path': '/target/allowed.txt'}}))
            assert result.state == 'completed'
            assert constructed == [('trusted-target', 'target-secret')]
            denied = await session.execute(Request(
                operation_id=uuid.uuid4().hex, tool='devices_call',
                arguments={'device_id': device_id, 'tool': 'files_write',
                           'arguments': {'path': '/target/outside.txt', 'text': 'bad'}}))
            assert denied.state == 'failed'
            assert constructed == [('trusted-target', 'target-secret')]
            block_catalog[0] = True
            raced_id = uuid.uuid4().hex
            racing = asyncio.create_task(session.execute(Request(
                operation_id=raced_id, tool='devices_call',
                arguments={'device_id': device_id, 'tool': 'files_read',
                           'arguments': {'path': '/target/allowed.txt'}})))
            await asyncio.wait_for(catalog_entered.wait(), timeout=2)
            if revoke_source == 'child':
                delegation.revoke(child_id)
            elif revoke_source == 'parent':
                source_authority.revoke(owner='owner', grant=parent.grant_id)
            else:
                route = DelegatedRouteStore(devices_dir / 'delegated-routes', vault=vault)
                try:
                    route.revoke(child_id, device_id)
                finally:
                    route.close()
            continue_catalog.set()
            raced = await asyncio.wait_for(racing, timeout=2)
            assert raced.state == 'failed'
            assert len(executed) == 1
            assert delegation.ledger.get(RemoteAgent.internal_id(
                'delegated-child:' + child_id, raced_id)).state == 'failed'
            audit = delegation.db.execute(
                'SELECT reason FROM audit WHERE operation_id=?', (raced_id,)).fetchone()
            assert audit == (('route_unavailable' if revoke_source == 'route'
                              else 'authorization_unavailable'),)
    finally:
        delegation.close()
        source_authority.close()
        await source_engine.close()


@pytest.mark.asyncio
async def test_owner_admin_issues_lists_and_revokes_child(tmp_path, unused_tcp_port):
    config = await setup(tmp_path, unused_tcp_port)
    owner = OwnerCredentials(tmp_path, resource=config.resource, owner=config.owner,
                             vault=MemoryVault())
    owner.initialize("synthetic owner password")
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    async with http_service(tmp_path, credentials=owner):
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{config.port}",
                                     trust_env=False) as http:
            parent_token = await authenticate(http)
        authority = AuthorizationStore(
            tmp_path / "http-server" / "authorization", resource=config.resource,
            known_tools=config.scopes,
        )
        try:
            parent = authority.verify(parent_token, resource=config.resource)
            assert parent is not None
        finally:
            authority.close()
        listed = manage_delegation(
            tmp_path, action="list", password="synthetic owner password", credentials=owner,
        )
        assert any(item["grant_id"] == parent.grant_id for item in listed["parents"])
        with pytest.raises(ValueError, match="authentication"):
            manage_delegation(tmp_path, action="list", password="wrong", credentials=owner)
        issued = manage_delegation(
            tmp_path, action="issue", password="synthetic owner password",
            parent_grant_id=parent.grant_id, tools=frozenset({"files_read"}),
            read_roots=(str(allowed),), credentials=owner,
        )
        assert isinstance(issued["bearer"], str)
        child_id = issued["child_id"]
        assert any(item["child_id"] == child_id and item["active"]
                   for item in manage_delegation(
                       tmp_path, action="list", password="synthetic owner password",
                       credentials=owner,
                   )["children"])
        revoked = manage_delegation(
            tmp_path, action="revoke", password="synthetic owner password",
            child_id=child_id, credentials=owner)
        assert revoked['source_child'] == 'revoked'
        assert revoked['source_route'] == 'not_applicable'
        assert revoked['target_child_grant'] == 'not_applicable'
        assert any(item["child_id"] == child_id and not item["active"]
                   for item in manage_delegation(
                       tmp_path, action="list", password="synthetic owner password",
                       credentials=owner,
                   )["children"])


@pytest.mark.asyncio
async def test_owner_admin_binds_and_revokes_remote_child_route(tmp_path, unused_tcp_port):
    scopes = frozenset({'devices_call', 'files_read', 'files_write', 'operations_get'})
    config = await setup(tmp_path, unused_tcp_port, scopes=scopes)
    owner = OwnerCredentials(tmp_path, resource=config.resource, owner=config.owner,
                             vault=MemoryVault())
    owner.initialize('synthetic owner password')
    devices = DeviceStore(tmp_path)
    try:
        device_id = devices.add_http('Target', 'https://target.example/mcp',
                                     'target-client', 'target-profile')['device_id']
    finally:
        devices.close()
    async with http_service(tmp_path, credentials=owner):
        async with httpx.AsyncClient(base_url=f'http://127.0.0.1:{config.port}',
                                     trust_env=False) as http:
            parent_token = await authenticate(http, scopes=scopes)
        authority = AuthorizationStore(
            tmp_path / 'http-server' / 'authorization', resource=config.resource,
            known_tools=config.scopes)
        try:
            parent = authority.verify(parent_token, resource=config.resource)
            assert parent is not None
        finally:
            authority.close()
        issued = manage_delegation(
            tmp_path, action='issue', password='synthetic owner password',
            parent_grant_id=parent.grant_id, device_id=device_id,
            tools=frozenset({'files_read'}), credentials=owner)
        child_id = issued['child_id']
        vault = MemoryVault()
        real_routes = DelegatedRouteStore
        with patch('anywhere_computer.delegation_admin.DelegatedRouteStore',
                   side_effect=lambda directory: real_routes(directory, vault=vault)):
            linked = manage_delegation(
                tmp_path, action='route', password='synthetic owner password',
                child_id=child_id, target_bearer='target-secret',
                target_child_id='c' * 32, credentials=owner)
            assert linked == {'child_id': child_id, 'device_id': device_id,
                              'target_route': 'bound', 'target_child_id': 'c' * 32}
            listed = manage_delegation(
                tmp_path, action='list', password='synthetic owner password',
                credentials=owner)
            assert next(item for item in listed['children']
                        if item['child_id'] == child_id)['target_child_id'] == 'c' * 32
            assert 'target-secret' not in str(linked)
            route = real_routes(tmp_path / 'delegated-routes', vault=vault)
            try:
                assert route.bearer(child_id, device_id, 'https://target.example/mcp') == (
                    'target-secret')
            finally:
                route.close()
            revoked = manage_delegation(
                tmp_path, action='revoke', password='synthetic owner password',
                child_id=child_id, credentials=owner)
            assert revoked['revoked'] is True
            assert revoked['source_child'] == 'revoked'
            assert revoked['source_route'] == 'revoked'
            assert revoked['target_child_grant'] == 'not_revoked_here'
            assert revoked['target_child_id'] == 'c' * 32
            listed_after = manage_delegation(
                tmp_path, action='list', password='synthetic owner password',
                credentials=owner)
            assert next(item for item in listed_after['children']
                        if item['child_id'] == child_id)['target_child_id'] == 'c' * 32
            route = real_routes(tmp_path / 'delegated-routes', vault=vault)
            try:
                with pytest.raises(ValueError, match='unavailable'):
                    route.bearer(child_id, device_id, 'https://target.example/mcp')
            finally:
                route.close()
            unlinked = manage_delegation(
                tmp_path, action='unroute', password='synthetic owner password',
                child_id=child_id, credentials=owner)
            assert unlinked['target_route'] == 'revoked'
            assert unlinked['target_child_id'] == 'c' * 32


@pytest.mark.skipif(os.name == 'nt', reason='requires POSIX delegated replacement')
async def test_delegated_and_regular_writes_share_mutation_lock(tmp_path, monkeypatch):
    from anywhere_computer.files import sha256
    from anywhere_computer.locking import ProcessLock
    from anywhere_computer.models import WriteFile

    engine = Engine(tmp_path / 'engine')
    tools = frozenset({'files_write', 'operations_get'})
    authority = AuthorizationStore(tmp_path / 'authority', resource='https://fixture.example/mcp',
                                   known_tools=frozenset(engine.tools))
    authority.register_client('chat', frozenset({'https://chat.example/callback'}))
    authority.enroll_device('owner', 'gateway', tools)
    code = authority.approve(
        owner='owner', device='gateway', client='chat', redirect='https://chat.example/callback',
        resource=authority.resource, tools=tools, challenge=pkce_s256('v' * 43))
    token = authority.exchange_code(
        code=code, verifier='v' * 43, client='chat', redirect='https://chat.example/callback',
        resource=authority.resource).value
    delegation = DelegatedTaskStore(tmp_path / 'delegation', authority)
    backend = AuthorizedDeviceMCP(authority, engine, owner='owner', device='gateway',
                                  delegated_tasks=delegation)
    parent_id = await backend.authenticate(token)
    assert parent_id is not None
    allowed = tmp_path / 'allowed'
    allowed.mkdir()
    path = allowed / 'shared.txt'
    path.write_bytes(b'original')
    original_hash = sha256(b'original')
    child_id = uuid.uuid4().hex
    delegation.issue(DelegatedTaskGrant(
        owner='owner', child_id=child_id, parent_grant_id=parent_id, device_id='local',
        tools=tools, write_roots=(str(allowed.resolve()),), expires_at=time.time() + 600))
    child = backend.session('child:' + child_id)
    entered, release, regular_attempted = threading.Event(), threading.Event(), threading.Event()
    original_replace = os.replace
    original_lock = ProcessLock.__enter__
    main_thread = threading.get_ident()

    def held_replace(source, destination, **kwargs):
        if kwargs.get('src_dir_fd') is not None:
            entered.set()
            assert release.wait(10)
        return original_replace(source, destination, **kwargs)

    def observed_lock(lock):
        if threading.get_ident() != main_thread and entered.is_set():
            regular_attempted.set()
        return original_lock(lock)

    monkeypatch.setattr(os, 'replace', held_replace)
    monkeypatch.setattr(ProcessLock, '__enter__', observed_lock)
    delegated = asyncio.create_task(child.execute(Request(
        operation_id=uuid.uuid4().hex, tool='files_write', arguments={
            'path': str(path), 'mode': 'replace', 'expected_sha256': original_hash,
            'text': 'delegated'})))
    regular = None
    lock_busy = False
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        try:
            with ProcessLock(engine.files.locks / sha256(str(path.resolve()).encode())):
                pass
        except TimeoutError:
            lock_busy = True
        regular = asyncio.create_task(asyncio.to_thread(engine.files.write, WriteFile(
            path=str(path), mode='replace', expected_sha256=original_hash, text='regular')))
        assert await asyncio.to_thread(regular_attempted.wait, 5)
        if not lock_busy:
            # On the defective implementation the second writer can acknowledge
            # success while delegated publication is still paused.
            await asyncio.wait_for(asyncio.shield(regular), 5)
    finally:
        release.set()
        result = await delegated
        regular_result = ((await asyncio.gather(regular, return_exceptions=True))[0]
                          if regular else None)
        await backend.close()
        delegation.close()
        authority.close()
        await engine.close()
    assert lock_busy, 'Delegated publication did not hold the regular file mutation lock'
    assert result.state == 'completed'
    assert isinstance(regular_result, ValueError), regular_result
    assert path.read_bytes() == b'delegated'
