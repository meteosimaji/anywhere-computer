"""One-shot localhost generation transport for an unintegrated HTTP sender.

This exercises reservation, HTTP streaming and candidate checkpointing without
claiming that ChatGPT accepts an independently prepared generation request.
No production endpoint, credential discovery or browser fallback is provided.
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

from .subchat import SubchatAccessError
from .subchat_sse import SSEDecoder
from .subchat_state import SubchatAccountMismatch, SubchatSubmission

CONVERSATION_ID = re.compile(r'[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}\Z')


@dataclass(frozen=True)
class HTTPGenerationPlan:
    """The exact identity and input already committed before dispatch."""

    operation_id: str
    account_id: str
    user_message_id: str
    prompt: str
    model_slug: str
    thinking_effort: str | None
    conversation_id: str | None
    predecessor_id: str | None

    @classmethod
    def from_reserved(cls, submission: SubchatSubmission) -> HTTPGenerationPlan:
        if (submission.state != 'sending' or submission.user_message_id is None
                or submission.provider_account_id is None or submission.http_selection is None):
            raise ValueError('An exact HTTP input, account and selection must be reserved')
        if submission.resources is not None:
            raise ValueError('HTTP generation resources are not implemented')
        if (submission.requested_conversation_id is None) != (
                submission.expected_last_user_message_id is None):
            raise ValueError('Follow-up conversation and predecessor must be paired')
        return cls(submission.operation_id, submission.provider_account_id,
                   submission.user_message_id, submission.prompt,
                   submission.http_selection.model_slug,
                   submission.http_selection.thinking_effort,
                   submission.requested_conversation_id,
                   submission.expected_last_user_message_id)

    def body(self) -> bytes:
        # This local fixture shape is not a claim about the provider's current
        # request-specific preparation or accepted generation schema.
        body = {
            'action': 'next',
            'messages': [{'id': self.user_message_id, 'author': {'role': 'user'},
                          'content': {'content_type': 'text', 'parts': [self.prompt]},
                          'metadata': {}}],
            'model': self.model_slug, 'thinking_effort': self.thinking_effort,
        }
        if self.conversation_id is not None:
            body['conversation_id'] = self.conversation_id
            body['parent_message_id'] = self.predecessor_id
        return json.dumps(body, ensure_ascii=False, separators=(',', ':')).encode('utf-8')


@dataclass(frozen=True)
class HTTPStreamObservation:
    """Transport evidence only; never a verified receipt or final answer."""

    conversation_id: str | None
    event_count: int
    done_marker: bool


def _localhost_url(url: str) -> None:
    parsed = urlsplit(url)
    if (parsed.scheme != 'http' or parsed.hostname != '127.0.0.1'
            or parsed.port is None or parsed.username is not None
            or parsed.password is not None or parsed.fragment or parsed.query
            or parsed.path != '/backend-api/f/conversation'):
        raise ValueError('Only the exact loopback generation test path is supported')


async def post_once(plan: HTTPGenerationPlan, *, url: str, account_id: str,
                    on_conversation: Callable[[str], None],
                    on_rejection: Callable[[int], None]) -> HTTPStreamObservation:
    """Dispatch one POST and checkpoint consistent conversation candidates.

    The caller owns the durable reservation and supplies a synchronous checkpoint
    callback. Every transport error after entry has an unknown remote outcome;
    callers must recover the original operation rather than invoke this again.
    """
    _localhost_url(url)
    if plan.account_id != account_id:
        raise SubchatAccountMismatch('Reserved HTTP account changed')
    # Kept out of normal runtime dependencies until a real provider contract is
    # established. This module is not imported by the production HTTP backend.
    import httpx

    candidate = plan.conversation_id
    decoder = SSEDecoder()
    done = False
    async with httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(retries=0),
                                 follow_redirects=False, trust_env=False,
                                 timeout=httpx.Timeout(120.0, connect=10.0)) as client:
        async with client.stream('POST', url, content=plan.body(), headers={
                'content-type': 'application/json', 'accept': 'text/event-stream',
                'chatgpt-account-id': account_id}) as response:
            if response.status_code in (401, 403):
                on_rejection(response.status_code)
                raise SubchatAccessError(response.status_code)
            if response.status_code != 200:
                raise ConnectionError('Generation HTTP status was not successful')
            if response.headers.get('content-type', '').split(';', 1)[0].strip() != (
                    'text/event-stream'):
                raise ValueError('Unexpected generation response format')
            async for chunk in response.aiter_bytes():
                for event in decoder.feed(chunk):
                    if event.data == '[DONE]':
                        done = True
                        continue
                    data = json.loads(event.data)
                    if not isinstance(data, dict):
                        raise ValueError('Invalid generation event shape')
                    observed = data.get('conversation_id')
                    if observed is None:
                        continue
                    if (not isinstance(observed, str)
                            or CONVERSATION_ID.fullmatch(observed) is None):
                        raise ValueError('Invalid conversation candidate')
                    if candidate is not None and candidate != observed:
                        raise ValueError('Conflicting conversation candidates')
                    if candidate is None:
                        on_conversation(observed)
                        candidate = observed
            decoder.finish()
    return HTTPStreamObservation(candidate, decoder.event_count, done)
