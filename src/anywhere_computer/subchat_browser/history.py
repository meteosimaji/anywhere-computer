"""Read-only ordinary-Chat history projection; never replay generation requests."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal
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


_IMAGE_POINTER = re.compile(r'sediment://file_[0-9a-f]{32}\Z')
_IMAGE_MIMES = frozenset({'image/png', 'image/jpeg', 'image/webp'})


@dataclass(frozen=True, slots=True)
class BoundImage:
    pointer: str
    mime_type: str
    size_bytes: int
    width: int
    height: int


def _image_asset(part: dict[str, JsonValue]) -> BoundImage:
    pointer = part.get('asset_pointer')
    mime = part.get('mime_type')
    size = part.get('size_bytes')
    width = part.get('width')
    height = part.get('height')
    if (not isinstance(pointer, str) or not _IMAGE_POINTER.fullmatch(pointer)
            or not isinstance(mime, str) or mime not in _IMAGE_MIMES
            or type(size) is not int or size <= 0
            or type(width) is not int or width <= 0
            or type(height) is not int or height <= 0):
        raise ValueError('Saved image metadata is invalid')
    return BoundImage(pointer, mime, size, width, height)


def _final_parts(content: dict[str, JsonValue]
                 ) -> tuple[str | None, tuple[BoundImage, ...]] | None:
    """Keep exact text fragments and recognize only explicit image asset parts."""
    if content.get('content_type') not in {'text', 'multimodal_text'}:
        return None
    parts = content.get('parts')
    if not isinstance(parts, list) or not parts:
        return None
    fragments: list[str] = []
    images: list[BoundImage] = []
    for part in parts:
        if isinstance(part, str):
            fragments.append(part)
        elif isinstance(part, dict) and part.get('content_type') == 'image_asset_pointer':
            images.append(_image_asset(part))
        else:
            return None
    text = ''.join(fragments) or None
    return (text, tuple(images)) if text is not None or images else None


def bound_tool_images(history: HistoryPage, user: HistoryMessage, *,
                      before: HistoryMessage | None = None) -> tuple[BoundImage, ...]:
    """Find finished image tool results after the verified input in this exact turn."""
    keys = ('turn_exchange_id', 'working_turn_id')
    stop = history.messages.index(before) if before is not None else len(history.messages)
    images: list[BoundImage] = []
    for message in history.messages[history.messages.index(user) + 1:stop]:
        async_source = message.metadata.get('async_source')
        if (message.author.get('role') != 'tool' or message.channel != 'final'
                or message.status != 'finished_successfully'
                or not isinstance(async_source, str) or not async_source.strip()
                or any(message.metadata.get(key) != user.metadata.get(key) for key in keys)):
            continue
        if message.content.get('content_type') != 'multimodal_text':
            continue
        parsed = _final_parts(message.content)
        if parsed is not None:
            images.extend(parsed[1])
    return tuple(images)


def bound_final_images(message: HistoryMessage) -> tuple[BoundImage, ...]:
    parsed = _final_parts(message.content)
    return parsed[1] if parsed is not None else ()


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
    if history.messages.index(answer) <= history.messages.index(user):
        return SubchatPendingObservation(operation_id=submission.operation_id,
                                         reason='final_not_observed')
    finish = answer.metadata.get('finish_details')
    if isinstance(finish, dict) and finish.get('type') == 'interrupted':
        raise SubchatInterrupted('Provider recorded an interrupted response; do not resend')
    if (answer.status != 'finished_successfully' or answer.end_turn is not True
            or ('is_complete' in answer.metadata and answer.metadata['is_complete'] is not True)
            or ('finish_details' in answer.metadata
                and (not isinstance(finish, dict) or finish.get('type') != 'stop'))):
        return SubchatPendingObservation(operation_id=submission.operation_id,
                                         reason='final_not_complete')
    try:
        parsed = _final_parts(answer.content)
        tool_images = bound_tool_images(history, user, before=answer)
    except ValueError:
        return SubchatPendingObservation(operation_id=submission.operation_id,
                                         reason='final_text_unavailable')
    empty_final = (answer.content.get('content_type') == 'text'
                   and answer.content.get('parts') == [''])
    if parsed is None and not (empty_final and tool_images):
        return SubchatPendingObservation(operation_id=submission.operation_id,
                                         reason='final_text_unavailable')
    text, final_images = parsed if parsed is not None else (None, ())
    images = (*final_images, *tool_images)
    if images:
        turn_users = [message for message in history.messages
                      if message.author.get('role') == 'user'
                      and all(message.metadata.get(key) == user.metadata.get(key)
                              for key in ('turn_exchange_id', 'working_turn_id'))]
        if len(turn_users) != 1:
            return SubchatPendingObservation(operation_id=submission.operation_id,
                                             reason='correlation_ambiguous')
    answer_type: Literal['text', 'image', 'multimodal']
    if text is None:
        answer_type = 'image'
    elif images:
        answer_type = 'multimodal'
    else:
        answer_type = 'text'
    assert submission.conversation_id is not None and submission.user_message_id is not None
    # Optional evidence: retain only bounded provider settings, never arbitrary metadata.
    settings = {key: value for key in ('model_slug', 'thinking_effort')
                if isinstance(value := answer.metadata.get(key), str)
                and value.strip() and len(value) <= 256}
    reported = SubchatReportedSettings.model_validate(settings) if settings else None
    return SubchatAnswer(conversation_id=submission.conversation_id,
                         user_message_id=submission.user_message_id, prompt=submission.prompt,
                         answer_message_id=answer.id, text=text,
                         answer_type=answer_type, reported_settings=reported)


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
