"""Stdio Subchat entry point for the bundled Codex Plugin."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from .state import state_directory
from .subchat_cli import run


def _configured_path(name: str, default: Path) -> Path:
    value = os.environ.get(name)
    path = Path(value).expanduser() if value is not None else default
    if not path.is_absolute():
        raise ValueError(f"{name} must be an absolute path")
    return path.resolve()


def plugin_paths() -> tuple[Path, Path]:
    """Keep the Plugin's headless Chat login and ledger separate from the main engine."""
    root = state_directory().resolve() / "subchat"
    profile = _configured_path("ANYWHERE_SUBCHAT_CHROME_LOGIN_PROFILE", root / "chrome-login")
    state = _configured_path("ANYWHERE_SUBCHAT_STATE_DIR", root / "ledger")
    if profile == state or profile in state.parents or state in profile.parents:
        raise ValueError("Subchat browser profile and state directory must be separate")
    return profile, state


def browser_send_profile(profile: Path, state: Path) -> Path:
    """Reuse the dedicated login profile unless the operator selects another."""
    browser = _configured_path("ANYWHERE_SUBCHAT_BROWSER_SEND_PROFILE", profile)
    if browser == state or browser in state.parents or state in browser.parents:
        raise ValueError("Subchat browser send profile must be separate from state")
    return browser


def selected_chrome_source(state: Path) -> Path | None:
    """Read an explicit local account selection; never infer a Chrome profile."""
    configured = os.environ.get("ANYWHERE_SUBCHAT_CHROME_SOURCE_PROFILE")
    if configured is not None:
        source = _configured_path("ANYWHERE_SUBCHAT_CHROME_SOURCE_PROFILE", state)
    else:
        selection = state.parent / "login-selection.json"
        if not selection.exists():
            return None
        if selection.stat().st_size > 4096:
            raise ValueError("Subchat login selection exceeds the size limit")
        try:
            record = json.loads(selection.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ValueError("Invalid Subchat login selection") from error
        if (not isinstance(record, dict)
                or set(record) != {"chrome_source_profile"}
                or not isinstance(record["chrome_source_profile"], str)
                or not record["chrome_source_profile"]):
            raise ValueError("Subchat login selection requires chrome_source_profile")
        source = Path(record["chrome_source_profile"]).expanduser()
        if not source.is_absolute():
            raise ValueError("Subchat Chrome source profile must be an absolute path")
        source = source.resolve()
    if source == state or source in state.parents or state in source.parents:
        raise ValueError("Subchat Chrome source profile and state must be separate")
    return source


def main() -> None:
    profile, state = plugin_paths()
    transport = os.environ.get("ANYWHERE_SUBCHAT_PLUGIN_TRANSPORT", "http-read-only")
    if transport == "http-read-only":
        source = selected_chrome_source(state)
        asyncio.run(run(None, state, mcp=True, http_only=True,
                        chrome_login_profile=profile if source is None else None,
                        chrome_login_source_profile=source, read_only_mcp=True))
    elif transport == "browser-send":
        browser = browser_send_profile(profile, state)
        asyncio.run(run(browser, state, mcp=True, http_read=True, minimized=True))
    else:
        raise ValueError("ANYWHERE_SUBCHAT_PLUGIN_TRANSPORT must be http-read-only "
                         "or browser-send")
