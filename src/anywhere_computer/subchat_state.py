"""Intermediate submission receipts in the existing operation database.

A persisted 'sending' record is intentionally not reset on restart. Its caller
may inspect the conversation, but may not dispatch the same submission again.
"""

import sqlite3
from typing import Literal

from pydantic import Field

from .models import Contract


class SubchatInputReference(Contract):
    reference: str = Field(min_length=1, max_length=4096)
    sha256: str = Field(pattern=r'^[0-9a-f]{64}$')


class SubchatWorkContext(Contract):
    """Caller-supplied provenance, not authentication or filesystem authorization."""

    task_id: str = Field(pattern=r'^[0-9a-f]{32}$')
    parent_operation_id: str | None = Field(default=None, pattern=r'^[0-9a-f]{32}$')
    device_id: str = Field(min_length=1, max_length=128)
    workspace: str = Field(min_length=1, max_length=4096)
    inputs: tuple[SubchatInputReference, ...] = Field(default=(), max_length=32)


class SubchatSubmission(Contract):
    operation_id: str = Field(pattern=r'^[0-9a-f]{32}$')
    prompt: str = Field(min_length=1, max_length=100_000)
    model: str = Field(min_length=1, max_length=256)
    effort: str = Field(min_length=1, max_length=256)
    state: Literal[
        'queued', 'prepared', 'sending', 'submitted', 'completed', 'cancelled'
    ] = 'prepared'
    after_operation_id: str | None = Field(default=None, pattern=r'^[0-9a-f]{32}$')
    expected_last_user_message_id: str | None = None
    requested_conversation_id: str | None = None
    conversation_id: str | None = None
    baseline_message_ids: tuple[str, ...] = ()
    user_message_id: str | None = None
    answer_message_id: str | None = None
    answer: str | None = None
    work_context: SubchatWorkContext | None = None


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
                *, owner: str | None, conversation_id: str | None = None,
                work_context: SubchatWorkContext | None = None,
                after_operation_id: str | None = None) -> SubchatSubmission:
        if conversation_id is not None and not conversation_id.strip():
            raise ValueError('Conversation identity must not be empty')
        if work_context is not None and work_context.parent_operation_id is not None:
            if work_context.parent_operation_id == operation_id:
                raise ValueError('A submission cannot be its own parent')
            self.get(work_context.parent_operation_id, owner=owner)
        expected_last_user_message_id = None
        if after_operation_id is not None:
            if after_operation_id == operation_id:
                raise ValueError('A message cannot wait for itself')
            target = self.get(after_operation_id, owner=owner)
            if target.state not in {'submitted', 'completed'} or target.user_message_id is None:
                raise ValueError('Queue target identity must be confirmed first')
            if conversation_id != target.conversation_id:
                raise ValueError('Queue target conversation does not match')
            expected_last_user_message_id = target.user_message_id
        proposed = SubchatSubmission(operation_id=operation_id, prompt=prompt,
                                     model=model, effort=effort,
                                     requested_conversation_id=conversation_id,
                                     conversation_id=conversation_id, work_context=work_context,
                                     state='queued' if after_operation_id else 'prepared',
                                     after_operation_id=after_operation_id,
                                     expected_last_user_message_id=expected_last_user_message_id)
        with self.connection:
            self.connection.execute(
                'INSERT OR IGNORE INTO subchat_submissions VALUES (?,?,?)',
                (operation_id, owner, proposed.model_dump_json()),
            )
        existing = self.get(operation_id, owner=owner)
        if (existing.prompt, existing.model, existing.effort,
            existing.requested_conversation_id, existing.work_context,
            existing.after_operation_id) != (
                prompt, model, effort, conversation_id, work_context, after_operation_id):
            raise ValueError('Subchat submission ID was already used for different arguments')
        return existing

    def _replace(self, old: SubchatSubmission, new: SubchatSubmission,
                 owner: str | None) -> SubchatSubmission:
        with self.connection:
            row = self.connection.execute(
                'SELECT body FROM subchat_submissions WHERE operation_id=? AND owner IS ?',
                (old.operation_id, owner),
            ).fetchone()
            if row is None or SubchatSubmission.model_validate_json(row[0]) != old:
                raise ValueError('Subchat submission changed; inspect it before continuing')
            cursor = self.connection.execute(
                'UPDATE subchat_submissions SET body=? '
                'WHERE operation_id=? AND owner IS ? AND body=?',
                (new.model_dump_json(), old.operation_id, owner, row[0]),
            )
            if cursor.rowcount != 1:
                raise ValueError('Subchat submission changed; inspect it before continuing')
        return new

    def cancel(self, operation_id: str, *, owner: str | None) -> SubchatSubmission:
        """Cancel only a not-yet-reserved submission, never stop a provider turn."""
        old = self.get(operation_id, owner=owner)
        if old.state == 'cancelled':
            return old
        if old.state not in {'queued', 'prepared'}:
            raise ValueError('Submission may already be dispatched; cancellation is unavailable')
        return self._replace(old, old.model_copy(update={'state': 'cancelled'}), owner)

    def begin_send(self, operation_id: str, *, owner: str | None,
                   conversation_id: str | None = None,
                   baseline_message_ids: tuple[str, ...] = ()) -> SubchatSubmission:
        old = self.get(operation_id, owner=owner)
        if old.state not in {'prepared', 'queued'}:
            raise ValueError('Submission may already have been sent; recover it without resending')
        if old.after_operation_id is not None:
            target = self.get(old.after_operation_id, owner=owner)
            if target.state != 'completed':
                raise ValueError('Queue target has not completed')
        if conversation_id is not None and not conversation_id.strip():
            raise ValueError('Conversation identity must not be empty')
        if old.conversation_id is not None and conversation_id not in {None, old.conversation_id}:
            raise ValueError('Conversation identity does not match the prepared submission')
        if (len(baseline_message_ids) > 10_000
                or len(set(baseline_message_ids)) != len(baseline_message_ids)
                or any(not item.strip() or len(item) > 256 for item in baseline_message_ids)):
            raise ValueError('Invalid baseline message identities')
        return self._replace(old, old.model_copy(update={
            'state': 'sending', 'conversation_id': old.conversation_id or conversation_id,
            'baseline_message_ids': baseline_message_ids}), owner)

    def submitted(self, operation_id: str, conversation_id: str, user_message_id: str,
                  *, owner: str | None) -> SubchatSubmission:
        old = self.get(operation_id, owner=owner)
        if not conversation_id.strip() or not user_message_id.strip():
            raise ValueError('Observed conversation and user identities are required')
        if old.conversation_id is not None and old.conversation_id != conversation_id:
            raise ValueError('Observed conversation does not match the submission')
        if user_message_id in old.baseline_message_ids:
            raise ValueError('Observed message predates this submission')
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
