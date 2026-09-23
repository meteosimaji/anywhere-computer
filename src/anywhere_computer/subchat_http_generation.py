"""Opt-in ordinary Chat HTTP generation from an explicit, in-memory handoff.

No credential or proof value is acquired or synthesized here. The operator hands
over headers observed on a successful Chrome generation and request body
templates. All handoff values are treated as sensitive. This transport never
launches Chrome or persists the handoff.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, BinaryIO
from urllib.parse import urlsplit

from pydantic import JsonValue, TypeAdapter

from .subchat import SubchatAccessError, SubchatStaleTarget
from .subchat_browser.request_content import generation_input
from .subchat_http_sender import HTTPGenerationPlan
from .subchat_sse import SSEDecoder
from .subchat_state import SubchatAccountMismatch, SubchatSubmission, SubchatSubmissions

if TYPE_CHECKING:
    from playwright.async_api import APIRequestContext, APIResponse


_HEADERS = frozenset({
    'accept', 'authorization', 'chatgpt-account-id', 'content-type', 'cookie', 'oai-did',
    'oai-echo-logs', 'oai-language', 'openai-sentinel-chat-requirements-prepare-token',
    'openai-sentinel-proof-token', 'openai-sentinel-turnstile-token', 'origin', 'originator',
    'referer', 'sec-ch-ua', 'sec-ch-ua-arch', 'sec-ch-ua-bitness', 'sec-ch-ua-full-version',
    'sec-ch-ua-full-version-list', 'sec-ch-ua-mobile', 'sec-ch-ua-model', 'sec-ch-ua-platform',
    'sec-ch-ua-platform-version', 'user-agent', 'x-oai-turn-trace-id',
    'x-openai-codex-window-type', 'x-openai-web-frontend', 'x-openai-web-sse-compression',
})
_CHAT = re.compile(r'[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}\Z')
_PATHS = {
    'sentinel': '/backend-api/sentinel/chat-requirements/prepare',
    'prepare': '/backend-api/f/conversation/prepare',
    'generation': '/backend-api/f/conversation',
}


@dataclass(frozen=True, slots=True, repr=False)
class ObservedHTTPGeneration:
    """Request-specific values held only for this process's lifetime."""

    _headers: bytes
    sentinel_p: str
    _prepare_template: bytes
    _generation_template: bytes

    def __repr__(self) -> str:
        return 'ObservedHTTPGeneration(<redacted>)'

    @property
    def headers(self) -> dict[str, str]:
        return TypeAdapter(dict[str, str]).validate_json(self._headers)

    @property
    def prepare_template(self) -> dict[str, JsonValue]:
        return TypeAdapter(dict[str, JsonValue]).validate_json(self._prepare_template)

    @property
    def generation_template(self) -> dict[str, JsonValue]:
        return TypeAdapter(dict[str, JsonValue]).validate_json(self._generation_template)

    @classmethod
    def from_data(cls, data: object, *, authorization: str, account_id: str
                  ) -> ObservedHTTPGeneration:
        if not isinstance(data, dict) or set(data) != {
                'headers', 'sentinel_p', 'prepare_template', 'generation_template'}:
            raise ValueError('Invalid HTTP generation handoff')
        raw_headers = data['headers']
        if (not isinstance(raw_headers, dict) or set(raw_headers) != _HEADERS
                or any(not isinstance(key, str) or key.lower() != key
                       or not isinstance(value, str) or not value
                       or len(value) > 16_384 or any(ord(char) < 32 for char in value)
                       for key, value in raw_headers.items())):
            raise ValueError('Invalid HTTP generation headers')
        if (raw_headers['authorization'] != authorization
                or raw_headers['chatgpt-account-id'] != account_id
                or raw_headers['origin'] != 'https://chatgpt.com'
                or urlsplit(raw_headers['referer']).scheme != 'https'
                or urlsplit(raw_headers['referer']).netloc != 'chatgpt.com'
                or raw_headers['content-type'].split(';', 1)[0] != 'application/json'):
            raise ValueError('HTTP generation session or origin changed')
        sentinel_p = data['sentinel_p']
        if not isinstance(sentinel_p, str) or not sentinel_p or len(sentinel_p) > 4096:
            raise ValueError('Invalid sentinel handoff')
        prepare = TypeAdapter(dict[str, JsonValue]).validate_python(data['prepare_template'])
        query = prepare.get('partial_query')
        functions = prepare.get('local_function_names')
        author = query.get('author') if isinstance(query, dict) else None
        content = query.get('content') if isinstance(query, dict) else None
        if (len(json.dumps(prepare).encode()) > 1_048_576
                or prepare.get('client_prepare_state') != 'sent'
                or prepare.get('action') != 'next'
                or type(prepare.get('is_do_not_remember')) is not bool
                or not isinstance(prepare.get('timezone'), str) or not prepare['timezone']
                or type(prepare.get('timezone_offset_min')) is not int
                or not isinstance(functions, list)
                or any(not isinstance(name, str) for name in functions)
                or not isinstance(query, dict)
                or not isinstance(author, dict) or author.get('role') != 'user'
                or not isinstance(content, dict) or content.get('content_type') != 'text'
                or not isinstance(query.get('id'), str)):
            raise ValueError('Invalid conversation preparation template')
        generation = TypeAdapter(dict[str, JsonValue]).validate_python(
            data['generation_template'])
        if not generation or len(json.dumps(generation).encode()) > 1_048_576:
            raise ValueError('Invalid generation template')
        return cls(json.dumps(raw_headers).encode(), sentinel_p,
                   json.dumps(prepare).encode(), json.dumps(generation).encode())

    def prepare_body(self, plan: HTTPGenerationPlan) -> bytes:
        body = self.prepare_template
        query = body['partial_query']
        assert isinstance(query, dict) and isinstance(query['content'], dict)
        query['id'] = plan.user_message_id
        query['content']['parts'] = [plan.prompt]
        body['model'] = plan.model_slug
        body['thinking_effort'] = plan.thinking_effort
        if plan.conversation_id is None:
            body.pop('conversation_id', None)
            body.pop('parent_message_id', None)
        else:
            body['conversation_id'] = plan.conversation_id
            body['parent_message_id'] = plan.predecessor_id
        return json.dumps(body, ensure_ascii=False, separators=(',', ':')).encode()

    def generation_body(self, plan: HTTPGenerationPlan,
                        submission: SubchatSubmission) -> bytes:
        body = json.loads(json.dumps(self.generation_template))
        messages = body.get('messages')
        if (body.get('action') != 'next' or not isinstance(messages, list)
                or len(messages) != 1 or not isinstance(messages[0], dict)):
            raise ValueError('Generation template shape changed')
        message = messages[0]
        content = message.get('content')
        if not isinstance(content, dict) or content.get('content_type') != 'text':
            raise ValueError('Generation template content changed')
        message['id'] = plan.user_message_id
        content['parts'] = [plan.prompt]
        message['create_time'] = time.time()
        body['model'] = plan.model_slug
        body['thinking_effort'] = plan.thinking_effort
        if plan.conversation_id is None:
            body.pop('conversation_id', None)
            body.pop('parent_message_id', None)
        else:
            body['conversation_id'] = plan.conversation_id
            body['parent_message_id'] = plan.predecessor_id
        raw = json.dumps(body, ensure_ascii=False, separators=(',', ':'))
        generation_input(raw, submission)
        if body.get('parent_message_id') != plan.predecessor_id:
            raise ValueError('Generation predecessor changed')
        return raw.encode()


def read_http_generation_handoff(source: BinaryIO, *, authorization: str,
                                 account_id: str) -> ObservedHTTPGeneration:
    """Consume one bounded JSON line; never include handoff contents in errors."""
    def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('Duplicate handoff field')
            result[key] = value
        return result

    try:
        raw = source.readline(1_572_865)
        if not raw.endswith(b'\n') or len(raw) > 1_572_864:
            raise ValueError('Missing or oversized handoff')
        data = json.loads(raw, object_pairs_hook=unique)
        return ObservedHTTPGeneration.from_data(data, authorization=authorization,
                                                account_id=account_id)
    except Exception:
        raise ValueError('Invalid HTTP generation handoff; no request was made') from None


def _url(origin: str, path: str) -> str:
    if origin != 'https://chatgpt.com' and not re.fullmatch(r'http://127\.0\.0\.1:[0-9]+', origin):
        raise ValueError('Unsupported HTTP generation origin')
    return origin + path


async def _json_response(response: APIResponse) -> dict[str, JsonValue]:
    if response.status in (401, 403):
        raise SubchatAccessError(response.status)
    if response.status != 200:
        raise ConnectionError('Chat preparation was not accepted')
    if response.headers.get('content-type', '').split(';', 1)[0].strip() != 'application/json':
        raise ValueError('Unexpected Chat preparation response')
    raw = await response.body()
    if len(raw) > 1_048_576:
        raise ValueError('Chat preparation response is too large')
    return TypeAdapter(dict[str, JsonValue]).validate_json(raw)


async def _post(client: APIRequestContext, url: str, *, body: bytes,
                headers: dict[str, str]) -> APIResponse:
    return await client.post(url, data=body, headers=headers,
                             timeout=120_000, max_redirects=0, max_retries=0)


async def _preparation_post(stage: str, *, plan: HTTPGenerationPlan,
                            client: APIRequestContext, store: SubchatSubmissions,
                            owner: str | None, origin: str, body: bytes,
                            headers: dict[str, str]) -> dict[str, JsonValue]:
    store.record_http_event(plan.operation_id, stage + '_request', owner=owner)
    try:
        response = await _post(client, _url(origin, _PATHS[stage]), body=body,
                               headers=headers)
        try:
            store.record_http_event(plan.operation_id, stage + '_response', owner=owner,
                                    status=response.status)
            return await _json_response(response)
        finally:
            await response.dispose()
    except SubchatAccessError:
        store.record_http_event(plan.operation_id, stage + '_failed', owner=owner)
        raise
    except Exception:
        store.record_http_event(plan.operation_id, stage + '_failed', owner=owner)
        raise ConnectionError('Chat preparation failed; outcome is unknown') from None


async def dispatch_generation(plan: HTTPGenerationPlan, submission: SubchatSubmission, *,
                              handoff: ObservedHTTPGeneration, client: APIRequestContext,
                              store: SubchatSubmissions, owner: str | None,
                              origin: str = 'https://chatgpt.com') -> None:
    """Prepare once, inspect branch, claim once, then POST once.

    Any failure after `begin_send` leaves the original operation in `sending`.
    A second call through Subchats will recover rather than repeat preparation.
    HTTP 200 and SSE markers are candidates only; history proves receipt/final.
    """
    if plan.account_id != handoff.headers['chatgpt-account-id']:
        raise SubchatAccountMismatch('HTTP generation account changed')
    try:
        body = handoff.generation_body(plan, submission)
    except Exception:
        store.record_http_event(plan.operation_id, 'generation_failed', owner=owner)
        raise ValueError('HTTP generation request is invalid') from None
    headers = {**handoff.headers, 'accept': 'application/json'}
    observed = await _preparation_post('sentinel', plan=plan, client=client, store=store,
        owner=owner, origin=origin, body=json.dumps({'p': handoff.sentinel_p}).encode(),
        headers=headers)
    token = observed.get('prepare_token')
    if not isinstance(token, str) or not token or len(token) > 16_384:
        store.record_http_event(plan.operation_id, 'sentinel_failed', owner=owner)
        raise ValueError('Sentinel preparation token is unavailable')
    # Keep the exact successful Chrome header value. A fresh sentinel response
    # token with the handed-off proof and Turnstile values is not yet verified.
    try:
        prepare_body = handoff.prepare_body(plan)
    except Exception:
        store.record_http_event(plan.operation_id, 'prepare_failed', owner=owner)
        raise ValueError('Chat preparation request is invalid') from None
    await _preparation_post('prepare', plan=plan, client=client, store=store,
                            owner=owner, origin=origin, body=prepare_body, headers=headers)
    if plan.conversation_id is not None:
        if _CHAT.fullmatch(plan.conversation_id) is None:
            store.record_http_event(plan.operation_id, 'branch_failed', owner=owner)
            raise ValueError('Invalid follow-up conversation identity')
        store.record_http_event(plan.operation_id, 'branch_request', owner=owner)
        try:
            branch = await client.get(_url(origin,
                '/backend-api/conversation/' + plan.conversation_id), headers=headers,
                timeout=15_000, max_redirects=0, max_retries=0)
            try:
                store.record_http_event(plan.operation_id, 'branch_response', owner=owner,
                                        status=branch.status)
                current = await _json_response(branch)
            finally:
                await branch.dispose()
        except SubchatAccessError:
            store.record_http_event(plan.operation_id, 'branch_failed', owner=owner)
            raise
        except Exception:
            store.record_http_event(plan.operation_id, 'branch_failed', owner=owner)
            raise ConnectionError('Chat branch check failed; outcome is unknown') from None
        if current.get('current_node') != plan.predecessor_id:
            store.record_http_event(plan.operation_id, 'branch_stale', owner=owner)
            raise SubchatStaleTarget('Provider conversation branch changed before dispatch')
    if not store.claim_http_dispatch(
            plan.operation_id, owner=owner, user_message_id=plan.user_message_id,
            provider_account_id=plan.account_id, prompt=plan.prompt,
            model_slug=plan.model_slug, thinking_effort=plan.thinking_effort,
            conversation_id=plan.conversation_id, predecessor_id=plan.predecessor_id):
        raise ValueError('HTTP generation was already claimed; recover without resending')
    # HTTPX exposes the candidate before the response ends. The operator's
    # observed generation headers are forwarded unchanged, including the copied
    # prepare token. HTTPX does not acquire or fabricate protection values.
    import httpx

    decoder = SSEDecoder()
    candidate = plan.conversation_id
    candidate_logged = False
    store.record_http_event(plan.operation_id, 'generation_request', owner=owner)
    try:
        async with httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(retries=0),
                                     follow_redirects=False, trust_env=False,
                                     timeout=httpx.Timeout(120.0, connect=10.0)) as sender:
            async with sender.stream('POST', _url(origin, _PATHS['generation']),
                                     content=body, headers=handoff.headers) as response:
                store.record_http_event(plan.operation_id, 'generation_response', owner=owner,
                                        status=response.status_code)
                if response.status_code in (401, 403):
                    store.observe_rejection(plan.operation_id, plan.user_message_id,
                                            response.status_code, owner=owner,
                                            provider_account_id=plan.account_id)
                    raise SubchatAccessError(response.status_code)
                if response.status_code != 200:
                    raise ConnectionError('Generation HTTP status was not successful')
                if response.headers.get('content-type', '').split(';', 1)[0].strip() != (
                        'text/event-stream'):
                    raise ValueError('Unexpected generation response format')
                async for chunk in response.aiter_bytes():
                    for event in decoder.feed(chunk):
                        if event.data == '[DONE]':
                            continue
                        try:
                            data = json.loads(event.data)
                        except json.JSONDecodeError:
                            continue  # Candidate observation does not interpret other SSE data.
                        if not isinstance(data, dict):
                            continue
                        observed_id = data.get('conversation_id')
                        if observed_id is None:
                            continue
                        if not isinstance(observed_id, str) or _CHAT.fullmatch(observed_id) is None:
                            raise ValueError('Invalid conversation candidate')
                        if candidate is not None and candidate != observed_id:
                            raise ValueError('Conflicting conversation candidates')
                        if candidate is None:
                            store.observe_conversation(plan.operation_id, plan.user_message_id,
                                observed_id, owner=owner, provider_account_id=plan.account_id)
                            candidate = observed_id
                        if not candidate_logged:
                            store.record_http_event(plan.operation_id, 'sse_candidate', owner=owner)
                            candidate_logged = True
                decoder.finish()
    except SubchatAccessError:
        store.record_http_event(plan.operation_id, 'generation_failed', owner=owner)
        raise
    except Exception:
        store.record_http_event(plan.operation_id, 'generation_failed', owner=owner)
        raise ConnectionError('Generation outcome is unknown; recover without resending') from None
