"""Restore bounded MCP media only into its exact, known result content summary."""

from typing import cast

from pydantic import JsonValue

from .mcp_result_data import media_result_data
from .models import Reply
from .plugin_audio import AUDIO_LIMIT, MAX_AUDIO_ITEMS, audio_summary, bounded_audio
from .plugin_images import IMAGE_LIMIT, MAX_IMAGES, bounded_image, image_summary


def restore_reply_media(tool_name: str, result: dict[str, JsonValue]) -> Reply:
    """Parse a CallToolResult and restore content media by MIME, size and SHA-256.

    Only completed, documented result envelopes participate. Extra/missing media,
    mismatched summaries and invalid/oversized bytes reject the response; callers
    retain the original operation ID and recover instead of replaying execution.
    Provider metadata (including structured_content duplicates) stays summarized.
    Neither the received packet nor a durable result is mutated.
    """
    reply = Reply.model_validate(result.get('structuredContent'))
    envelope = cast(dict[str, JsonValue], reply.model_dump(mode='json'))
    data, _ = media_result_data(tool_name, envelope)
    if data is None:
        return reply
    content = data.get('content')
    if content is None:
        wire_items = result.get('content')
        if isinstance(wire_items, list) and any(
            isinstance(item, dict) and item.get('type') in ('image', 'audio')
            for item in wire_items
        ):
            raise ValueError('Unexpected remote media without content summaries')
        return reply
    if not isinstance(content, list):
        raise ValueError('Invalid remote media content')
    wire_content = result.get('content')
    if not isinstance(wire_content, list):
        raise ValueError('Missing remote media content')
    media: list[tuple[dict[str, JsonValue], dict[str, JsonValue]]] = []
    image_count = audio_count = image_bytes = audio_bytes = 0
    try:
        for item in wire_content:
            if not isinstance(item, dict):
                continue
            if item.get('type') == 'image':
                image_count += 1
                if image_count > MAX_IMAGES:
                    raise ValueError('Remote image count exceeds limit')
                clean, size = bounded_image(item, IMAGE_LIMIT - image_bytes)
                image_bytes += size
                summary = image_summary(clean) if clean is not None else None
            elif item.get('type') == 'audio':
                audio_count += 1
                if audio_count > MAX_AUDIO_ITEMS:
                    raise ValueError('Remote audio count exceeds limit')
                clean, size = bounded_audio(item, AUDIO_LIMIT - audio_bytes)
                audio_bytes += size
                summary = audio_summary(clean) if clean is not None else None
            else:
                continue
            if clean is None or summary is None:
                raise ValueError('Remote media exceeds supported limits')
            media.append((summary, clean))
    except ValueError:
        # Do not expose provider-supplied bytes or parser exception input.
        raise ValueError('Invalid or oversized remote media') from None

    restored: list[JsonValue] = []
    for item in content:
        if not isinstance(item, dict) or item.get('type') not in ('image', 'audio'):
            restored.append(item)
            continue
        if type(item.get('bytes')) is not int:
            raise ValueError('Invalid remote media byte count')
        match = next((index for index, (summary, _) in enumerate(media)
                      if item == summary), None)
        if match is None:
            raise ValueError('Missing or mismatched remote media summary')
        restored.append(media.pop(match)[1])
    if media:
        raise ValueError('Unexpected remote media without a matching summary')
    data['content'] = restored
    return Reply.model_validate(envelope)
