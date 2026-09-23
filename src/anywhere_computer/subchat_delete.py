"""Bound, one-shot ordinary Chat deletion; no credential acquisition or retry."""
from __future__ import annotations

import re
import sqlite3
from typing import Literal

from pydantic import Field

from .models import Contract
from .subchat import Subchats, SubchatUnsupported
from .subchat_state import SubchatAccountMismatch, SubchatSubmission, SubchatSubmissions

CONVERSATION_ID = re.compile(r'[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}\Z')


class DeleteRequest(Contract):
    operation_id: str = Field(pattern=r'^[0-9a-f]{32}$')
    conversation_id: str = Field(pattern=r'^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$')


class DeleteResult(Contract):
    operation_id: str
    conversation_id: str
    state: Literal['unknown', 'deleted']
    automatic_retry: Literal[False] = False


class SubchatDeletionUnknown(RuntimeError):
    """A deletion PATCH may have reached Chat; inspect before any further action."""


class DeletionLedger:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        with connection:
            connection.execute('CREATE TABLE IF NOT EXISTS subchat_http_deletions ('
                               'account_id TEXT NOT NULL, conversation_id TEXT NOT NULL, '
                               'operation_id TEXT NOT NULL, owner TEXT, '
                               'state TEXT NOT NULL, '
                               'PRIMARY KEY (account_id, conversation_id))')

    def check_active_peers(self, request: DeleteRequest, *, owner: str | None,
                           store: SubchatSubmissions) -> None:
        for peer_operation, peer_owner in self.connection.execute(
                'SELECT operation_id, owner FROM subchat_submissions'):
            if peer_operation == request.operation_id and peer_owner == owner:
                continue
            peer = store.get(peer_operation, owner=peer_owner)
            if ((peer.conversation_id == request.conversation_id
                    or peer.requested_conversation_id == request.conversation_id)
                    and peer.state in {'queued', 'prepared', 'sending', 'submitted'}):
                raise ValueError('Conversation has an active saved submission')

    def get(self, request: DeleteRequest, *, account_id: str,
            owner: str | None) -> DeleteResult | None:
        row = self.connection.execute(
            'SELECT operation_id, owner, state FROM subchat_http_deletions '
            'WHERE account_id=? AND conversation_id=?',
            (account_id, request.conversation_id)).fetchone()
        if row is None:
            return None
        if row[0] != request.operation_id or row[1] != owner:
            raise ValueError('Conversation deletion was claimed by another operation')
        return DeleteResult(operation_id=request.operation_id,
                            conversation_id=request.conversation_id, state=row[2])

    def claim(self, request: DeleteRequest, *, account_id: str, owner: str | None,
              store: SubchatSubmissions, verified: SubchatSubmission,
              ) -> tuple[DeleteResult, bool]:
        with self.connection:
            self.connection.execute('BEGIN IMMEDIATE')
            if store.get(request.operation_id, owner=owner) != verified:
                raise ValueError('Saved conversation changed after deletion preflight')
            self.check_active_peers(request, owner=owner, store=store)
            prior = self.get(request, account_id=account_id, owner=owner)
            if prior is not None:
                return prior, False
            self.connection.execute(
                'INSERT INTO subchat_http_deletions VALUES (?,?,?,?,?)',
                (account_id, request.conversation_id, request.operation_id,
                 owner, 'unknown'))
        return DeleteResult(operation_id=request.operation_id,
                            conversation_id=request.conversation_id, state='unknown'), True

    def confirm(self, request: DeleteRequest, *, account_id: str,
                owner: str | None) -> DeleteResult:
        with self.connection:
            cursor = self.connection.execute(
                "UPDATE subchat_http_deletions SET state='deleted' "
                'WHERE account_id=? AND conversation_id=? AND operation_id=? '
                "AND owner IS ? AND state='unknown'",
                (account_id, request.conversation_id, request.operation_id, owner))
            if cursor.rowcount != 1:
                raise ValueError('Deletion claim changed during confirmation')
        return DeleteResult(operation_id=request.operation_id,
                            conversation_id=request.conversation_id, state='deleted')


async def delete_saved(service: Subchats, request: DeleteRequest,
                       *, owner: str | None) -> DeleteResult:
    """Verify saved identity and remote history, then claim before the sole PATCH."""
    saved = service.store.get(request.operation_id, owner=owner)
    if (saved.state != 'completed'
            or saved.conversation_id != request.conversation_id
            or CONVERSATION_ID.fullmatch(request.conversation_id) is None
            or saved.user_message_id is None):
        raise ValueError('Saved operation does not identify a confirmed conversation')
    account_id = saved.provider_account_id
    if account_id is None:
        raise SubchatAccountMismatch('Saved operation has no bound Chat account')
    ledger = DeletionLedger(service.store.connection)
    prior = ledger.get(request, account_id=account_id, owner=owner)
    if prior is not None:
        return prior
    ledger.check_active_peers(request, owner=owner, store=service.store)
    backend = service.backend
    verify = getattr(backend, 'verify_delete_target', None)
    patch = getattr(backend, 'patch_delete', None)
    if verify is None or patch is None:
        raise SubchatUnsupported('http_delete_unavailable')
    await verify(saved)
    claim, inserted = ledger.claim(request, account_id=account_id, owner=owner,
                                   store=service.store, verified=saved)
    if not inserted:
        return claim
    try:
        accepted = await patch(saved)
    except Exception:
        raise SubchatDeletionUnknown('Deletion outcome is unknown') from None
    if not accepted:
        raise SubchatDeletionUnknown('Deletion outcome is unknown')
    try:
        return ledger.confirm(request, account_id=account_id, owner=owner)
    except Exception:
        raise SubchatDeletionUnknown(
            'Deletion succeeded but its local receipt is unknown') from None
