"""Submission lifecycle shared by browser adapters; no model or provider defaults."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol

from pydantic import Field, model_validator

from .models import Contract
from .subchat_content import SubchatResources
from .subchat_state import (
    SubchatAccountMismatch,
    SubchatConcurrentSend,
    SubchatHTTPSelection,
    SubchatReportedSettings,
    SubchatSelectionError,
    SubchatSubmission,
    SubchatSubmissions,
    SubchatWorkContext,
)

logger = logging.getLogger(__name__)


class SubchatReceipt(Contract):
    conversation_id: str = Field(min_length=1)
    user_message_id: str = Field(min_length=1)
    prompt: str


@dataclass(frozen=True)
class SubchatPreparedSend:
    """Locally prepared HTTP identity; this is not provider acceptance."""

    baseline_message_ids: tuple[str, ...]
    user_message_id: str
    provider_account_id: str


class SubchatAnswer(SubchatReceipt):
    answer_message_id: str = Field(min_length=1)
    text: str | None = None
    answer_type: Literal['text', 'image', 'multimodal'] = 'text'
    reported_settings: SubchatReportedSettings | None = None

    @model_validator(mode='after')
    def validate_content(self) -> SubchatAnswer:
        if (self.answer_type == 'text' and not self.text
                or self.answer_type == 'image' and self.text is not None
                or self.answer_type == 'multimodal' and not self.text):
            raise ValueError('Final answer type and text disagree')
        return self


class SubchatPendingObservation(Contract):
    """Call-scoped evidence, never a durable generation state or permission to retry."""

    operation_id: str = Field(pattern=r'^[0-9a-f]{32}$')
    source: Literal['http_history'] = 'http_history'
    reason: Literal[
        'input_not_observed', 'correlation_unavailable', 'correlation_ambiguous',
        'final_not_observed', 'final_ambiguous', 'final_not_complete', 'final_text_unavailable',
    ]
    observed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class SubchatObservedSubmission(SubchatSubmission):
    """Recovery response only: do not save ephemeral observations in the submission ledger."""

    observation: SubchatPendingObservation


class SubchatBackend(Protocol):
    async def prepare(self, submission: SubchatSubmission
                      ) -> tuple[str, ...] | SubchatPreparedSend:
        """Prepare input without submitting; failure is known not to have sent."""
        ...

    async def send(self, submission: SubchatSubmission) -> SubchatReceipt | None: ...

    async def find_submission(self, submission: SubchatSubmission) -> SubchatReceipt | None: ...

    async def read_answer(self, submission: SubchatSubmission
                          ) -> SubchatAnswer | SubchatPendingObservation | None: ...


class SubchatStaleTarget(ValueError):
    """The requested predecessor is no longer the last observed conversation turn."""


class SubchatAccessError(ConnectionError):
    """Observed HTTP access failure; contains no provider body or credentials."""

    def __init__(self, status: int) -> None:
        if status not in (401, 403):
            raise ValueError('Expected an authentication or access rejection')
        self.status = status
        self.code = 'authentication_required' if status == 401 else 'access_denied'
        super().__init__(self.code)


class SubchatBrowserClosed(ConnectionError):
    """The dedicated browser closed; saved operations must not be replayed."""


class SubchatInterrupted(ValueError):
    """The provider recorded interruption; partial output is not a completed answer."""


class SubchatOutcomeUnknown(RuntimeError):
    """The send stage was entered; the caller must recover rather than resubmit."""

    def __init__(self, operation_id: str) -> None:
        super().__init__('Subchat submission is unconfirmed; recover without resending')
        self.operation_id = operation_id


class SubchatPreparationFailed(ValueError):
    """Preparation failed before dispatch; provider details remain local."""


class SubchatPreflightFailed(ValueError):
    """HTTP preparation ended before any generation POST was claimed."""


class SubchatUnsupported(ValueError):
    """An explicit unavailable capability; no fallback or automatic retry is allowed."""

    def __init__(self, code: Literal['http_generation_unavailable', 'http_session_required',
                                    'http_delete_unavailable', 'ui_unavailable']) -> None:
        self.code = code
        super().__init__(code)


class Subchats:
    def __init__(self, store: SubchatSubmissions, backend: SubchatBackend) -> None:
        self.store = store
        self.backend = backend

    def _accept(self, submission: SubchatSubmission, receipt: SubchatReceipt,
                owner: str | None) -> SubchatSubmission:
        if receipt.prompt != submission.prompt:
            raise ValueError('Observed prompt does not match the subchat submission')
        return self.store.submitted(submission.operation_id, receipt.conversation_id,
                                    receipt.user_message_id, owner=owner)

    async def send(self, operation_id: str, prompt: str, model: str, effort: str,
                   *, owner: str | None,
                   conversation_id: str | None = None,
                   work_context: SubchatWorkContext | None = None,
                   resources: SubchatResources | None = None,
                   http_selection: SubchatHTTPSelection | None = None) -> SubchatSubmission:
        submission = self.store.prepare(operation_id, prompt, model, effort, owner=owner,
                                        conversation_id=conversation_id, work_context=work_context,
                                        resources=resources, http_selection=http_selection)
        if submission.state != 'prepared':
            # A duplicate request never enters the backend again, even after restart.
            return submission
        return await self._dispatch(submission, owner=owner)

    def queue(self, operation_id: str, target_operation_id: str, prompt: str,
              *, owner: str | None) -> SubchatSubmission:
        target = self.store.get(target_operation_id, owner=owner)
        validate = getattr(self.backend, 'validate_send_selection', None)
        if validate is not None:
            validate(target.http_selection)
        return self.store.prepare(operation_id, prompt, target.model, target.effort, owner=owner,
                                  conversation_id=target.conversation_id,
                                  work_context=target.work_context,
                                  after_operation_id=target_operation_id,
                                  http_selection=target.http_selection)

    async def _dispatch(self, submission: SubchatSubmission,
                        *, owner: str | None) -> SubchatSubmission:
        submission = self.store.get(submission.operation_id, owner=owner)
        if submission.state not in {'prepared', 'queued'}:
            return submission
        try:
            prepared = await self.backend.prepare(submission)
            identity_kind = getattr(self.backend, 'baseline_identity_kind', None)
            baseline_identity_kind = (
                identity_kind(submission) if identity_kind is not None else None)
        except (SubchatStaleTarget, SubchatBrowserClosed, SubchatAccessError,
                SubchatAccountMismatch,
                SubchatUnsupported, SubchatSelectionError):
            raise
        except Exception as error:
            raise SubchatPreparationFailed(str(error)) from error
        try:
            if isinstance(prepared, SubchatPreparedSend):
                submission = self.store.begin_send(
                    submission.operation_id, owner=owner,
                    baseline_message_ids=prepared.baseline_message_ids,
                    baseline_identity_kind=baseline_identity_kind,
                    user_message_id=prepared.user_message_id,
                    provider_account_id=prepared.provider_account_id)
            else:
                submission = self.store.begin_send(submission.operation_id, owner=owner,
                                                   baseline_message_ids=prepared,
                                                   baseline_identity_kind=baseline_identity_kind)
        except SubchatConcurrentSend:
            discard = getattr(self.backend, 'discard_prepared', None)
            if discard is not None:
                try:
                    await discard(submission)
                except Exception as error:
                    logger.warning('Concurrent Subchat page cleanup failed error_type=%s',
                                   type(error).__name__)
            raise
        try:
            receipt = await self.backend.send(submission)
            if receipt is None:
                # Dispatch happened, but receipt observation is not yet available.
                # Keep the durable reservation; recover/wait may observe it later.
                return self.store.get(submission.operation_id, owner=owner)
            return self._accept(submission, receipt, owner)
        except Exception as error:
            if self.store.get(submission.operation_id, owner=owner).state == 'preflight_failed':
                raise SubchatPreflightFailed('HTTP generation stopped before dispatch') from error
            # Provider errors may contain account data; retain only the cause locally.
            raise SubchatOutcomeUnknown(submission.operation_id) from error
        # Browser sends and claimed HTTP generation retain an uncertain outcome
        # after cancellation; unclaimed HTTP preflight is terminalized by backend.send.

    async def recover(self, operation_id: str, *, owner: str | None) -> SubchatSubmission:
        submission = self.store.get(operation_id, owner=owner)
        if submission.state == 'interrupted':
            raise SubchatInterrupted('Provider interruption is saved; do not resend')
        if submission.state == 'queued':
            assert submission.after_operation_id is not None
            target = await self.recover(submission.after_operation_id, owner=owner)
            if target.state != 'completed':
                if isinstance(target, SubchatObservedSubmission):
                    return SubchatObservedSubmission(**submission.model_dump(),
                                                     observation=target.observation)
                return submission
            return await self._dispatch(submission, owner=owner)
        if submission.state in {'prepared', 'completed', 'cancelled', 'preflight_failed'}:
            return submission
        if submission.state == 'sending':
            receipt = await self.backend.find_submission(submission)
            if receipt is None:
                return submission
            submission = self._accept(submission, receipt, owner)
        try:
            answer = await self.backend.read_answer(submission)
        except SubchatInterrupted:
            self.store.interrupt(operation_id, owner=owner)
            raise
        if isinstance(answer, SubchatPendingObservation):
            if answer.operation_id != submission.operation_id:
                raise ValueError('Observation belongs to a different subchat')
            return SubchatObservedSubmission(**submission.model_dump(), observation=answer)
        if answer is None:
            # Thinking or unavailable observation: leave the submission untouched.
            return submission
        self._accept(submission, answer, owner)
        completed = self.store.complete(
            operation_id, answer.answer_message_id, answer.text, owner=owner,
            answer_type=answer.answer_type, reported_settings=answer.reported_settings)
        release = getattr(self.backend, 'release_completed', None)
        if release is not None and completed.conversation_id is not None:
            try:
                await release(completed, keep_for_queue=self.store.has_queued_for_conversation(
                    completed.conversation_id, owner=owner))
            except Exception as error:
                # The final answer was committed before optional tab cleanup.
                logger.warning('Completed Subchat cleanup failed error_type=%s',
                               type(error).__name__)
        return completed
