"""Read-only ordinary-Chat history projection; never replay generation requests."""
from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from ..subchat import SubchatAnswer
from ..subchat_state import SubchatSubmission

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


class SubchatInterrupted(ValueError):
    """The saved provider response records interruption, not a complete answer."""


def project_history(payload: bytes, submission: SubchatSubmission) -> SubchatAnswer | None:
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
    if user.content != {'content_type': 'text', 'parts': [submission.prompt]}:
        raise ValueError('Saved prompt does not match')
    keys = ('request_id', 'turn_exchange_id', 'working_turn_id')
    if any(not isinstance(user.metadata.get(key), str) or not user.metadata[key] for key in keys):
        return None
    answers = [message for message in history.messages
               if message.author.get('role') == 'assistant' and message.channel == 'final'
               and all(message.metadata.get(key) == user.metadata[key] for key in keys)]
    if len(answers) != 1:
        return None  # Regenerated alternatives require explicit reconciliation.
    answer = answers[0]
    finish = answer.metadata.get('finish_details')
    if isinstance(finish, dict) and finish.get('type') == 'interrupted':
        raise SubchatInterrupted('Provider recorded an interrupted response; do not resend')
    if (answer.status != 'finished_successfully' or answer.end_turn is not True
            or answer.metadata.get('is_complete') is not True
            or not isinstance(finish, dict) or finish.get('type') != 'stop'):
        return None
    parts = answer.content.get('parts')
    if (answer.content.get('content_type') != 'text' or not isinstance(parts, list)
            or len(parts) != 1 or not isinstance(parts[0], str) or not parts[0]):
        return None
    assert submission.conversation_id is not None and submission.user_message_id is not None
    return SubchatAnswer(conversation_id=submission.conversation_id,
                         user_message_id=submission.user_message_id, prompt=submission.prompt,
                         answer_message_id=answer.id, text=parts[0])


async def collect_history(page: Page, submission: SubchatSubmission) -> SubchatAnswer | None:
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
    if response.status != 200 or not await response.request.header_value('authorization'):
        raise ConnectionError('Authenticated conversation history was not observed')
    if response.headers.get('content-type', '').split(';', 1)[0].strip() != 'application/json':
        raise ValueError('Unexpected conversation response format')
    return project_history(await response.body(), submission)
