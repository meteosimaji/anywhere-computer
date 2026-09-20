"""Submission lifecycle shared by browser adapters; no model or provider defaults."""

from typing import Protocol

from pydantic import Field

from .models import Contract
from .subchat_state import SubchatSubmission, SubchatSubmissions, SubchatWorkContext


class SubchatReceipt(Contract):
    conversation_id: str = Field(min_length=1)
    user_message_id: str = Field(min_length=1)
    prompt: str


class SubchatAnswer(SubchatReceipt):
    answer_message_id: str = Field(min_length=1)
    text: str = Field(min_length=1)


class SubchatBackend(Protocol):
    async def prepare(self, submission: SubchatSubmission) -> tuple[str, ...]:
        """Prepare input without submitting; failure is known not to have sent."""
        ...

    async def send(self, submission: SubchatSubmission) -> SubchatReceipt | None: ...

    async def find_submission(self, submission: SubchatSubmission) -> SubchatReceipt | None: ...

    async def read_answer(self, submission: SubchatSubmission) -> SubchatAnswer | None: ...


class SubchatStaleTarget(ValueError):
    """The requested predecessor is no longer the last observed conversation turn."""


class SubchatOutcomeUnknown(RuntimeError):
    """The send stage was entered; the caller must recover rather than resubmit."""

    def __init__(self, operation_id: str) -> None:
        super().__init__('Subchat submission is unconfirmed; recover without resending')
        self.operation_id = operation_id


class SubchatPreparationFailed(ValueError):
    """Preparation failed before dispatch; provider details remain local."""


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
                   work_context: SubchatWorkContext | None = None) -> SubchatSubmission:
        submission = self.store.prepare(operation_id, prompt, model, effort, owner=owner,
                                        conversation_id=conversation_id, work_context=work_context)
        if submission.state != 'prepared':
            # A duplicate request never enters the backend again, even after restart.
            return submission
        return await self._dispatch(submission, owner=owner)

    def queue(self, operation_id: str, target_operation_id: str, prompt: str,
              *, owner: str | None) -> SubchatSubmission:
        target = self.store.get(target_operation_id, owner=owner)
        return self.store.prepare(operation_id, prompt, target.model, target.effort, owner=owner,
                                  conversation_id=target.conversation_id,
                                  work_context=target.work_context,
                                  after_operation_id=target_operation_id)

    async def _dispatch(self, submission: SubchatSubmission,
                        *, owner: str | None) -> SubchatSubmission:
        submission = self.store.get(submission.operation_id, owner=owner)
        if submission.state not in {'prepared', 'queued'}:
            return submission
        try:
            baseline = await self.backend.prepare(submission)
        except SubchatStaleTarget:
            raise
        except Exception as error:
            raise SubchatPreparationFailed(str(error)) from error
        submission = self.store.begin_send(submission.operation_id, owner=owner,
                                           baseline_message_ids=baseline)
        try:
            receipt = await self.backend.send(submission)
            if receipt is None:
                # Dispatch happened, but receipt observation is not yet available.
                # Keep the durable reservation; recover/wait may observe it later.
                return submission
            return self._accept(submission, receipt, owner)
        except Exception as error:
            # Provider errors may contain account data; retain only the cause locally.
            raise SubchatOutcomeUnknown(submission.operation_id) from error
        # Cancellation also leaves the committed 'sending' record intact.

    async def recover(self, operation_id: str, *, owner: str | None) -> SubchatSubmission:
        submission = self.store.get(operation_id, owner=owner)
        if submission.state == 'queued':
            assert submission.after_operation_id is not None
            target = await self.recover(submission.after_operation_id, owner=owner)
            if target.state != 'completed':
                return submission
            return await self._dispatch(submission, owner=owner)
        if submission.state in {'prepared', 'completed', 'cancelled'}:
            return submission
        if submission.state == 'sending':
            receipt = await self.backend.find_submission(submission)
            if receipt is None:
                return submission
            submission = self._accept(submission, receipt, owner)
        answer = await self.backend.read_answer(submission)
        if answer is None:
            # Thinking or unavailable observation: leave the submission untouched.
            return submission
        self._accept(submission, answer, owner)
        return self.store.complete(operation_id, answer.answer_message_id, answer.text, owner=owner)
