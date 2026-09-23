"""Stdio Subchat entry point for the bundled Codex Plugin."""

from __future__ import annotations

import asyncio
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


def main() -> None:
    profile, state = plugin_paths()
    transport = os.environ.get("ANYWHERE_SUBCHAT_PLUGIN_TRANSPORT", "http-read-only")
    if transport == "http-read-only":
        asyncio.run(run(None, state, mcp=True, http_only=True,
                        chrome_login_profile=profile, read_only_mcp=True))
    elif transport == "browser-send":
        browser = browser_send_profile(profile, state)
        asyncio.run(run(browser, state, mcp=True, http_read=True, minimized=True))
    else:
        raise ValueError("ANYWHERE_SUBCHAT_PLUGIN_TRANSPORT must be http-read-only "
                         "or browser-send")
