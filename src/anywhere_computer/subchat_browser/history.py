"""Read-only ordinary-Chat history projection; never replay generation requests."""
from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from ..subchat import (
    SubchatAccessError,
    SubchatAnswer,
    SubchatInterrupted,
    SubchatPendingObservation,
    SubchatReceipt,
)
from ..subchat_state import SubchatReportedSettings, SubchatSubmission
from .request_content import matches_prompt

if TYPE_CHECKING:
    from playwright.async_api import Page, Response


class HistoryMessage(BaseModel):
    model_config = ConfigDict(strict=True)
    id: str = Field(min_length=1)
    author: dict[str, JsonValue]
    content: dict[str, JsonValue]
    metadata: dict[str, JsonValue]
    status: str
    channel: str | None = None
    end_turn: bool | None = None


class HistoryPage(BaseModel):
    model_config = ConfigDict(strict=True)
    conversation_id: str
    messages: list[HistoryMessage] = Field(max_length=1000)


def matched_input(payload: bytes, submission: SubchatSubmission
                  ) -> tuple[HistoryPage, HistoryMessage] | None:
    if len(payload) > 4_194_304:
        raise ValueError('Conversation response is too large')
    history = HistoryPage.model_validate_json(payload)
    if history.conversation_id != submission.conversation_id:
        raise ValueError('Conversation identity changed')
    if len({message.id for message in history.messages}) != len(history.messages):
        raise ValueError('Duplicate message identity')
    users = [message for message in history.messages
             if message.id == submission.user_message_id and message.author.get('role') == 'user']
    if len(users) != 1:
        return None  # Pagination or a missing identity is not permission to guess.
    user = users[0]
    if not matches_prompt(user.content, submission.wire_prompt):
        raise ValueError('Saved prompt does not match')
    if submission.resources is not None:
        resources = submission.resources
        if (user.metadata.get('attachments', []) != resources.files()
                or user.metadata.get('system_hints', []) != resources.hints()):
            raise ValueError('Saved message resources do not match')
    return history, user


def project_receipt(payload: bytes, submission: SubchatSubmission) -> SubchatReceipt | None:
    matched = matched_input(payload, submission)
    if matched is None:
        return None
    history, user = matched
    return SubchatReceipt(conversation_id=history.conversation_id,
                          user_message_id=user.id, prompt=submission.prompt)


def project_history(payload: bytes, submission: SubchatSubmission) -> SubchatAnswer | None:
    """Compatibility projection for callers that only need a verified final answer."""
    observed = project_observation(payload, submission)
    return observed if isinstance(observed, SubchatAnswer) else None


def project_observation(payload: bytes, submission: SubchatSubmission
                        ) -> SubchatAnswer | SubchatPendingObservation:
    matched = matched_input(payload, submission)
    if matched is None:
        return SubchatPendingObservation(operation_id=submission.operation_id,
                                         reason='input_not_observed')
    history, user = matched
    keys = ('request_id', 'turn_exchange_id', 'working_turn_id')
    if any(not isinstance(user.metadata.get(key), str) or not user.metadata[key] for key in keys):
        return SubchatPendingObservation(operation_id=submission.operation_id,
                                         reason='correlation_unavailable')
    matching_users = [message for message in history.messages
                      if message.author.get('role') == 'user'
                      and all(message.metadata.get(key) == user.metadata[key] for key in keys)]
    if len(matching_users) != 1:
        return SubchatPendingObservation(operation_id=submission.operation_id,
                                         reason='correlation_ambiguous')
    correlated = [message for message in history.messages
                  if message.author.get('role') == 'assistant'
                  and all(message.metadata.get(key) == user.metadata[key] for key in keys)]
    # Thinking can be cancelled before a final message exists. Use the provider's
    # explicit terminal marker, never absence of an answer or app-level idle.
    if any(message.content.get('content_type') == 'reasoning_recap'
           and message.end_turn is True
           and message.metadata.get('reasoning_status') == 'reasoning_cancelled'
           for message in correlated):
        raise SubchatInterrupted('Provider recorded cancelled reasoning; do not resend')
    answers = [message for message in correlated if message.channel == 'final']
    # Asynchronous provider work can finish under a new request ID. Require
    # explicit async evidence and both stable turn IDs, never conversation alone.
    async_answers = [message for message in history.messages
                     if message.author.get('role') == 'assistant'
                     and message.channel == 'final'
                     and isinstance(request_id := message.metadata.get('request_id'), str)
                     and request_id.strip()
                     and request_id != user.metadata['request_id']
                     and isinstance(async_source := message.metadata.get('async_source'), str)
                     and async_source.strip()
                     and message.metadata.get('message_type') == 'next'
                     and all(message.metadata.get(key) == user.metadata[key] for key in keys[1:])]
    if async_answers:
        turn_users = [message for message in history.messages
                      if message.author.get('role') == 'user'
                      and all(message.metadata.get(key) == user.metadata[key] for key in keys[1:])]
        if len(turn_users) != 1:
            return SubchatPendingObservation(operation_id=submission.operation_id,
                                             reason='correlation_ambiguous')
        answers.extend(async_answers)
    if len(answers) != 1:
        return SubchatPendingObservation(operation_id=submission.operation_id,
            reason='final_not_observed' if not answers else 'final_ambiguous')
    answer = answers[0]
    finish = answer.metadata.get('finish_details')
    if isinstance(finish, dict) and finish.get('type') == 'interrupted':
        raise SubchatInterrupted('Provider recorded an interrupted response; do not resend')
    if (answer.status != 'finished_successfully' or answer.end_turn is not True
            or ('is_complete' in answer.metadata and answer.metadata['is_complete'] is not True)
            or ('finish_details' in answer.metadata
                and (not isinstance(finish, dict) or finish.get('type') != 'stop'))):
        return SubchatPendingObservation(operation_id=submission.operation_id,
                                         reason='final_not_complete')
    parts = answer.content.get('parts')
    if (answer.content.get('content_type') != 'text' or not isinstance(parts, list)
            or len(parts) != 1 or not isinstance(parts[0], str) or not parts[0]):
        return SubchatPendingObservation(operation_id=submission.operation_id,
                                         reason='final_text_unavailable')
    assert submission.conversation_id is not None and submission.user_message_id is not None
    # Optional evidence: retain only bounded provider settings, never arbitrary metadata.
    settings = {key: value for key in ('model_slug', 'thinking_effort')
                if isinstance(value := answer.metadata.get(key), str)
                and value.strip() and len(value) <= 256}
    reported = SubchatReportedSettings.model_validate(settings) if settings else None
    return SubchatAnswer(conversation_id=submission.conversation_id,
                         user_message_id=submission.user_message_id, prompt=submission.prompt,
                         answer_message_id=answer.id, text=parts[0], reported_settings=reported)


async def observe_history(page: Page, submission: SubchatSubmission) -> Response:
    """Observe the application's own history response on an owned observation tab."""
    path = '/backend-api/conversations/' + str(submission.conversation_id)

    def matches(response: Response) -> bool:
        url = urlsplit(response.url)
        return (url.scheme == 'https' and url.netloc == 'chatgpt.com' and url.path == path
                and response.request.method == 'GET')

    async with page.expect_response(matches, timeout=15_000) as pending:
        await page.goto('https://chatgpt.com/c/' + str(submission.conversation_id),
                        wait_until='domcontentloaded')
    response = await pending.value
    if response.status in (401, 403):
        raise SubchatAccessError(response.status)
    if response.status != 200 or not await response.request.header_value('authorization'):
        raise ConnectionError('Authenticated conversation history was not observed')
    if response.headers.get('content-type', '').split(';', 1)[0].strip() != 'application/json':
        raise ValueError('Unexpected conversation response format')
    return response
