"""Durable admission gates and failures, using real SQLite locks and transactions."""

import sqlite3
import time
import uuid
from contextlib import closing

import pytest
from test_authorization import REDIRECT, RESOURCE, TOOLS, approve, redeem

from anywhere_computer.authorization import AuthorizationError, AuthorizationStore
from anywhere_computer.delegated_tasks import DelegatedTaskGrant, DelegatedTaskStore
from anywhere_computer.grant_revocation import (
    flush_pending_audit,
    record_pending_audit,
    revocation_requested,
)
from anywhere_computer.models import Request


@pytest.fixture
def stores(tmp_path):
    authority = AuthorizationStore(tmp_path / 'authority', resource=RESOURCE, known_tools=TOOLS)
    authority.register_client('client', frozenset({REDIRECT}))
    authority.enroll_device('owner', 'device', TOOLS)
    token = redeem(authority, approve(authority))
    parent = authority.verify(token.value, resource=RESOURCE)
    assert parent is not None
    delegated = DelegatedTaskStore(tmp_path / 'delegated', authority)
    path = tmp_path / 'selected.txt'
    path.write_text('synthetic content', encoding='utf-8')
    child = uuid.uuid4().hex
    delegated.issue(DelegatedTaskGrant(owner='owner', child_id=child,
        parent_grant_id=parent.grant_id, device_id='local', tools=frozenset({'files_read'}),
        read_files=(str(path.resolve()),), expires_at=time.time() + 600))
    try:
        yield authority, delegated, parent, child, token, path
    finally:
        delegated.close()
        authority.close()


@pytest.mark.parametrize('kind', ['parent', 'child'])
def test_pending_intent_survives_reopen_and_blocks_until_original_update_commits(stores, kind):
    authority, delegated, parent, child, token, _ = stores
    database = authority.database if kind == 'parent' else delegated.database
    with closing(sqlite3.connect(database)) as reservation:
        reservation.execute('BEGIN IMMEDIATE')
        state = (authority.revoke(owner='owner', grant=parent.grant_id) if kind == 'parent'
                 else delegated.revoke(child))
        assert state == 'pending'
        reopened = AuthorizationStore(authority.database.parent, resource=RESOURCE,
                                      known_tools=TOOLS)
        other = DelegatedTaskStore(delegated.directory, reopened)
        try:
            assert other.current(child) is None
            assert other.verify_token('unrecognized synthetic bearer') is None
            if kind == 'parent':
                assert reopened.verify(token.value, resource=RESOURCE) is None
            else:
                assert other.revocation_state(child) == 'pending'
        finally:
            other.close()
            reopened.close()
        reservation.rollback()
    assert delegated.current(child) is None
    state = (authority.revoke(owner='owner', grant=parent.grant_id) if kind == 'parent'
             else delegated.revoke(child))
    assert state == 'revoked'


def test_owner_mismatch_and_missing_grants_never_save_a_revocation(stores):
    authority, delegated, parent, child, _, _ = stores
    with pytest.raises(AuthorizationError):
        authority.revoke(owner='different-owner', grant=parent.grant_id)
    with pytest.raises(AuthorizationError):
        authority.revoke(owner='owner', grant=uuid.uuid4().hex)
    with pytest.raises(ValueError, match='unavailable'):
        delegated.revoke(uuid.uuid4().hex)
    assert not revocation_requested(authority.database, parent.grant_id)
    assert delegated.current(child) is not None


@pytest.mark.parametrize('stage', ['intent', 'confirmation'])
def test_failed_commit_never_claims_accepted_or_confirmed_revocation(stores, stage):
    authority, delegated, parent, child, _, _ = stores
    # Prepare the real queue schema with a denial, then fault its SQL transaction.
    record_pending_audit(authority.database, uuid.uuid4().hex, child, 'files_read',
                         'synthetic_denial', time.time())
    queue = authority.database.with_name('authorization-revocations.sqlite3')
    with closing(sqlite3.connect(queue)) as db, db:
        sql = ('BEFORE INSERT' if stage == 'intent' else 'BEFORE UPDATE')
        db.execute(f"CREATE TRIGGER reject_commit {sql} ON revocations "
                   "BEGIN SELECT RAISE(ABORT, 'synthetic commit failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match='synthetic commit failure'):
        authority.revoke(owner='owner', grant=parent.grant_id)
    assert revocation_requested(authority.database, parent.grant_id) == (stage == 'confirmation')
    assert bool(authority.db.execute('SELECT revoked FROM grants WHERE id=?',
                                    (parent.grant_id,)).fetchone()[0]) == (stage == 'confirmation')
    if stage == 'intent':
        assert delegated.current(child) is not None
    with closing(sqlite3.connect(queue)) as db, db:
        db.execute('DROP TRIGGER reject_commit')
    assert authority.revoke(owner='owner', grant=parent.grant_id) == 'revoked'
    assert delegated.current(child) is None


def test_denial_audit_is_saved_while_locked_and_reconciled_only_once(stores):
    _, delegated, _, child, _, _ = stores
    operation = uuid.uuid4().hex
    with closing(sqlite3.connect(delegated.database)) as reservation:
        reservation.execute('BEGIN IMMEDIATE')
        record_pending_audit(delegated.database, operation, child, 'files_read',
                             'authorization_unavailable', time.time())
        flush_pending_audit(delegated.database)
        assert delegated.db.execute('SELECT count(*) FROM audit').fetchone() == (0,)
        reservation.rollback()
    flush_pending_audit(delegated.database)
    flush_pending_audit(delegated.database)
    assert delegated.db.execute('SELECT operation_id,child_id,reason FROM audit').fetchall() == [
        (operation, child, 'authorization_unavailable')]


def test_bindings_written_before_migration_and_new_queue_survive_reopen(stores):
    authority, delegated, _, child, _, path = stores
    old = Request(operation_id=uuid.uuid4().hex, tool='files_read', arguments={'path': str(path)})
    new = old.model_copy(update={'operation_id': uuid.uuid4().hex})
    with delegated.db:
        delegated.db.execute('INSERT INTO operation_inputs VALUES (?,?,?)',
            (child, old.operation_id, delegated.ledger.request_digest(old)))
    delegated.bind_request(child, old)
    delegated.bind_request(child, new)
    other = DelegatedTaskStore(delegated.directory, authority)
    try:
        for request in (old, new):
            other.bind_request(child, request)
            with pytest.raises(ValueError, match='another request'):
                other.bind_request(child, request.model_copy(update={'arguments': {}}))
    finally:
        other.close()


@pytest.mark.parametrize('corruption', ['resource', 'column'])
def test_current_authorization_schema_is_checked_on_read_only_reopen(stores, corruption):
    authority, _, _, _, _, _ = stores
    with authority.db:
        if corruption == 'resource':
            authority.db.execute('UPDATE settings SET resource=?', ('https://other.example/mcp',))
        else:
            authority.db.execute('ALTER TABLE tokens RENAME COLUMN expires TO incorrect')
    with pytest.raises((ValueError, sqlite3.OperationalError)):
        AuthorizationStore(authority.database.parent, resource=RESOURCE, known_tools=TOOLS)


def test_revocation_store_failure_does_not_allow_file_guard(stores):
    _, delegated, _, child, _, path = stores
    queue = delegated.database.with_name('delegated-tasks-revocations.sqlite3')
    queue.mkdir()
    request = Request(operation_id=uuid.uuid4().hex, tool='files_read',
                      arguments={'path': str(path)})
    with pytest.raises(ValueError, match='storage is unavailable'):
        with delegated.local_file_guard(child, request):
            pytest.fail('File guard admitted with unavailable revocation storage')
