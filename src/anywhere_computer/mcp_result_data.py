"""Locate typed provider content through the known reply and routing envelopes."""

from typing import cast

from pydantic import JsonValue

MEDIA_TOOLS = frozenset({
    "codex_plugin_call", "mcp_call", "gui_observe", "gui_type", "gui_click", "gui_key",
    "browser_observe", "documents_preview", "gui_native_observe",
    "media_audio_clip", "media_video_frames",
})


def media_result_data(tool_name: str, reply: dict[str, JsonValue]
                      ) -> tuple[dict[str, JsonValue] | None, bool]:
    """Return the existing content container and whether this is a recovery lookup.

    Only documented wrappers are traversed, never arbitrary provider metadata. The
    returned dictionary belongs to the caller's envelope, allowing a wire-only copy
    to be projected or restored without changing durable operation results.
    """
    recovered = False
    current = reply
    for _ in range(4):
        data = current.get("data")
        if current.get("state") != "completed" or not isinstance(data, dict):
            return None, recovered
        if tool_name == "devices_call":
            nested_tool, result = data.get("tool"), data.get("result")
            if (not isinstance(data.get("device_id"), str)
                    or not isinstance(nested_tool, str) or not isinstance(result, dict)
                    or nested_tool.startswith(("devices_", "connection_setup_", "__"))):
                return None, recovered
            tool_name = nested_tool
            current = {"state": "completed", "data": result}
            continue
        if tool_name == "operations_get":
            recovered = True
            if (not isinstance(data.get("operation_id"), str)
                    or data.get("state") != "completed" or not isinstance(data.get("data"), dict)):
                return None, recovered
            current = data
            original_data = cast(dict[str, JsonValue], data["data"])
            # The ledger does not persist the original tool name. A routing
            # envelope identifies itself; otherwise this is the original content.
            if "device_id" in original_data and "tool" in original_data:
                tool_name = "devices_call"
            else:
                return original_data, recovered
            continue
        return (data if tool_name in MEDIA_TOOLS else None), recovered
    return None, recovered
