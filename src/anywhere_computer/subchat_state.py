"""Intermediate submission receipts in the existing operation database.

A persisted 'sending' record is intentionally not reset on restart. Its caller
may inspect the conversation, but may not dispatch the same submission again.
"""

from __future__ import annotations

import builtins
import json
import re
import sqlite3
import time
from typing import Literal

from pydantic import ConfigDict, Field, JsonValue

from .models import Contract
from .subchat_content import SubchatResources


class SubchatAccountMismatch(ValueError):
    """The observed account does not match a saved operation or active reader."""

    code = 'account_mismatch'


class SubchatOperationNotFound(ValueError):
    """No operation with this ID is visible in the selected owner and ledger."""

    code = 'unknown_operation'


class SubchatRequestConflict(ValueError):
    """An existing operation ID has different immutable request arguments."""

    code = 'request_conflict'


class SubchatCommittedMutationConflict(SubchatRequestConflict):
    """The request ID already committed a mutation with another binding."""


class SubchatConcurrentSend(ValueError):
    """Another durable operation is active in the same conversation."""

    code = 'concurrent_send'

    def __init__(self, blocking_operation_id: str | None) -> None:
        self.blocking_operation_id = blocking_operation_id
        super().__init__('Conversation has another active send; recover it first')


class SubchatAutoQueueDisarmed(ValueError):
    """An unattended worker lost its explicit opt-in before send reservation."""


class SubchatSelectionError(ValueError):
    """Safe, field-specific local catalog validation failure before dispatch."""

    code = 'invalid_parameter'

    def __init__(self, field: Literal['choice_id', 'http_selection', 'version_id', 'preset_id',
                                     'model_slug', 'thinking_effort', 'model', 'effort'],
                 reason: Literal['required', 'invalid', 'not_found', 'unavailable', 'mismatch',
                                 'ambiguous']) -> None:
        self.field = field
        self.reason = reason
        super().__init__(f'{field}: {reason}')


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


class SubchatHTTPSelection(Contract):
    """Exact observed catalog choice; null effort is an explicit value, not a wildcard."""

    model_config = ConfigDict(extra='forbid', strict=True)

    version_id: str = Field(min_length=1, max_length=256)
    preset_id: int
    model_slug: str = Field(min_length=1, max_length=256)
    thinking_effort: str | None = Field(min_length=1, max_length=256)


class SubchatReportedSettings(Contract):
    """Provider-reported answer settings, not proof of equivalence to UI labels."""

    model_slug: str | None = Field(default=None, min_length=1, max_length=256)
    thinking_effort: str | None = Field(default=None, min_length=1, max_length=256)


class SubchatSubmission(Contract):
    operation_id: str = Field(pattern=r'^[0-9a-f]{32}$')
    prompt: str = Field(min_length=1, max_length=100_000)
    model: str = Field(min_length=1, max_length=256)
    effort: str = Field(min_length=1, max_length=256)
    state: Literal[
        'queued', 'prepared', 'sending', 'submitted', 'completed', 'cancelled',
        'interrupted', 'preflight_failed'
    ] = 'prepared'
    after_operation_id: str | None = Field(default=None, pattern=r'^[0-9a-f]{32}$')
    expected_last_user_message_id: str | None = None
    requested_conversation_id: str | None = None
    conversation_id: str | None = None
    baseline_message_ids: tuple[str, ...] = ()
    # Absent on saved operations created before baseline identities were versioned.
    baseline_identity_kind: Literal['legacy_turn_key', 'message_id', 'empty'] | None = None
    user_message_id: str | None = None
    answer_message_id: str | None = None
    answer: str | None = None
    answer_type: Literal['text', 'image', 'multimodal'] | None = None
    interruption_reason: Literal['provider_interrupted', 'output_limit'] | None = None
    generation_http_status: int | None = Field(default=None, ge=400, le=599)
    reported_settings: SubchatReportedSettings | None = None
    provider_account_id: str | None = Field(default=None, min_length=1, max_length=256)
    work_context: SubchatWorkContext | None = None
    resources: SubchatResources | None = None
    http_selection: SubchatHTTPSelection | None = None

    @property
    def wire_prompt(self) -> str:
        return self.resources.prompt(self.prompt) if self.resources else self.prompt


def provider_receipt_state(submission: SubchatSubmission) -> Literal[
    'not_sent', 'unconfirmed', 'confirmed'
]:
    """Classify saved provider evidence, independently of tool-call completion."""
    if submission.state in {'queued', 'prepared', 'cancelled', 'preflight_failed'}:
        return 'not_sent'
    if submission.state == 'sending':
        return 'unconfirmed'
    if submission.conversation_id and submission.user_message_id:
        return 'confirmed'
    return 'unconfirmed'


def confirmed_conversation_url(submission: SubchatSubmission) -> str | None:
    """Link only a conversation whose matching user-message receipt was confirmed."""
    conversation_id = submission.conversation_id
    if (provider_receipt_state(submission) != 'confirmed'
            or conversation_id is None
            or re.fullmatch(r'[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}',
                            conversation_id) is None):
        return None
    return f'https://chatgpt.com/c/{conversation_id}'


class SubchatQueueRevisionConflict(ValueError):
    """The queued selection changed while a caller was preparing it."""


def _saved_submission_json(submission: SubchatSubmission) -> str:
    """Keep new optional fields absent until they have evidence to persist."""
    excluded = {'reported_settings', 'provider_account_id', 'generation_http_status',
                'answer_type'}
    if submission.http_selection is None:
        excluded.add('http_selection')
    if submission.interruption_reason is None:
        excluded.add('interruption_reason')
    return submission.model_dump_json(exclude=excluded)


class SubchatList(Contract):
    limit: int = Field(default=20, ge=1, le=100)
    before: int | None = Field(default=None, ge=1)
    include_prompt_preview: bool = Field(default=False, strict=True)


class SubchatSummary(Contract):
    operation_id: str
    submission_operation_id: str
    state: str
    model: str
    effort: str
    conversation_id: str | None
    provider_receipt: Literal['not_sent', 'unconfirmed', 'confirmed']
    conversation_url: str | None
    created_at: float | None = None
    prompt_preview: str | None = None


class SubchatPage(Contract):
    submissions: list[SubchatSummary]
    next_before: int | None


class SubchatSubmissions:
    """Uses the ledger's connection; does not own or close it."""

    def __init__(self, connection: sqlite3.Connection, *, initialize: bool = True) -> None:
        self.connection = connection
        if not initialize:
            self._answer_types_available = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='subchat_answer_types'").fetchone() is not None
            self._preparation_failures_available = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='subchat_preparation_failures'").fetchone() is not None
            self._created_at_available = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='subchat_created_at'").fetchone() is not None
            self._auto_queue_available = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='subchat_auto_queue'").fetchone() is not None
            self._auto_queue_events_available = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='subchat_auto_queue_events'").fetchone() is not None
            self._auto_queue_epoch_available = (
                self._auto_queue_available and 'epoch' in {row[1] for row in
                connection.execute('PRAGMA table_info(subchat_auto_queue)')})
            self._auto_queue_notify_available = (
                self._auto_queue_available and 'notify_desktop' in {row[1] for row in
                connection.execute('PRAGMA table_info(subchat_auto_queue)')})
            return
        with connection:
            connection.execute('CREATE TABLE IF NOT EXISTS subchat_generation_responses ('
                               'operation_id TEXT PRIMARY KEY, owner TEXT, '
                               'user_message_id TEXT NOT NULL, account_id TEXT NOT NULL, '
                               'status INTEGER NOT NULL)')
            connection.execute('CREATE TABLE IF NOT EXISTS subchat_account_bindings ('
                               'operation_id TEXT PRIMARY KEY, owner TEXT, '
                               'user_message_id TEXT NOT NULL, account_id TEXT NOT NULL)')
            connection.execute('CREATE TABLE IF NOT EXISTS subchat_submissions ('
                               'operation_id TEXT PRIMARY KEY, owner TEXT, body TEXT NOT NULL)')
            # A side table keeps older submission JSON readable and gives legacy rows
            # an honest null timestamp rather than an invented migration time.
            connection.execute('CREATE TABLE IF NOT EXISTS subchat_created_at ('
                               'operation_id TEXT PRIMARY KEY, owner TEXT, '
                               'created_at REAL NOT NULL)')
            self._created_at_available = True
            connection.execute('CREATE TABLE IF NOT EXISTS subchat_auto_queue ('
                               'operation_id TEXT PRIMARY KEY, owner TEXT, '
                               'state TEXT NOT NULL, expires_at REAL NOT NULL, '
                               'event TEXT, updated_at REAL NOT NULL, '
                               'epoch INTEGER NOT NULL DEFAULT 1, '
                               'authorization_grant_id TEXT, '
                               'notify_desktop INTEGER NOT NULL DEFAULT 0)')
            auto_columns = {row[1] for row in connection.execute(
                'PRAGMA table_info(subchat_auto_queue)')}
            if 'epoch' not in auto_columns:
                connection.execute('ALTER TABLE subchat_auto_queue ADD COLUMN '
                                   'epoch INTEGER NOT NULL DEFAULT 1')
            if 'authorization_grant_id' not in auto_columns:
                connection.execute('ALTER TABLE subchat_auto_queue ADD COLUMN '
                                   'authorization_grant_id TEXT')
            if 'notify_desktop' not in auto_columns:
                connection.execute('ALTER TABLE subchat_auto_queue ADD COLUMN '
                                   'notify_desktop INTEGER NOT NULL DEFAULT 0')
            self._auto_queue_epoch_available = True
            self._auto_queue_notify_available = True
            events_exist = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='subchat_auto_queue_events'").fetchone() is not None
            connection.execute('CREATE TABLE IF NOT EXISTS subchat_auto_queue_events ('
                               'id INTEGER PRIMARY KEY, operation_id TEXT NOT NULL, '
                               'owner TEXT, event TEXT NOT NULL, updated_at REAL NOT NULL)')
            self._auto_queue_events_available = True
            if not events_exist:
                connection.execute(
                    'INSERT INTO subchat_auto_queue_events '
                    '(operation_id, owner, event, updated_at) '
                    'SELECT operation_id, owner, event, updated_at '
                    'FROM subchat_auto_queue WHERE event IS NOT NULL')
            self._auto_queue_available = True
            connection.execute('CREATE TABLE IF NOT EXISTS subchat_queue_revisions ('
                               'operation_id TEXT PRIMARY KEY, owner TEXT, '
                               'revision INTEGER NOT NULL)')
            connection.execute('CREATE TABLE IF NOT EXISTS subchat_mutation_receipts ('
                               'request_id TEXT PRIMARY KEY, owner TEXT, tool TEXT NOT NULL, '
                               'digest TEXT NOT NULL, result TEXT NOT NULL)')
            connection.execute('CREATE TABLE IF NOT EXISTS subchat_send_intents ('
                               'owner TEXT, intent_key TEXT NOT NULL, '
                               'operation_id TEXT NOT NULL UNIQUE, '
                               'PRIMARY KEY (owner, intent_key))')
            connection.execute('CREATE UNIQUE INDEX IF NOT EXISTS '
                               'subchat_send_intents_unowned ON '
                               'subchat_send_intents(intent_key) WHERE owner IS NULL')
            connection.execute('CREATE TABLE IF NOT EXISTS subchat_preparation_failures ('
                               'operation_id TEXT PRIMARY KEY, owner TEXT, '
                               'reason TEXT NOT NULL)')
            self._preparation_failures_available = True
            connection.execute('CREATE TABLE IF NOT EXISTS subchat_answer_types ('
                               'operation_id TEXT PRIMARY KEY, owner TEXT, '
                               'answer_message_id TEXT NOT NULL, answer_type TEXT NOT NULL)')
            self._answer_types_available = True
            # Older readers reject this new field, including its null value.
            # Preserve completed types in a separate table before removing it.
            for operation_id, owner, body in connection.execute(
                    'SELECT operation_id, owner, body FROM subchat_submissions'):
                saved_body = json.loads(body)
                if not isinstance(saved_body, dict) or 'answer_type' not in saved_body:
                    continue
                answer_type = saved_body.pop('answer_type')
                if answer_type is not None:
                    if (answer_type not in {'text', 'image', 'multimodal'}
                            or saved_body.get('state') != 'completed'
                            or not isinstance(saved_body.get('answer_message_id'), str)):
                        raise ValueError('Invalid saved Subchat answer type')
                    connection.execute(
                        'INSERT OR IGNORE INTO subchat_answer_types VALUES (?,?,?,?)',
                        (operation_id, owner, saved_body['answer_message_id'], answer_type))
                    existing = connection.execute(
                        'SELECT owner, answer_message_id, answer_type '
                        'FROM subchat_answer_types WHERE operation_id=?',
                        (operation_id,)).fetchone()
                    if existing != (owner, saved_body['answer_message_id'], answer_type):
                        raise ValueError('Conflicting saved Subchat answer type')
                connection.execute(
                    'UPDATE subchat_submissions SET body=? WHERE operation_id=? AND body=?',
                    (json.dumps(saved_body, ensure_ascii=False, separators=(',', ':')),
                     operation_id, body))
            connection.execute('CREATE TABLE IF NOT EXISTS subchat_answer_settings ('
                               'operation_id TEXT PRIMARY KEY, owner TEXT, '
                               'answer_message_id TEXT NOT NULL, body TEXT NOT NULL)')
            connection.execute('CREATE TABLE IF NOT EXISTS subchat_http_dispatch_claims ('
                               'operation_id TEXT PRIMARY KEY, owner TEXT, '
                               'user_message_id TEXT NOT NULL, account_id TEXT NOT NULL)')
            connection.execute('CREATE TABLE IF NOT EXISTS subchat_http_events ('
                               'id INTEGER PRIMARY KEY, operation_id TEXT NOT NULL, '
                               'stage TEXT NOT NULL, status INTEGER, timestamp REAL NOT NULL)')
            connection.execute('CREATE INDEX IF NOT EXISTS subchat_http_events_operation '
                               'ON subchat_http_events(operation_id, id)')

    _HTTP_STAGES = frozenset({
        'sentinel_request', 'sentinel_response', 'sentinel_failed',
        'prepare_request', 'prepare_response', 'prepare_failed',
        'branch_request', 'branch_response', 'branch_stale', 'branch_failed',
        'dispatch_claimed', 'generation_request', 'generation_response',
        'generation_failed', 'sse_candidate', 'history_receipt',
        'history_final', 'history_unknown', 'history_failed',
    })
    _HTTP_EVENT_LIMIT = 64

    def _insert_http_event(self, operation_id: str, stage: str, status: int | None) -> None:
        self.connection.execute(
            'INSERT INTO subchat_http_events(operation_id,stage,status,timestamp) '
            'VALUES (?,?,?,?)', (operation_id, stage, status, time.time()))
        self.connection.execute(
            'DELETE FROM subchat_http_events WHERE operation_id=? AND id NOT IN '
            '(SELECT id FROM subchat_http_events WHERE operation_id=? '
            'ORDER BY id DESC LIMIT ?)',
            (operation_id, operation_id, self._HTTP_EVENT_LIMIT))

    def record_http_event(self, operation_id: str, stage: str, *, owner: str | None,
                          status: int | None = None) -> None:
        """Persist only a fixed stage, numeric status, operation ID, and timestamp."""
        if stage not in self._HTTP_STAGES or (status is not None and (
                type(status) is not int or not 100 <= status <= 599)):
            raise ValueError('Invalid HTTP diagnostic event')
        with self.connection:
            self.connection.execute('BEGIN IMMEDIATE')
            self.get(operation_id, owner=owner)
            self._insert_http_event(operation_id, stage, status)

    def http_events(self, operation_id: str, *, owner: str | None,
                    limit: int = 64) -> list[dict[str, str | int | float | None]]:
        """Read bounded private diagnostics; never return request or response content."""
        if type(limit) is not int or not 1 <= limit <= self._HTTP_EVENT_LIMIT:
            raise ValueError('Invalid HTTP diagnostic limit')
        self.get(operation_id, owner=owner)
        rows = self.connection.execute(
            'SELECT stage,status,timestamp FROM subchat_http_events '
            'WHERE operation_id=? ORDER BY id DESC LIMIT ?', (operation_id, limit)).fetchall()
        return [{'operation_id': operation_id, 'stage': stage, 'status': status,
                 'timestamp': timestamp} for stage, status, timestamp in reversed(rows)]

    def http_progress(self, operation_id: str, *, owner: str | None
                      ) -> dict[str, JsonValue] | None:
        """Return the latest durable transport checkpoint for an owned submission."""
        self.get(operation_id, owner=owner)
        row = self.connection.execute(
            'SELECT stage,status,timestamp FROM subchat_http_events '
            'WHERE operation_id=? ORDER BY id DESC LIMIT 1', (operation_id,)).fetchone()
        if row is None:
            return None
        stage, status, timestamp = row
        if stage not in self._HTTP_STAGES:
            raise ValueError('Invalid saved HTTP diagnostic event')
        return {'stage': stage, 'status': status, 'timestamp': timestamp}

    def claim_http_dispatch(self, operation_id: str, *, owner: str | None,
                            user_message_id: str, provider_account_id: str,
                            prompt: str, model_slug: str, thinking_effort: str | None,
                            conversation_id: str | None, predecessor_id: str | None) -> bool:
        """Commit the one HTTP dispatch right before network I/O; never release it.

        A crash after this commit leaves the outcome unknown. Separate ledger
        connections serialize at BEGIN IMMEDIATE, so only one caller may POST.
        """
        with self.connection:
            self.connection.execute('BEGIN IMMEDIATE')
            saved = self.get(operation_id, owner=owner)
            if (saved.state != 'sending' or saved.http_selection is None
                    or saved.resources is not None
                    or saved.user_message_id != user_message_id
                    or saved.provider_account_id != provider_account_id
                    or saved.prompt != prompt
                    or saved.http_selection.model_slug != model_slug
                    or saved.http_selection.thinking_effort != thinking_effort
                    or saved.requested_conversation_id != conversation_id):
                raise ValueError('HTTP dispatch identity does not match the reserved submission')
            if saved.after_operation_id is None:
                if predecessor_id is not None:
                    raise ValueError('New Chat cannot have a predecessor')
            else:
                target = self.get(saved.after_operation_id, owner=owner)
                if (target.state != 'completed'
                        or target.conversation_id != conversation_id
                        or target.user_message_id != saved.expected_last_user_message_id
                        or target.answer_message_id != predecessor_id):
                    raise ValueError('HTTP predecessor does not match the completed target')
            cursor = self.connection.execute(
                'INSERT OR IGNORE INTO subchat_http_dispatch_claims VALUES (?,?,?,?)',
                (operation_id, owner, user_message_id, provider_account_id),
            )
            if cursor.rowcount == 1:
                self._insert_http_event(operation_id, 'dispatch_claimed', None)
            return cursor.rowcount == 1

    def fail_http_before_dispatch(self, operation_id: str, *, owner: str | None) -> bool:
        """Finish an HTTP send only when no generation dispatch was claimed.

        The claim and this transition use the same SQLite write lock. Never
        make a failed operation retryable: preparation may have remote effects.
        """
        with self.connection:
            self.connection.execute('BEGIN IMMEDIATE')
            saved = self.get(operation_id, owner=owner)
            if saved.http_selection is None or saved.state != 'sending':
                return False
            if self.connection.execute(
                    'SELECT 1 FROM subchat_http_dispatch_claims WHERE operation_id=?',
                    (operation_id,)).fetchone() is not None:
                return False
            updated = saved.model_copy(update={'state': 'preflight_failed'})
            self.connection.execute(
                'UPDATE subchat_submissions SET body=? WHERE operation_id=? AND owner IS ?',
                (_saved_submission_json(updated),
                 operation_id, owner),
            )
            return True

    def record_preparation_failure(
        self, operation_id: str, *, owner: str | None, reason: str,
    ) -> None:
        if not self._preparation_failures_available:
            return
        if re.fullmatch(r'[a-z_]{1,64}', reason) is None:
            raise ValueError('Invalid Subchat preparation failure reason')
        with self.connection:
            saved = self.get(operation_id, owner=owner)
            if saved.state not in {'prepared', 'queued'}:
                return
            self.connection.execute(
                'INSERT OR REPLACE INTO subchat_preparation_failures VALUES (?,?,?)',
                (operation_id, owner, reason),
            )

    def preparation_failure(
        self, operation_id: str, *, owner: str | None,
    ) -> str | None:
        self.get(operation_id, owner=owner)
        if not self._preparation_failures_available:
            return None
        row = self.connection.execute(
            'SELECT reason FROM subchat_preparation_failures '
            'WHERE operation_id=? AND owner IS ?', (operation_id, owner),
        ).fetchone()
        return row[0] if row is not None else None

    def clear_preparation_failure(self, operation_id: str, *, owner: str | None) -> None:
        self.get(operation_id, owner=owner)
        if self._preparation_failures_available:
            with self.connection:
                self.connection.execute(
                    'DELETE FROM subchat_preparation_failures '
                    'WHERE operation_id=? AND owner IS ?', (operation_id, owner),
                )

    def get(self, operation_id: str, *, owner: str | None) -> SubchatSubmission:
        row = self.connection.execute(
            'SELECT owner, body FROM subchat_submissions WHERE operation_id=?',
            (operation_id,),
        ).fetchone()
        if row is None or row[0] != owner:
            raise SubchatOperationNotFound('Unknown subchat submission')
        saved = SubchatSubmission.model_validate_json(row[1])
        if saved.state == 'completed' and self._answer_types_available:
            answer_type_row = self.connection.execute(
                'SELECT answer_type FROM subchat_answer_types WHERE operation_id=? '
                'AND owner IS ? AND answer_message_id=?',
                (operation_id, owner, saved.answer_message_id),
            ).fetchone()
            if answer_type_row is not None:
                saved = saved.model_copy(update={'answer_type': answer_type_row[0]})
        binding = self.connection.execute(
            'SELECT account_id FROM subchat_account_bindings WHERE operation_id=? '
            'AND owner IS ? AND user_message_id=?',
            (operation_id, owner, saved.user_message_id),
        ).fetchone()
        if binding is not None:
            saved = saved.model_copy(update={'provider_account_id': binding[0]})
        if saved.state == 'completed':
            evidence = self.connection.execute(
                'SELECT body FROM subchat_answer_settings WHERE operation_id=? '
                'AND owner IS ? AND answer_message_id=?',
                (operation_id, owner, saved.answer_message_id),
            ).fetchone()
            if evidence is not None:
                saved = saved.model_copy(update={
                    'reported_settings': SubchatReportedSettings.model_validate_json(evidence[0])})
        response = self.connection.execute(
            'SELECT status FROM subchat_generation_responses WHERE operation_id=? '
            'AND owner IS ? AND user_message_id=? AND account_id=?',
            (operation_id, owner, saved.user_message_id, saved.provider_account_id),
        ).fetchone()
        if response is not None:
            saved = saved.model_copy(update={'generation_http_status': response[0]})
        return saved

    def list(self, request: SubchatList, *, owner: str | None) -> SubchatPage:
        if self._created_at_available:
            rows = self.connection.execute(
                'SELECT s.rowid, s.body, c.created_at FROM subchat_submissions AS s '
                'LEFT JOIN subchat_created_at AS c ON c.operation_id=s.operation_id '
                'AND c.owner IS s.owner WHERE s.owner IS ? '
                'AND (? IS NULL OR s.rowid < ?) ORDER BY s.rowid DESC LIMIT ?',
                (owner, request.before, request.before, request.limit + 1),
            ).fetchall()
        else:
            rows = [(*row, None) for row in self.connection.execute(
                'SELECT rowid, body FROM subchat_submissions WHERE owner IS ? '
                'AND (? IS NULL OR rowid < ?) ORDER BY rowid DESC LIMIT ?',
                (owner, request.before, request.before, request.limit + 1),
            )]
        summaries = []
        for row in rows[:request.limit]:
            item = SubchatSubmission.model_validate_json(row[1])
            summary = item.model_dump(include={
                'operation_id', 'state', 'model', 'effort', 'conversation_id',
            })
            summary['submission_operation_id'] = item.operation_id
            summary['provider_receipt'] = provider_receipt_state(item)
            summary['conversation_url'] = confirmed_conversation_url(item)
            summary['created_at'] = row[2]
            if request.include_prompt_preview:
                summary['prompt_preview'] = item.prompt[:160]
            summaries.append(SubchatSummary.model_validate(summary))
        return SubchatPage(submissions=summaries, next_before=(
            rows[request.limit - 1][0] if len(rows) > request.limit else None))

    def prepare(self, operation_id: str, prompt: str, model: str, effort: str,
                *, owner: str | None, conversation_id: str | None = None,
                work_context: SubchatWorkContext | None = None,
                after_operation_id: str | None = None,
                resources: SubchatResources | None = None,
                http_selection: SubchatHTTPSelection | None = None,
                intent_key: str | None = None) -> SubchatSubmission:
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
            checkpointed_sending = (target.state == 'sending'
                                    and target.user_message_id is not None
                                    and target.provider_account_id is not None
                                    and target.conversation_id is not None)
            if ((target.state not in {'submitted', 'completed'}
                 and not checkpointed_sending)
                    or target.user_message_id is None):
                raise ValueError('Queue target identity must be checkpointed first')
            if conversation_id != target.conversation_id:
                raise ValueError('Queue target conversation does not match')
            if http_selection != target.http_selection:
                raise ValueError('Queue HTTP selection does not match its target')
            expected_last_user_message_id = target.user_message_id
        with self.connection:
            self.connection.execute('BEGIN IMMEDIATE')
            if intent_key is not None:
                if re.fullmatch(r'[0-9a-f]{32}', intent_key) is None:
                    raise ValueError('Invalid Subchat intent key')
                bound = self.connection.execute(
                    'SELECT operation_id FROM subchat_send_intents '
                    'WHERE owner IS ? AND intent_key=?', (owner, intent_key),
                ).fetchone()
                if bound is not None:
                    operation_id = bound[0]
                else:
                    existing_binding = self.connection.execute(
                        'SELECT intent_key FROM subchat_send_intents WHERE operation_id=?',
                        (operation_id,),
                    ).fetchone()
                    if existing_binding is not None or self.connection.execute(
                            'SELECT 1 FROM subchat_submissions WHERE operation_id=?',
                            (operation_id,)).fetchone() is not None:
                        raise SubchatRequestConflict(
                            'Subchat submission ID already belongs to another intent')
                    self.connection.execute(
                        'INSERT INTO subchat_send_intents VALUES (?,?,?)',
                        (owner, intent_key, operation_id),
                    )
            proposed = SubchatSubmission(operation_id=operation_id, prompt=prompt,
                                         model=model, effort=effort,
                                         requested_conversation_id=conversation_id,
                                         conversation_id=conversation_id,
                                         work_context=work_context,
                                         resources=resources, http_selection=http_selection,
                                         state='queued' if after_operation_id else 'prepared',
                                         after_operation_id=after_operation_id,
                                         expected_last_user_message_id=expected_last_user_message_id)
            inserted = self.connection.execute(
                'INSERT OR IGNORE INTO subchat_submissions VALUES (?,?,?)',
                (operation_id, owner, _saved_submission_json(proposed)),
            )
            if inserted.rowcount == 1:
                self.connection.execute(
                    'INSERT INTO subchat_created_at VALUES (?,?,?)',
                    (operation_id, owner, time.time()),
                )
            existing = self.get(operation_id, owner=owner)
            changed_queue = (existing.state == 'queued'
                             and after_operation_id is not None
                             and self.queue_revision(operation_id, owner=owner) > 0)
            changed_queue_selection = (changed_queue
                                       and (model, effort, http_selection) ==
                                       (target.model, target.effort, target.http_selection))
            if ((existing.prompt, existing.requested_conversation_id,
                 existing.work_context, existing.after_operation_id) != (
                     prompt, conversation_id, work_context, after_operation_id)
                    or (not changed_queue
                        and existing.resources != resources)
                    or (not changed_queue_selection and
                        (existing.model, existing.effort, existing.http_selection) !=
                        (model, effort, http_selection))):
                raise SubchatRequestConflict(
                    'Subchat submission ID was already used for different arguments')
            return existing

    def _replace(self, old: SubchatSubmission, new: SubchatSubmission,
                 owner: str | None, *, http_event: str | None = None,
                 require_auto_queue_armed: bool = False,
                 expected_queue_revision: int | None = None,
                 mutation_request_id: str | None = None,
                 mutation_digest: str | None = None) -> SubchatSubmission:
        with self.connection:
            # Serialize identity validation and mutation across ledger connections.
            self.connection.execute('BEGIN IMMEDIATE')
            if mutation_request_id is not None:
                if mutation_digest is None:
                    raise ValueError('Mutation digest is required')
                receipt = self.mutation_receipt(
                    mutation_request_id, owner=owner, tool='subchat_cancel',
                    digest=mutation_digest)
                if receipt is not None:
                    return SubchatSubmission.model_validate(receipt['submission'])
            if (expected_queue_revision is not None and
                    self.queue_revision(old.operation_id, owner=owner)
                    != expected_queue_revision):
                raise SubchatQueueRevisionConflict('Queued model changed during preparation')
            if require_auto_queue_armed:
                if (not self._auto_queue_available or old.state != 'queued'
                        or new.state != 'sending'):
                    raise SubchatAutoQueueDisarmed('Automatic queue is no longer armed')
                armed = self.connection.execute(
                    "SELECT 1 FROM subchat_auto_queue WHERE operation_id=? "
                    "AND owner IS ? AND state='armed' AND expires_at>?",
                    (old.operation_id, owner, time.time())).fetchone()
                if armed is None:
                    raise SubchatAutoQueueDisarmed('Automatic queue is no longer armed')
            if new.state == 'sending' and new.conversation_id is not None:
                deletion_table = self.connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' "
                    "AND name='subchat_http_deletions'").fetchone()
                if deletion_table is not None and self.connection.execute(
                        'SELECT 1 FROM subchat_http_deletions WHERE conversation_id=?',
                        (new.conversation_id,)).fetchone() is not None:
                    raise ValueError('Conversation has a pending or confirmed deletion')
                # Separate Codex and Claude MCP processes share this SQLite
                # ledger but not their in-memory browser locks. Serialize
                # distinct sends to one Chat before either may dispatch.
                peers = self.connection.execute(
                    'SELECT owner, body FROM subchat_submissions WHERE operation_id != ?',
                    (old.operation_id,),
                )
                for peer_row in peers:
                    peer = SubchatSubmission.model_validate_json(peer_row[1])
                    if (peer.conversation_id == new.conversation_id
                            and peer.state in {'sending', 'submitted'}):
                        raise SubchatConcurrentSend(
                            peer.operation_id if peer_row[0] == owner else None)
            if new.user_message_id is not None:
                peers = self.connection.execute(
                    'SELECT body FROM subchat_submissions WHERE owner IS ? AND operation_id != ?',
                    (owner, old.operation_id),
                )
                for peer_row in peers:
                    peer = SubchatSubmission.model_validate_json(peer_row[0])
                    if peer.conversation_id != new.conversation_id:
                        continue
                    if (peer.user_message_id == new.user_message_id or
                            (new.answer_message_id is not None and
                             peer.answer_message_id == new.answer_message_id)):
                        raise ValueError('Observed message is already bound to another submission')
            row = self.connection.execute(
                'SELECT body FROM subchat_submissions WHERE operation_id=? AND owner IS ?',
                (old.operation_id, owner),
            ).fetchone()
            if row is None or self.get(old.operation_id, owner=owner) != old:
                raise ValueError('Subchat submission changed; inspect it before continuing')
            cursor = self.connection.execute(
                'UPDATE subchat_submissions SET body=? '
                'WHERE operation_id=? AND owner IS ? AND body=?',
                (_saved_submission_json(new),
                 old.operation_id, owner, row[0]),
            )
            if cursor.rowcount != 1:
                raise ValueError('Subchat submission changed; inspect it before continuing')
            if new.provider_account_id != old.provider_account_id:
                if old.provider_account_id is not None or new.user_message_id is None:
                    raise ValueError('Provider account binding cannot be replaced')
                self.connection.execute(
                    'INSERT INTO subchat_account_bindings VALUES (?,?,?,?)',
                    (new.operation_id, owner, new.user_message_id, new.provider_account_id),
                )
            if new.reported_settings is not None:
                # Keep the legacy submission JSON readable by older runtimes.
                # Evidence commits atomically with its bound completed answer.
                self.connection.execute(
                    'INSERT INTO subchat_answer_settings VALUES (?,?,?,?)',
                    (new.operation_id, owner, new.answer_message_id,
                     new.reported_settings.model_dump_json()),
                )
            if new.state == 'completed' and new.answer_type is not None:
                self.connection.execute(
                    'INSERT INTO subchat_answer_types VALUES (?,?,?,?)',
                    (new.operation_id, owner, new.answer_message_id, new.answer_type),
                )
            if (http_event is not None and self.connection.execute(
                    'SELECT 1 FROM subchat_http_dispatch_claims WHERE operation_id=?',
                    (new.operation_id,)).fetchone() is not None):
                self._insert_http_event(new.operation_id, http_event, None)
            self._save_mutation_receipt(
                mutation_request_id, owner=owner, tool='subchat_cancel',
                digest=mutation_digest, result={'submission': new.model_dump(mode='json')})
        return new

    def cancel(self, operation_id: str, *, owner: str | None,
               request_id: str | None = None, digest: str | None = None) -> SubchatSubmission:
        """Cancel only a not-yet-reserved submission, never stop a provider turn."""
        if request_id is not None:
            if digest is None:
                raise ValueError('Mutation digest is required')
            receipt = self.mutation_receipt(
                request_id, owner=owner, tool='subchat_cancel', digest=digest)
            if receipt is not None:
                return SubchatSubmission.model_validate(receipt['submission'])
        old = self.get(operation_id, owner=owner)
        if old.state == 'cancelled':
            if request_id is not None:
                assert digest is not None
                with self.connection:
                    self.connection.execute('BEGIN IMMEDIATE')
                    receipt = self.mutation_receipt(
                        request_id, owner=owner, tool='subchat_cancel', digest=digest)
                    if receipt is not None:
                        return SubchatSubmission.model_validate(receipt['submission'])
                    self._save_mutation_receipt(
                        request_id, owner=owner, tool='subchat_cancel', digest=digest,
                        result={'submission': old.model_dump(mode='json')})
            return old
        if old.state not in {'queued', 'prepared'}:
            raise ValueError('Submission may already be dispatched; cancellation is unavailable')
        return self._replace(
            old, old.model_copy(update={'state': 'cancelled'}), owner,
            mutation_request_id=request_id, mutation_digest=digest)

    def queue_revision(self, operation_id: str, *, owner: str | None) -> int:
        self.get(operation_id, owner=owner)
        row = self.connection.execute(
            'SELECT revision FROM subchat_queue_revisions WHERE operation_id=? AND owner IS ?',
            (operation_id, owner)).fetchone()
        return row[0] if row is not None else 0

    def mutation_receipt(self, request_id: str, *, owner: str | None,
                         tool: str, digest: str) -> dict[str, JsonValue] | None:
        """Return an exact committed mutation result, or reject a reused request ID."""
        row = self.connection.execute(
            'SELECT owner, tool, digest, result FROM subchat_mutation_receipts '
            'WHERE request_id=?', (request_id,)).fetchone()
        if row is None:
            return None
        if (row[0], row[1], row[2]) != (owner, tool, digest):
            raise SubchatCommittedMutationConflict('Subchat mutation ID was already used')
        result = json.loads(row[3])
        if not isinstance(result, dict):
            raise ValueError('Saved Subchat mutation result is invalid')
        return result

    def _save_mutation_receipt(self, request_id: str | None, *, owner: str | None,
                               tool: str, digest: str | None,
                               result: dict[str, JsonValue]) -> None:
        if request_id is None:
            return
        if digest is None:
            raise ValueError('Mutation digest is required with a request ID')
        self.connection.execute(
            'INSERT INTO subchat_mutation_receipts '
            '(request_id, owner, tool, digest, result) VALUES (?,?,?,?,?)',
            (request_id, owner, tool, digest,
             json.dumps(result, sort_keys=True, separators=(',', ':'))))

    def change_queued_model(self, operation_id: str, *, owner: str | None,
                            expected_revision: int, model: str, effort: str,
                            http_selection: SubchatHTTPSelection | None,
                            request_id: str | None = None, digest: str | None = None
                            ) -> tuple[SubchatSubmission, int]:
        """CAS a queued selection before the durable send reservation."""
        with self.connection:
            self.connection.execute('BEGIN IMMEDIATE')
            if request_id is not None:
                if digest is None:
                    raise ValueError('Mutation digest is required')
                receipt = self.mutation_receipt(
                    request_id, owner=owner, tool='subchat_queue_model_change', digest=digest)
                if receipt is not None:
                    saved_revision = receipt['queue_revision']
                    if type(saved_revision) is not int:
                        raise ValueError('Saved queue revision is invalid')
                    return (SubchatSubmission.model_validate(receipt['submission']),
                            saved_revision)
            old = self.get(operation_id, owner=owner)
            if old.state != 'queued':
                raise ValueError('Only a queued input can change its model')
            revision = self.queue_revision(operation_id, owner=owner)
            if revision != expected_revision:
                raise SubchatQueueRevisionConflict('Queued model revision changed')
            if old.after_operation_id is None:
                raise ValueError('Queued input has no parent')
            parent = self.get(old.after_operation_id, owner=owner)
            if (old.conversation_id != parent.conversation_id
                    or old.expected_last_user_message_id != parent.user_message_id):
                raise ValueError('Queued parent identity changed')
            updated = old.model_copy(update={
                'model': model, 'effort': effort, 'http_selection': http_selection})
            # Keep old JSON readers compatible and make the row and revision atomic.
            row = self.connection.execute(
                'UPDATE subchat_submissions SET body=? WHERE operation_id=? AND owner IS ? '
                'AND body=?',
                (_saved_submission_json(updated), operation_id, owner,
                 _saved_submission_json(old)))
            if row.rowcount != 1:
                raise SubchatQueueRevisionConflict('Queued input changed')
            self.connection.execute(
                'INSERT INTO subchat_queue_revisions VALUES (?,?,?) '
                'ON CONFLICT(operation_id) DO UPDATE SET revision=excluded.revision',
                (operation_id, owner, revision + 1))
            self._save_mutation_receipt(
                request_id, owner=owner, tool='subchat_queue_model_change', digest=digest,
                result={'submission': updated.model_dump(mode='json'),
                        'queue_revision': revision + 1})
            return updated, revision + 1

    def change_queued_resources(self, operation_id: str, *, owner: str | None,
                                expected_revision: int, resources: SubchatResources,
                                request_id: str | None = None, digest: str | None = None
                                ) -> tuple[SubchatSubmission, int]:
        """Replace explicit resource references only before a queued send is reserved."""
        with self.connection:
            self.connection.execute('BEGIN IMMEDIATE')
            if request_id is not None:
                if digest is None:
                    raise ValueError('Mutation digest is required')
                receipt = self.mutation_receipt(
                    request_id, owner=owner, tool='subchat_queue_resources_change',
                    digest=digest)
                if receipt is not None:
                    saved_revision = receipt['queue_revision']
                    if type(saved_revision) is not int:
                        raise ValueError('Saved queue revision is invalid')
                    return (SubchatSubmission.model_validate(receipt['submission']),
                            saved_revision)
            old = self.get(operation_id, owner=owner)
            if old.state != 'queued':
                raise ValueError('Only a queued input can change its resources')
            revision = self.queue_revision(operation_id, owner=owner)
            if revision != expected_revision:
                raise SubchatQueueRevisionConflict('Queued resources revision changed')
            if old.after_operation_id is None:
                raise ValueError('Queued input has no parent')
            parent = self.get(old.after_operation_id, owner=owner)
            if (old.conversation_id != parent.conversation_id
                    or old.expected_last_user_message_id != parent.user_message_id):
                raise ValueError('Queued parent identity changed')
            updated = old.model_copy(update={
                'resources': resources if resources.attachments or resources.plugins else None})
            row = self.connection.execute(
                'UPDATE subchat_submissions SET body=? WHERE operation_id=? AND owner IS ? '
                'AND body=?',
                (_saved_submission_json(updated), operation_id, owner,
                 _saved_submission_json(old)))
            if row.rowcount != 1:
                raise SubchatQueueRevisionConflict('Queued input changed')
            self.connection.execute(
                'INSERT INTO subchat_queue_revisions VALUES (?,?,?) '
                'ON CONFLICT(operation_id) DO UPDATE SET revision=excluded.revision',
                (operation_id, owner, revision + 1))
            self._save_mutation_receipt(
                request_id, owner=owner, tool='subchat_queue_resources_change', digest=digest,
                result={'submission': updated.model_dump(mode='json'),
                        'queue_revision': revision + 1})
            return updated, revision + 1

    def interrupt(self, operation_id: str, *, owner: str | None,
                  reason: Literal['provider_interrupted', 'output_limit'] =
                  'provider_interrupted') -> SubchatSubmission:
        """Persist an explicitly observed provider interruption, not a timeout."""
        if reason not in {'provider_interrupted', 'output_limit'}:
            raise ValueError('Invalid interruption reason')
        old = self.get(operation_id, owner=owner)
        if old.state == 'interrupted':
            return old
        if old.state != 'submitted':
            raise ValueError('Only a confirmed submission can be marked interrupted')
        return self._replace(old, old.model_copy(update={
            'state': 'interrupted', 'interruption_reason': reason}), owner)

    def begin_send(self, operation_id: str, *, owner: str | None,
                   conversation_id: str | None = None,
                   baseline_message_ids: tuple[str, ...] = (),
                   baseline_identity_kind: Literal[
                       'legacy_turn_key', 'message_id', 'empty'] | None = None,
                   user_message_id: str | None = None,
                   provider_account_id: str | None = None,
                   require_auto_queue_armed: bool = False,
                   expected_queue_revision: int | None = None) -> SubchatSubmission:
        old = self.get(operation_id, owner=owner)
        if (expected_queue_revision is not None and
                self.queue_revision(operation_id, owner=owner) != expected_queue_revision):
            raise SubchatQueueRevisionConflict('Queued model changed during preparation')
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
        if baseline_identity_kind not in (None, 'legacy_turn_key', 'message_id', 'empty'):
            raise ValueError('Invalid baseline identity kind')
        if baseline_identity_kind == 'empty' and baseline_message_ids:
            raise ValueError('Empty baseline identity kind requires empty history')
        if (user_message_id is None) != (provider_account_id is None):
            raise ValueError('Outgoing identity and account must be reserved together')
        if user_message_id is not None:
            if (not user_message_id.strip() or len(user_message_id) > 256
                    or user_message_id in baseline_message_ids):
                raise ValueError('Invalid outgoing submission identity')
            assert provider_account_id is not None
            if not provider_account_id.strip() or len(provider_account_id) > 256:
                raise ValueError('Invalid provider account identity')
            if old.after_operation_id is not None:
                target = self.get(old.after_operation_id, owner=owner)
                if (target.provider_account_id is not None
                        and target.provider_account_id != provider_account_id):
                    raise SubchatAccountMismatch(
                        'Queued request belongs to a different Chat account')
        return self._replace(old, old.model_copy(update={
            'state': 'sending', 'conversation_id': old.conversation_id or conversation_id,
            'baseline_message_ids': baseline_message_ids,
            'baseline_identity_kind': baseline_identity_kind,
            'user_message_id': user_message_id,
            'provider_account_id': provider_account_id}), owner,
            require_auto_queue_armed=require_auto_queue_armed,
            expected_queue_revision=expected_queue_revision)

    def observe_request(self, operation_id: str, user_message_id: str,
                        *, owner: str | None, provider_account_id: str | None = None
                        ) -> SubchatSubmission:
        """Checkpoint outgoing identity before transport; not server acceptance."""
        old = self.get(operation_id, owner=owner)
        if (old.state != 'sending' or not user_message_id.strip()
                or len(user_message_id) > 256
                or user_message_id in old.baseline_message_ids):
            raise ValueError('Invalid outgoing submission identity')
        if old.after_operation_id is not None:
            target = self.get(old.after_operation_id, owner=owner)
            if (target.provider_account_id is not None
                    and target.provider_account_id != provider_account_id):
                raise SubchatAccountMismatch('Queued request belongs to a different Chat account')
        if provider_account_id is not None and (not provider_account_id.strip()
                or len(provider_account_id) > 256):
            raise ValueError('Invalid provider account identity')
        if old.user_message_id is not None:
            if (old.user_message_id != user_message_id
                    or old.provider_account_id != provider_account_id):
                raise ValueError('Outgoing submission identity changed')
            return old
        return self._replace(
            old, old.model_copy(update={'user_message_id': user_message_id,
                                        'provider_account_id': provider_account_id}), owner)

    def observe_rejection(self, operation_id: str, user_message_id: str, status: int,
                          *, owner: str | None, provider_account_id: str) -> None:
        """Save bound HTTP evidence without declaring effects absent or permitting replay."""
        if type(status) is not int or not 400 <= status <= 599:
            raise ValueError('Invalid generation rejection status')
        with self.connection:
            self.connection.execute('BEGIN IMMEDIATE')
            saved = self.get(operation_id, owner=owner)
            if (saved.state != 'sending' or saved.user_message_id != user_message_id
                    or not provider_account_id
                    or saved.provider_account_id != provider_account_id):
                raise ValueError('Generation response identity does not match submission')
            if saved.generation_http_status is not None:
                if saved.generation_http_status != status:
                    raise ValueError('Generation response evidence changed')
                return
            self.connection.execute(
                'INSERT INTO subchat_generation_responses VALUES (?,?,?,?,?)',
                (operation_id, owner, user_message_id, provider_account_id, status))

    def observe_conversation(self, operation_id: str, user_message_id: str,
                             conversation_id: str, *, owner: str | None,
                             provider_account_id: str) -> SubchatSubmission:
        """Save a transport candidate; HTTP history must still verify acceptance."""
        old = self.get(operation_id, owner=owner)
        if (old.state != 'sending' or old.user_message_id != user_message_id
                or old.provider_account_id is None
                or old.provider_account_id != provider_account_id):
            raise ValueError('Conversation candidate does not match the outgoing request')
        if (not conversation_id.strip() or len(conversation_id) > 256
                or old.conversation_id not in {None, conversation_id}):
            raise ValueError('Conversation candidate changed')
        if old.conversation_id == conversation_id:
            return old
        return self._replace(
            old, old.model_copy(update={'conversation_id': conversation_id}), owner)

    def submitted(self, operation_id: str, conversation_id: str, user_message_id: str,
                  *, owner: str | None) -> SubchatSubmission:
        old = self.get(operation_id, owner=owner)
        if not conversation_id.strip() or not user_message_id.strip():
            raise ValueError('Observed conversation and user identities are required')
        if old.conversation_id is not None and old.conversation_id != conversation_id:
            raise ValueError('Observed conversation does not match the submission')
        if user_message_id in old.baseline_message_ids:
            raise ValueError('Observed message predates this submission')
        if old.user_message_id is not None and old.user_message_id != user_message_id:
            raise ValueError('Observed message does not match the submission')
        if old.state in {'submitted', 'completed'}:
            if old.user_message_id != user_message_id:
                raise ValueError('Observed message does not match the submission')
            return old
        if old.state != 'sending':
            raise ValueError('Submission has not entered the send stage')
        return self._replace(old, old.model_copy(update={
            'state': 'submitted', 'conversation_id': conversation_id,
            'user_message_id': user_message_id}), owner)

    def complete(self, operation_id: str, answer_message_id: str, answer: str | None,
                 *, owner: str | None,
                 answer_type: Literal['text', 'image', 'multimodal'] = 'text',
                 reported_settings: SubchatReportedSettings | None = None) -> SubchatSubmission:
        old = self.get(operation_id, owner=owner)
        if (not answer_message_id.strip() or answer_message_id == old.user_message_id
                or answer_type == 'image' and answer is not None
                or answer_type != 'image' and not answer):
            raise ValueError('A distinct observed answer identity and content are required')
        if old.state == 'completed':
            if (old.answer_message_id, old.answer, old.answer_type or 'text',
                    old.reported_settings) != (
                    answer_message_id, answer, answer_type, reported_settings):
                raise ValueError('Completed subchat answer cannot be replaced')
            return old
        if old.state != 'submitted':
            raise ValueError('Submission identity must be observed before its answer')
        return self._replace(old, old.model_copy(update={
            'state': 'completed', 'answer_message_id': answer_message_id, 'answer': answer,
            'answer_type': answer_type,
            'reported_settings': reported_settings}), owner, http_event='history_final')

    def has_queued_for_conversation(self, conversation_id: str, *, owner: str | None) -> bool:
        """Keep an owned browser page only while a saved child awaits dispatch."""
        for (body,) in self.connection.execute(
            'SELECT body FROM subchat_submissions WHERE owner IS ?', (owner,)
        ):
            submission = SubchatSubmission.model_validate_json(body)
            if submission.state == 'queued' and submission.conversation_id == conversation_id:
                return True
        return False

    def arm_auto_queue(self, operation_id: str, *, owner: str | None,
                       lease_seconds: int, authorization_grant_id: str | None = None,
                       notify_desktop: bool = False, request_id: str | None = None,
                       digest: str | None = None
                       ) -> dict[str, JsonValue]:
        """Opt in a saved queue; keep only bounded delivery metadata, never prompt text."""
        if not self._auto_queue_available:
            raise ValueError('Automatic queue storage is unavailable')
        if type(lease_seconds) is not int or not 30 <= lease_seconds <= 86_400:
            raise ValueError('Invalid automatic queue lease')
        if type(notify_desktop) is not bool:
            raise ValueError('Invalid desktop notification preference')
        with self.connection:
            self.connection.execute('BEGIN IMMEDIATE')
            if request_id is not None:
                if digest is None:
                    raise ValueError('Mutation digest is required')
                receipt = self.mutation_receipt(
                    request_id, owner=owner, tool='subchat_queue_auto', digest=digest)
                if receipt is not None:
                    return receipt
            saved = self.get(operation_id, owner=owner)
            if saved.state != 'queued':
                raise ValueError('Automatic delivery requires a queued input')
            active = self.connection.execute(
                "SELECT COUNT(*) FROM subchat_auto_queue WHERE owner IS ? "
                "AND state='armed' AND operation_id<>?", (owner, operation_id)).fetchone()
            if active is not None and active[0] >= 8:
                raise ValueError('Automatic queue limit reached')
            now = time.time()
            expires_at = now + lease_seconds
            event_cursor = self.connection.execute(
                'SELECT COALESCE(MAX(id),0) FROM subchat_auto_queue_events '
                'WHERE owner IS ?', (owner,)
            ).fetchone()[0]
            self.connection.execute(
                'INSERT INTO subchat_auto_queue '
                '(operation_id, owner, state, expires_at, event, updated_at, epoch, '
                'authorization_grant_id, notify_desktop) VALUES (?,?,?,?,?,?,1,?,?) '
                'ON CONFLICT(operation_id) DO UPDATE SET state=excluded.state, '
                'expires_at=excluded.expires_at, event=NULL, updated_at=excluded.updated_at, '
                'epoch=subchat_auto_queue.epoch+1, '
                'authorization_grant_id=excluded.authorization_grant_id, '
                'notify_desktop=excluded.notify_desktop '
                'WHERE subchat_auto_queue.owner IS excluded.owner',
                (operation_id, owner, 'armed', expires_at, None, now,
                 authorization_grant_id, int(notify_desktop)))
            row = self.connection.execute(
                'SELECT owner, state, expires_at, epoch, notify_desktop FROM subchat_auto_queue '
                'WHERE operation_id=?', (operation_id,)).fetchone()
            if row is None or row[0] != owner:
                raise SubchatOperationNotFound('No queue visible to this owner')
            result: dict[str, JsonValue] = {'state': row[1], 'expires_at': row[2],
                                            'event_cursor': event_cursor, 'epoch': row[3],
                                            'notify_desktop': bool(row[4])}
            self._save_mutation_receipt(
                request_id, owner=owner, tool='subchat_queue_auto', digest=digest,
                result=result)
            return result

    def auto_queue_grant_id(self, operation_id: str, *, owner: str | None) -> str | None:
        self.get(operation_id, owner=owner)
        row = self.connection.execute(
            'SELECT authorization_grant_id FROM subchat_auto_queue '
            'WHERE operation_id=? AND owner IS ?', (operation_id, owner)).fetchone()
        return str(row[0]) if row is not None and isinstance(row[0], str) else None

    def disable_auto_queue(self, operation_id: str, *, owner: str | None,
                           request_id: str | None = None, digest: str | None = None) -> bool:
        if not self._auto_queue_available:
            return False
        with self.connection:
            self.connection.execute('BEGIN IMMEDIATE')
            if request_id is not None:
                if digest is None:
                    raise ValueError('Mutation digest is required')
                receipt = self.mutation_receipt(
                    request_id, owner=owner, tool='subchat_queue_auto', digest=digest)
                if receipt is not None:
                    return bool(receipt['changed'])
            self.get(operation_id, owner=owner)
            now = time.time()
            changed = self.connection.execute(
                "UPDATE subchat_auto_queue SET state='disabled', event='disabled', "
                'updated_at=? WHERE operation_id=? AND owner IS ? AND state=?',
                (now, operation_id, owner, 'armed'))
            if changed.rowcount == 1:
                self.connection.execute(
                    'INSERT INTO subchat_auto_queue_events '
                    '(operation_id, owner, event, updated_at) VALUES (?,?,?,?)',
                    (operation_id, owner, 'disabled', now))
            result = changed.rowcount == 1
            status = self.auto_queue_status(operation_id, owner=owner)
            self._save_mutation_receipt(
                request_id, owner=owner, tool='subchat_queue_auto', digest=digest,
                result={'changed': result, 'auto_status': status or {'state': 'disabled'}})
            return result

    def active_auto_queues(self, *, owner: str | None) -> tuple[str, ...]:
        if not self._auto_queue_available:
            return ()
        return tuple(row[0] for row in self.connection.execute(
            "SELECT operation_id FROM subchat_auto_queue WHERE owner IS ? AND state='armed' "
            'ORDER BY updated_at, operation_id', (owner,)))

    def auto_queue_status(self, operation_id: str, *, owner: str | None
                          ) -> dict[str, JsonValue] | None:
        self.get(operation_id, owner=owner)
        if not self._auto_queue_available:
            return None
        epoch_column = 'epoch' if self._auto_queue_epoch_available else '1'
        notify_column = 'notify_desktop' if self._auto_queue_notify_available else '0'
        row = self.connection.execute(
            f'SELECT state, expires_at, event, updated_at, {epoch_column}, {notify_column} '
            'FROM subchat_auto_queue '
            'WHERE operation_id=? AND owner IS ?', (operation_id, owner)).fetchone()
        return ({'state': row[0], 'expires_at': row[1], 'event': row[2],
                 'updated_at': row[3], 'epoch': row[4],
                 'notify_desktop': bool(row[5])} if row is not None else None)

    def finish_auto_queue(self, operation_id: str, *, owner: str | None,
                          event: str, expected_epoch: int | None = None) -> int | None:
        if event not in {'completed', 'cancelled', 'interrupted', 'preflight_failed',
                         'lease_expired', 'authorization_lost', 'observation_failed',
                         'preparation_failed', 'predecessor_interrupted'}:
            raise ValueError('Invalid automatic queue event')
        if not self._auto_queue_available:
            return None
        if expected_epoch is not None and (type(expected_epoch) is not int
                                           or expected_epoch < 1):
            raise ValueError('Invalid automatic queue epoch')
        with self.connection:
            now = time.time()
            changed = self.connection.execute(
                "UPDATE subchat_auto_queue SET state='stopped', event=?, updated_at=? "
                "WHERE operation_id=? AND owner IS ? AND state='armed' "
                "AND (? IS NULL OR epoch=?)",
                (event, now, operation_id, owner, expected_epoch, expected_epoch))
            if changed.rowcount == 1:
                saved = self.connection.execute(
                    'INSERT INTO subchat_auto_queue_events '
                    '(operation_id, owner, event, updated_at) VALUES (?,?,?,?)',
                    (operation_id, owner, event, now))
                if saved.lastrowid is None:
                    raise ValueError('Automatic queue event has no durable identity')
                return saved.lastrowid
            return None

    def auto_queue_events(self, *, owner: str | None, limit: int = 50,
                          operation_id: str | None = None
                          ) -> builtins.list[dict[str, JsonValue]]:
        if not 1 <= limit <= 100:
            raise ValueError('Invalid event limit')
        if not self._auto_queue_available:
            return []
        if not self._auto_queue_events_available:
            # A read-only controller may inspect a pre-migration ledger.
            return [
                {'operation_id': operation_id, 'event': event,
                 'updated_at': updated_at}
                for operation_id, event, updated_at in self.connection.execute(
                    'SELECT operation_id, event, updated_at FROM subchat_auto_queue '
                    'WHERE owner IS ? AND event IS NOT NULL '
                    'AND (? IS NULL OR operation_id=?) '
                    'ORDER BY updated_at DESC LIMIT ?',
                    (owner, operation_id, operation_id, limit))
            ]
        return [
            {'operation_id': operation_id, 'event': event, 'updated_at': updated_at}
            for operation_id, event, updated_at in self.connection.execute(
                'SELECT operation_id, event, updated_at FROM subchat_auto_queue_events '
                'WHERE owner IS ? AND (? IS NULL OR operation_id=?) '
                'ORDER BY id DESC LIMIT ?',
                (owner, operation_id, operation_id, limit))
        ]

    def auto_queue_events_page(self, *, owner: str | None, after_id: int,
                               limit: int = 50, operation_id: str | None = None
                               ) -> tuple[builtins.list[dict[str, JsonValue]], bool]:
        """Read a stable forward cursor so missed notices cannot age out of a recent list."""
        if type(after_id) is not int or after_id < 0 or not 1 <= limit <= 100:
            raise ValueError('Invalid queue event cursor or limit')
        if not self._auto_queue_available:
            return [], False
        if not self._auto_queue_events_available:
            raise ValueError('Queue event cursors require migrated writable state')
        rows = self.connection.execute(
            'SELECT id, operation_id, event, updated_at FROM subchat_auto_queue_events '
            'WHERE owner IS ? AND id>? AND (? IS NULL OR operation_id=?) '
            'ORDER BY id ASC LIMIT ?',
            (owner, after_id, operation_id, operation_id, limit + 1)).fetchall()
        return ([{'id': row[0], 'operation_id': row[1], 'event': row[2],
                  'updated_at': row[3]} for row in rows[:limit]], len(rows) > limit)
