"""Stdio Subchat entry point for the bundled Codex Plugin."""

from __future__ import annotations

import asyncio
import json
import os
import sys
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


def selected_chrome_login(state: Path) -> tuple[Path | None, str | None]:
    """Read an explicit profile and account pin without inferring either one."""
    record = _selection_record(state)
    configured = os.environ.get("ANYWHERE_SUBCHAT_CHROME_SOURCE_PROFILE")
    source_value = configured if configured is not None else record.get("chrome_source_profile")
    source = None
    if source_value is not None:
        if not isinstance(source_value, str):
            raise ValueError("Subchat Chrome source profile must be a path")
        source = Path(source_value).expanduser()
        if not source.is_absolute():
            raise ValueError("Subchat Chrome source profile must be an absolute path")
        source = source.resolve()
    if source is not None and (source == state or source in state.parents
                               or state in source.parents):
        raise ValueError("Subchat Chrome source profile and state must be separate")
    account_id = os.environ.get("ANYWHERE_SUBCHAT_EXPECTED_ACCOUNT_ID",
                                record.get("expected_account_id"))
    if account_id is not None and (not isinstance(account_id, str)
                                   or not account_id or len(account_id) > 256
                                   or account_id.strip() != account_id
                                   or any(ord(character) < 32 for character in account_id)):
        raise ValueError("Subchat expected_account_id must be a nonempty account ID")
    return source, account_id


def _selection_record(state: Path) -> dict[str, object]:
    """Read local account selection and explicit send consent."""
    selection = state.parent / "login-selection.json"
    record: dict[str, object] = {}
    try:
        selection_size = selection.stat().st_size
    except FileNotFoundError:
        selection_size = None
    except OSError as error:
        raise ValueError("Invalid Subchat login selection") from error
    if selection_size is not None:
        if selection_size > 4096:
            raise ValueError("Subchat login selection exceeds the size limit")
        try:
            record = json.loads(selection.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ValueError("Invalid Subchat login selection") from error
        if (not isinstance(record, dict)
                or not set(record).issubset({"chrome_source_profile", "expected_account_id",
                                             "enable_background_send"})
                or "chrome_source_profile" not in record
                or not isinstance(record["chrome_source_profile"], str)
                or not record["chrome_source_profile"]
                or ("enable_background_send" in record
                    and type(record["enable_background_send"]) is not bool)):
            raise ValueError("Subchat login selection requires chrome_source_profile")
    return record


def main() -> None:
    profile, state = plugin_paths()
    transport = os.environ.get("ANYWHERE_SUBCHAT_PLUGIN_TRANSPORT")
    if transport is None:
        # A local explicit send selection, account pin, and source profile
        # are all required before exposing mutation tools by default.
        source, pinned_account = selected_chrome_login(state)
        transport = ('browser-prepared-httpx'
                     if (sys.platform == 'darwin' and source is not None
                         and pinned_account is not None
                         and _selection_record(state).get('enable_background_send') is True)
                     else 'http-read-only')
    if transport == "http-read-only":
        source, account_id = selected_chrome_login(state)
        asyncio.run(run(None, state, mcp=True, http_only=True,
                        chrome_login_profile=profile if source is None else None,
                        chrome_login_source_profile=source, expected_account_id=account_id,
                        read_only_mcp=True))
    elif transport == "browser-send":
        browser = browser_send_profile(profile, state)
        asyncio.run(run(browser, state, mcp=True, http_read=True, minimized=True))
    elif transport == "browser-prepared-httpx":
        source, account_id = selected_chrome_login(state)
        browser = browser_send_profile(profile, state)
        asyncio.run(run(browser, state, mcp=True, http_read=True, minimized=True,
                        httpx_generation=True, browser_source_profile=source,
                        expected_account_id=account_id))
    else:
        raise ValueError("ANYWHERE_SUBCHAT_PLUGIN_TRANSPORT must be http-read-only "
                         "or browser-send or browser-prepared-httpx")
