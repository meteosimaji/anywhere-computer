"""Intermediate submission receipts in the existing operation database.

A persisted 'sending' record is intentionally not reset on restart. Its caller
may inspect the conversation, but may not dispatch the same submission again.
"""

import sqlite3
from typing import Literal

from pydantic import Field

from .models import Contract


class SubchatSubmission(Contract):
    operation_id: str = Field(pattern=r'^[0-9a-f]{32}$')
    prompt: str = Field(min_length=1, max_length=100_000)
    model: str = Field(min_length=1, max_length=256)
    effort: str = Field(min_length=1, max_length=256)
    state: Literal['prepared', 'sending', 'submitted', 'completed'] = 'prepared'
    requested_conversation_id: str | None = None
    conversation_id: str | None = None
    user_message_id: str | None = None
    answer_message_id: str | None = None
    answer: str | None = None


class SubchatSubmissions:
    """Uses the ledger's connection; does not own or close it."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        with connection:
            connection.execute('CREATE TABLE IF NOT EXISTS subchat_submissions ('
                               'operation_id TEXT PRIMARY KEY, owner TEXT, body TEXT NOT NULL)')

    def get(self, operation_id: str, *, owner: str | None) -> SubchatSubmission:
        row = self.connection.execute(
            'SELECT owner, body FROM subchat_submissions WHERE operation_id=?',
            (operation_id,),
        ).fetchone()
        if row is None or row[0] != owner:
            raise ValueError('Unknown subchat submission')
        return SubchatSubmission.model_validate_json(row[1])

    def prepare(self, operation_id: str, prompt: str, model: str, effort: str,
                *, owner: str | None, conversation_id: str | None = None) -> SubchatSubmission:
        if conversation_id is not None and not conversation_id.strip():
            raise ValueError('Conversation identity must not be empty')
        proposed = SubchatSubmission(operation_id=operation_id, prompt=prompt,
                                     model=model, effort=effort,
                                     requested_conversation_id=conversation_id,
                                     conversation_id=conversation_id)
        with self.connection:
            self.connection.execute(
                'INSERT OR IGNORE INTO subchat_submissions VALUES (?,?,?)',
                (operation_id, owner, proposed.model_dump_json()),
            )
        existing = self.get(operation_id, owner=owner)
        if (existing.prompt, existing.model, existing.effort,
            existing.requested_conversation_id) != (prompt, model, effort, conversation_id):
            raise ValueError('Subchat submission ID was already used for different arguments')
        return existing

    def _replace(self, old: SubchatSubmission, new: SubchatSubmission,
                 owner: str | None) -> SubchatSubmission:
        with self.connection:
            cursor = self.connection.execute(
                'UPDATE subchat_submissions SET body=? '
                'WHERE operation_id=? AND owner IS ? AND body=?',
                (new.model_dump_json(), old.operation_id, owner, old.model_dump_json()),
            )
            if cursor.rowcount != 1:
                raise ValueError('Subchat submission changed; inspect it before continuing')
        return new

    def begin_send(self, operation_id: str, *, owner: str | None,
                   conversation_id: str | None = None) -> SubchatSubmission:
        old = self.get(operation_id, owner=owner)
        if old.state != 'prepared':
            raise ValueError('Submission may already have been sent; recover it without resending')
        if conversation_id is not None and not conversation_id.strip():
            raise ValueError('Conversation identity must not be empty')
        if old.conversation_id is not None and conversation_id not in {None, old.conversation_id}:
            raise ValueError('Conversation identity does not match the prepared submission')
        return self._replace(old, old.model_copy(update={
            'state': 'sending', 'conversation_id': old.conversation_id or conversation_id}), owner)

    def submitted(self, operation_id: str, conversation_id: str, user_message_id: str,
                  *, owner: str | None) -> SubchatSubmission:
        old = self.get(operation_id, owner=owner)
        if not conversation_id.strip() or not user_message_id.strip():
            raise ValueError('Observed conversation and user identities are required')
        if old.conversation_id is not None and old.conversation_id != conversation_id:
            raise ValueError('Observed conversation does not match the submission')
        if old.state in {'submitted', 'completed'}:
            if old.user_message_id != user_message_id:
                raise ValueError('Observed message does not match the submission')
            return old
        if old.state != 'sending':
            raise ValueError('Submission has not entered the send stage')
        return self._replace(old, old.model_copy(update={
            'state': 'submitted', 'conversation_id': conversation_id,
            'user_message_id': user_message_id}), owner)

    def complete(self, operation_id: str, answer_message_id: str, answer: str,
                 *, owner: str | None) -> SubchatSubmission:
        old = self.get(operation_id, owner=owner)
        if not answer_message_id.strip() or not answer or answer_message_id == old.user_message_id:
            raise ValueError('A distinct observed answer identity and text are required')
        if old.state == 'completed':
            if (old.answer_message_id, old.answer) != (answer_message_id, answer):
                raise ValueError('Completed subchat answer cannot be replaced')
            return old
        if old.state != 'submitted':
            raise ValueError('Submission identity must be observed before its answer')
        return self._replace(old, old.model_copy(update={
            'state': 'completed', 'answer_message_id': answer_message_id, 'answer': answer}), owner)
