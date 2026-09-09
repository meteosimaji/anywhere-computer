"""Packaged, inert-data workspace UI and MCP Apps metadata."""

import hashlib
from functools import lru_cache
from importlib.resources import files

from pydantic import JsonValue

UI_EXTENSION = "io.modelcontextprotocol/ui"
UI_MIME = "text/html;profile=mcp-app"
UI_ACTIONS = frozenset({
    "files_read", "files_write", "files_info", "files_read_binary", "documents_read",
    "directories_list", "settings_get", "settings_update", "operations_get",
    "devices_list", "devices_tools", "devices_call",
})


@lru_cache(maxsize=1)
def workspace_resource() -> dict[str, JsonValue]:
    html = files("anywhere_computer").joinpath("web/workspace.html").read_text(encoding="utf-8")
    digest = hashlib.sha256(html.encode()).hexdigest()[:20]
    return {
        "uri": f"ui://anywhere-computer/workspace-{digest}.html",
        "name": "Anywhere Computer workspace",
        "mimeType": UI_MIME,
        "text": html,
        "_meta": {"ui": {
            "prefersBorder": True,
            "csp": {"connectDomains": [], "resourceDomains": [], "frameDomains": []},
        }},
    }


def supports_ui(capabilities: dict[str, JsonValue]) -> bool:
    extensions = capabilities.get("extensions")
    ui = extensions.get(UI_EXTENSION) if isinstance(extensions, dict) else None
    mime_types = ui.get("mimeTypes") if isinstance(ui, dict) else None
    return isinstance(mime_types, list) and UI_MIME in mime_types


def with_ui_metadata(tools: list[JsonValue], *, enabled: bool) -> list[JsonValue]:
    if not enabled:
        return tools
    result: list[JsonValue] = []
    for tool in tools:
        if not isinstance(tool, dict):
            raise ValueError("Invalid tool descriptor")
        name = tool.get("name")
        if name == "workspace_open":
            uri = workspace_resource()["uri"]
            result.append({**tool, "_meta": {
                "ui": {"resourceUri": uri, "visibility": ["model", "app"]},
                "openai/outputTemplate": uri,
            }})
        elif isinstance(name, str) and name in UI_ACTIONS:
            result.append({**tool, "_meta": {
                "ui": {"visibility": ["model", "app"]}, "openai/widgetAccessible": True,
            }})
        else:
            result.append(tool)
    return result
