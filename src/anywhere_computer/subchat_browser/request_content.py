"""Add explicit resources to one browser-authenticated generation request.

Authentication and generation preparation remain the application's responsibility.
This is not an independent Chat HTTP client and never copies credential headers.
"""
from __future__ import annotations

import json
import re

from pydantic import JsonValue, TypeAdapter

from ..subchat_state import SubchatSubmission


def matches_prompt(content: JsonValue, prompt: str) -> bool:
    if content == {'content_type': 'text', 'parts': [prompt]}:
        return True
    if (not isinstance(content, dict) or set(content) != {'content_type', 'parts'}
            or content['content_type'] != 'text' or not isinstance(content['parts'], list)
            or len(content['parts']) != 1 or not isinstance(content['parts'][0], str)):
        return False
    # Ordinary Chat serializes generated URL decorations as Markdown links.
    # Only identical label/target URLs may reduce to the original literal input.
    literal = re.sub(r'\[(https?://[^\s\[\]()]+)\]\(\1\)', r'\1', content['parts'][0])
    return literal == prompt


def generation_input(payload: str, submission: SubchatSubmission) -> dict[str, JsonValue]:
    if len(payload) > 1_048_576:
        raise ValueError('A bounded request is required')
    body = TypeAdapter(dict[str, JsonValue]).validate_json(payload)
    messages = body.get('messages')
    if (body.get('action') != 'next' or not isinstance(messages, list)
            or len(messages) != 1 or not isinstance(messages[0], dict)):
        raise ValueError('Generation request shape changed')
    message = messages[0]
    author = message.get('author')
    metadata = message.get('metadata')
    if (not isinstance(author, dict) or author.get('role') != 'user'
            or not matches_prompt(message.get('content'), submission.prompt)
            or not isinstance(message.get('id'), str) or not message['id']
            or not isinstance(metadata, dict)
            or body.get('conversation_id') != submission.requested_conversation_id):
        raise ValueError('Generation input or conversation changed')
    if submission.http_selection is not None:
        selected = submission.http_selection
        if (body.get('model') != selected.model_slug
                or body.get('thinking_effort') != selected.thinking_effort):
            raise ValueError('Generation model or effort changed')
    # Do not silently merge an unrelated draft's attachments or plugin selection.
    if (metadata.get('attachments') or metadata.get('system_hints')
            or body.get('system_hints')):
        raise ValueError('Generation request already contains resources')
    return body


def add_resources(payload: str, submission: SubchatSubmission) -> str:
    resources = submission.resources
    if resources is None:
        raise ValueError('Explicit resources are required')
    body = generation_input(payload, submission)
    messages = body['messages']
    assert isinstance(messages, list) and isinstance(messages[0], dict)
    message = messages[0]
    metadata = message['metadata']
    assert isinstance(metadata, dict)
    message['content'] = {'content_type': 'text', 'parts': [submission.wire_prompt]}
    if resources.attachments:
        metadata['attachments'] = TypeAdapter(JsonValue).validate_python(resources.files())
    if resources.plugins:
        metadata['system_hints'] = list(resources.hints())
        body['system_hints'] = list(resources.hints())
    return json.dumps(body, ensure_ascii=False)
