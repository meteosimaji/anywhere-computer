"""Bound MCP tool results independently of the client or server provider."""

import json

from pydantic import JsonValue

from .plugin_images import IMAGE_LIMIT, MAX_IMAGES, bounded_image

TEXT_LIMIT = 64 * 1024


def _bounded_text(value: object, remaining: int) -> tuple[str, bool]:
    if not isinstance(value, str):
        return "", False
    raw = value.encode("utf-8")
    if len(raw) <= remaining:
        return value, False
    return raw[:remaining].decode("utf-8", errors="ignore"), True



def _strip_meta(value: JsonValue) -> JsonValue:
    if isinstance(value, dict):
        return {key: _strip_meta(item) for key, item in value.items() if key != "_meta"}
    if isinstance(value, list):
        return [_strip_meta(item) for item in value]
    return value



# Diagnostic claims from the provider, never authorization or a retry decision.
# Closed vocabularies prevent forwarding arbitrary metadata bodies or credentials.
_OUTCOME_VALUES = {
    'state': frozenset({'confirmed_change', 'confirmed_no_change', 'partial',
                        'dispatched_unverified', 'suspected_noop', 'refused', 'indeterminate'}),
    'dispatch_state': frozenset({'none', 'dispatched', 'may_have_dispatched'}),
    'effect': frozenset({'confirmed', 'partial', 'unverifiable', 'suspected_noop', 'refused'}),
    'evidence': frozenset({'verified_change', 'verified_no_change',
        'primary_change_verified_cleanup_failed', 'delivery_accepted', 'operation_still_running',
        'observed_no_change', 'request_refused', 'response_lost', 'completion_unknown'}),
    'escalation': frozenset({'none', 'correct_request', 'grant_permission', 'refresh_target',
        'reconnect_session', 'update_runtime', 'recover_side_effect', 'observe_before_retry'}),
    'refusal_reason': frozenset({'invalid_request', 'permission_denied', 'target_unavailable',
        'transport_session_unavailable', 'request_cancelled', 'runtime_incompatible',
        'foreground_consent_required', 'operation_unsupported'}),
    'retry_safety': frozenset({'safe', 'unsafe', 'not_applicable'}),
}


def _provider_diagnostics(meta: JsonValue) -> dict[str, JsonValue]:
    if not isinstance(meta, dict):
        return {}
    output: dict[str, JsonValue] = {}
    for key, allowed in _OUTCOME_VALUES.items():
        value = meta.get(key)
        if isinstance(value, str) and value in allowed:
            output[key] = value
    for key in ('mutation_dispatched', 'requires_fresh_observation', 'retry_safe'):
        value = meta.get(key)
        if isinstance(value, bool):
            output[key] = value
    return output


def normalize_tool_result(result: dict[str, JsonValue]) -> dict[str, JsonValue]:
    content = result.get("content", [])
    is_error = result.get("isError", False)
    if not isinstance(content, list) or not isinstance(is_error, bool):
        raise ValueError("Invalid plugin result")
    clean_content: list[JsonValue] = []
    total = 0
    truncated = False
    unsupported = 0
    image_bytes = 0
    image_count = 0
    omitted_images = 0
    for item in content:
        if not isinstance(item, dict) or not isinstance(item.get("type"), str):
            raise ValueError("Invalid plugin content")
        if item["type"] == "image":
            if image_count >= MAX_IMAGES:
                omitted_images += 1
                truncated = True
                continue
            image, size = bounded_image(item, IMAGE_LIMIT - image_bytes)
            if image is None:
                omitted_images += 1
                truncated = True
            else:
                clean_content.append(image)
                image_bytes += size
                image_count += 1
            continue
        if item["type"] != "text":
            unsupported += 1
            truncated = True
            continue
        if not isinstance(item.get("text"), str):
            raise ValueError("Invalid plugin text")
        text, cut = _bounded_text(item["text"], TEXT_LIMIT - total)
        if text:
            clean_content.append({"type": "text", "text": text})
            total += len(text.encode("utf-8"))
        truncated = truncated or cut
    output: dict[str, JsonValue] = {
        "content": clean_content, "is_error": is_error, "truncated": truncated,
    }
    diagnostics = _provider_diagnostics(result.get("_meta"))
    if diagnostics:
        output["provider_diagnostics"] = diagnostics
    if omitted_images:
        output["omitted_image_items"] = omitted_images
    if unsupported:
        output["unsupported_content_items"] = unsupported
    structured = result.get("structuredContent")
    if structured is not None:
        if not isinstance(structured, dict):
            raise ValueError("Invalid plugin structured data")
        raw = json.dumps(structured, ensure_ascii=False, allow_nan=False).encode()
        if len(raw) <= TEXT_LIMIT:
            output["structured_content"] = _strip_meta(structured)
        else:
            output["truncated"] = True
    return output
