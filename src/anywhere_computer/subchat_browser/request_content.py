"""Add explicit resources to one browser-authenticated generation request.

Authentication and generation preparation remain the application's responsibility.
This is not an independent Chat HTTP client and never copies credential headers.
"""
from __future__ import annotations

import json

from pydantic import JsonValue, TypeAdapter

from ..subchat_state import SubchatSubmission


def add_resources(payload: str, submission: SubchatSubmission) -> str:
    resources = submission.resources
    if resources is None or len(payload) > 1_048_576:
        raise ValueError('Explicit resources and a bounded request are required')
    body = TypeAdapter(dict[str, JsonValue]).validate_json(payload)
    messages = body.get('messages')
    if (body.get('action') != 'next' or not isinstance(messages, list)
            or len(messages) != 1 or not isinstance(messages[0], dict)):
        raise ValueError('Generation request shape changed')
    message = messages[0]
    author = message.get('author')
    metadata = message.get('metadata')
    if (not isinstance(author, dict) or author.get('role') != 'user'
            or message.get('content') != {'content_type': 'text', 'parts': [submission.prompt]}
            or not isinstance(message.get('id'), str) or not message['id']
            or not isinstance(metadata, dict)
            or body.get('conversation_id') != submission.requested_conversation_id):
        raise ValueError('Generation input or conversation changed')
    # Do not silently merge an unrelated draft's attachments or plugin selection.
    if (metadata.get('attachments') or metadata.get('system_hints')
            or body.get('system_hints')):
        raise ValueError('Generation request already contains resources')
    message['content'] = {'content_type': 'text', 'parts': [submission.wire_prompt]}
    if resources.attachments:
        metadata['attachments'] = TypeAdapter(JsonValue).validate_python(resources.files())
    if resources.plugins:
        metadata['system_hints'] = list(resources.hints())
        body['system_hints'] = list(resources.hints())
    return json.dumps(body, ensure_ascii=False)
