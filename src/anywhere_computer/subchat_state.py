"""Intermediate submission receipts in the existing operation database.

A persisted 'sending' record is intentionally not reset on restart. Its caller
may inspect the conversation, but may not dispatch the same submission again.
"""

import sqlite3
import time
from typing import Literal

from pydantic import Field

from .models import Contract
from .subchat_content import SubchatResources


class SubchatAccountMismatch(ValueError):
    """The observed account does not match a saved operation or active reader."""

    code = 'account_mismatch'


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
        'queued', 'prepared', 'sending', 'submitted', 'completed', 'cancelled', 'interrupted'
    ] = 'prepared'
    after_operation_id: str | None = Field(default=None, pattern=r'^[0-9a-f]{32}$')
    expected_last_user_message_id: str | None = None
    requested_conversation_id: str | None = None
    conversation_id: str | None = None
    baseline_message_ids: tuple[str, ...] = ()
    user_message_id: str | None = None
    answer_message_id: str | None = None
    answer: str | None = None
    generation_http_status: int | None = Field(default=None, ge=400, le=599)
    reported_settings: SubchatReportedSettings | None = None
    provider_account_id: str | None = Field(default=None, min_length=1, max_length=256)
    work_context: SubchatWorkContext | None = None
    resources: SubchatResources | None = None
    http_selection: SubchatHTTPSelection | None = None

    @property
    def wire_prompt(self) -> str:
        return self.resources.prompt(self.prompt) if self.resources else self.prompt


class SubchatList(Contract):
    limit: int = Field(default=20, ge=1, le=100)
    before: int | None = Field(default=None, ge=1)


class SubchatSummary(Contract):
    operation_id: str
    state: str
    model: str
    effort: str
    conversation_id: str | None


class SubchatPage(Contract):
    submissions: list[SubchatSummary]
    next_before: int | None


class SubchatSubmissions:
    """Uses the ledger's connection; does not own or close it."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
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

    def get(self, operation_id: str, *, owner: str | None) -> SubchatSubmission:
        row = self.connection.execute(
            'SELECT owner, body FROM subchat_submissions WHERE operation_id=?',
            (operation_id,),
        ).fetchone()
        if row is None or row[0] != owner:
            raise ValueError('Unknown subchat submission')
        saved = SubchatSubmission.model_validate_json(row[1])
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
        rows = self.connection.execute(
            'SELECT rowid, body FROM subchat_submissions WHERE owner IS ? '
            'AND (? IS NULL OR rowid < ?) ORDER BY rowid DESC LIMIT ?',
            (owner, request.before, request.before, request.limit + 1),
        ).fetchall()
        summaries = []
        for row in rows[:request.limit]:
            item = SubchatSubmission.model_validate_json(row[1])
            summaries.append(SubchatSummary.model_validate(item.model_dump(include={
                'operation_id', 'state', 'model', 'effort', 'conversation_id',
            })))
        return SubchatPage(submissions=summaries, next_before=(
            rows[request.limit - 1][0] if len(rows) > request.limit else None))

    def prepare(self, operation_id: str, prompt: str, model: str, effort: str,
                *, owner: str | None, conversation_id: str | None = None,
                work_context: SubchatWorkContext | None = None,
                after_operation_id: str | None = None,
                resources: SubchatResources | None = None,
                http_selection: SubchatHTTPSelection | None = None) -> SubchatSubmission:
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
            if http_selection != target.http_selection:
                raise ValueError('Queue HTTP selection does not match its target')
            expected_last_user_message_id = target.user_message_id
        proposed = SubchatSubmission(operation_id=operation_id, prompt=prompt,
                                     model=model, effort=effort,
                                     requested_conversation_id=conversation_id,
                                     conversation_id=conversation_id, work_context=work_context,
                                     resources=resources, http_selection=http_selection,
                                     state='queued' if after_operation_id else 'prepared',
                                     after_operation_id=after_operation_id,
                                     expected_last_user_message_id=expected_last_user_message_id)
        with self.connection:
            self.connection.execute(
                'INSERT OR IGNORE INTO subchat_submissions VALUES (?,?,?)',
                (operation_id, owner, proposed.model_dump_json(
                    exclude={'reported_settings', 'provider_account_id', 'generation_http_status'} |
                    ({'http_selection'} if proposed.http_selection is None else set()))),
            )
        existing = self.get(operation_id, owner=owner)
        if (existing.prompt, existing.model, existing.effort,
            existing.requested_conversation_id, existing.work_context,
            existing.after_operation_id, existing.resources, existing.http_selection) != (
                prompt, model, effort, conversation_id, work_context, after_operation_id,
                resources, http_selection):
            raise ValueError('Subchat submission ID was already used for different arguments')
        return existing

    def _replace(self, old: SubchatSubmission, new: SubchatSubmission,
                 owner: str | None, *, http_event: str | None = None) -> SubchatSubmission:
        with self.connection:
            # Serialize identity validation and mutation across ledger connections.
            self.connection.execute('BEGIN IMMEDIATE')
            if new.state == 'sending' and new.conversation_id is not None:
                deletion_table = self.connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' "
                    "AND name='subchat_http_deletions'").fetchone()
                if deletion_table is not None and self.connection.execute(
                        'SELECT 1 FROM subchat_http_deletions WHERE conversation_id=?',
                        (new.conversation_id,)).fetchone() is not None:
                    raise ValueError('Conversation has a pending or confirmed deletion')
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
                (new.model_dump_json(exclude={'reported_settings', 'provider_account_id',
                                              'generation_http_status'} |
                                     ({'http_selection'} if new.http_selection is None else set())),
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
            if (http_event is not None and self.connection.execute(
                    'SELECT 1 FROM subchat_http_dispatch_claims WHERE operation_id=?',
                    (new.operation_id,)).fetchone() is not None):
                self._insert_http_event(new.operation_id, http_event, None)
        return new

    def cancel(self, operation_id: str, *, owner: str | None) -> SubchatSubmission:
        """Cancel only a not-yet-reserved submission, never stop a provider turn."""
        old = self.get(operation_id, owner=owner)
        if old.state == 'cancelled':
            return old
        if old.state not in {'queued', 'prepared'}:
            raise ValueError('Submission may already be dispatched; cancellation is unavailable')
        return self._replace(old, old.model_copy(update={'state': 'cancelled'}), owner)

    def interrupt(self, operation_id: str, *, owner: str | None) -> SubchatSubmission:
        """Persist an explicitly observed provider interruption, not a timeout."""
        old = self.get(operation_id, owner=owner)
        if old.state == 'interrupted':
            return old
        if old.state != 'submitted':
            raise ValueError('Only a confirmed submission can be marked interrupted')
        return self._replace(old, old.model_copy(update={'state': 'interrupted'}), owner)

    def begin_send(self, operation_id: str, *, owner: str | None,
                   conversation_id: str | None = None,
                   baseline_message_ids: tuple[str, ...] = (),
                   user_message_id: str | None = None,
                   provider_account_id: str | None = None) -> SubchatSubmission:
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
            'user_message_id': user_message_id,
            'provider_account_id': provider_account_id}), owner)

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

    def complete(self, operation_id: str, answer_message_id: str, answer: str,
                 *, owner: str | None,
                 reported_settings: SubchatReportedSettings | None = None) -> SubchatSubmission:
        old = self.get(operation_id, owner=owner)
        if not answer_message_id.strip() or not answer or answer_message_id == old.user_message_id:
            raise ValueError('A distinct observed answer identity and text are required')
        if old.state == 'completed':
            if (old.answer_message_id, old.answer, old.reported_settings) != (
                    answer_message_id, answer, reported_settings):
                raise ValueError('Completed subchat answer cannot be replaced')
            return old
        if old.state != 'submitted':
            raise ValueError('Submission identity must be observed before its answer')
        return self._replace(old, old.model_copy(update={
            'state': 'completed', 'answer_message_id': answer_message_id, 'answer': answer,
            'reported_settings': reported_settings}), owner, http_event='history_final')
