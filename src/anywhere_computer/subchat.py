"""Submission lifecycle shared by browser adapters; no model or provider defaults."""

from typing import Protocol

from pydantic import Field

from .models import Contract
from .subchat_state import SubchatSubmission, SubchatSubmissions


class SubchatReceipt(Contract):
    conversation_id: str = Field(min_length=1)
    user_message_id: str = Field(min_length=1)
    prompt: str


class SubchatAnswer(SubchatReceipt):
    answer_message_id: str = Field(min_length=1)
    text: str = Field(min_length=1)


class SubchatBackend(Protocol):
    async def send(self, submission: SubchatSubmission) -> SubchatReceipt: ...

    async def find_submission(self, submission: SubchatSubmission) -> SubchatReceipt | None: ...

    async def read_answer(self, submission: SubchatSubmission) -> SubchatAnswer | None: ...


class SubchatOutcomeUnknown(RuntimeError):
    """The send stage was entered; the caller must recover rather than resubmit."""


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
                   conversation_id: str | None = None) -> SubchatSubmission:
        submission = self.store.prepare(operation_id, prompt, model, effort, owner=owner,
                                        conversation_id=conversation_id)
        if submission.state != 'prepared':
            # A duplicate request never enters the backend again, even after restart.
            return submission
        submission = self.store.begin_send(operation_id, owner=owner)
        try:
            receipt = await self.backend.send(submission)
            return self._accept(submission, receipt, owner)
        except Exception as error:
            # Provider errors may contain account data; retain only the cause locally.
            raise SubchatOutcomeUnknown(
                'Subchat submission is unconfirmed; recover this operation without resending'
            ) from error
        # Cancellation also leaves the committed 'sending' record intact.

    async def recover(self, operation_id: str, *, owner: str | None) -> SubchatSubmission:
        submission = self.store.get(operation_id, owner=owner)
        if submission.state in {'prepared', 'completed'}:
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
