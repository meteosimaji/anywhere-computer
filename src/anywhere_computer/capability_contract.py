"""Tool requirements used to describe capabilities across diagnostic scopes."""

CAPABILITY_TOOLS: dict[str, frozenset[str]] = {
    "files": frozenset({"files_read", "files_write"}),
    "terminal": frozenset({"terminal_start", "terminal_output", "terminal_input",
                            "terminal_list", "terminal_stop"}),
    "browser_isolated": frozenset({"browser_open", "browser_navigate", "browser_observe",
                                    "browser_click", "browser_fill", "browser_close"}),
    "literal_search": frozenset({"search_start", "search_results", "search_list",
                                  "search_stop"}),
    "office_text_read": frozenset({"documents_read"}),
    "office_rendered_preview": frozenset({"documents_preview"}),
    "audio_capture": frozenset({"audio_status", "audio_capture"}),
    "gui_native": frozenset({"gui_native_windows", "gui_native_observe", "gui_native_close",
                             "gui_native_set_value", "gui_native_press"}),
    "gui_mcp": frozenset({"gui_observe", "gui_click", "gui_type", "gui_key"}),
    "skills": frozenset({"skills_list", "skills_read"}),
    "codex_skills": frozenset({"codex_skills_list", "codex_skill_read"}),
}
