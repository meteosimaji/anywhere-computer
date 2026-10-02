"""Explicit HTTP publication ceilings, independent of the installed private Engine.

This is an API offering boundary, not an OS sandbox: terminal commands run on
the owner's computer. A newly registered tool is never implicitly public.
"""

from typing import Literal

HTTPToolProfile = Literal["full", "public-core"]

PUBLIC_CORE_TOOLS = frozenset({
    "computer_status", "settings_get", "usage_stats", "workspace_open", "operations_get",
    "files_read", "files_read_many", "files_read_binary", "files_info",
    "files_write", "files_write_binary", "files_edit", "files_move", "files_restore",
    "directories_list", "directories_create",
    "documents_read", "documents_preview", "documents_write", "documents_edit_paragraph",
    "upload_begin", "upload_chunk", "upload_status", "upload_commit", "upload_abort",
    "upload_resolve", "download_begin", "download_read", "download_status", "download_close",
    "search_start", "search_results", "search_stop", "search_list",
    "terminal_start", "terminal_input", "terminal_output", "terminal_resize", "terminal_list",
    "terminal_stop", "processes_list", "processes_stop",
    "browser_open", "browser_tabs", "browser_tab_open", "browser_tab_close", "browser_close",
    "browser_dialogs", "browser_dialog_handle", "browser_navigate", "browser_observe",
    "browser_source", "browser_network", "browser_console", "browser_research",
    "browser_click", "browser_fill", "browser_key", "browser_drag", "browser_hover",
    "browser_select", "browser_scroll", "browser_file_upload", "browser_download",
})


def validate_http_tool_profile(profile: HTTPToolProfile, scopes: frozenset[str] | None,
                               *, has_subchat: bool) -> None:
    if profile not in {"full", "public-core"}:
        raise ValueError("Unknown HTTP tool profile")
    if profile == "public-core":
        if scopes is None or not scopes or not scopes <= PUBLIC_CORE_TOOLS:
            raise ValueError("Public core requires an explicit subset of its fixed tool list")
        if has_subchat:
            raise ValueError("Public core cannot select a Subchat gateway")
