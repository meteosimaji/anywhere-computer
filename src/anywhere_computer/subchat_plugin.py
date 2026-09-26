"""Stdio Subchat entry point for the bundled Codex Plugin."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
from pathlib import Path

from .state import state_directory
from .subchat_chrome_profile import chrome_profile_by_id, selected_chrome_source
from .subchat_cli import run


def _configured_path(name: str, default: Path) -> Path:
    value = os.environ.get(name)
    path = Path(value).expanduser() if value is not None else default
    if not path.is_absolute():
        raise ValueError(f"{name} must be an absolute path")
    return path.resolve()


def plugin_paths() -> tuple[Path, Path]:
    """Keep the Plugin's headless Chat login and ledger separate from the main engine."""
    if sys.platform == "win32" and any(os.environ.get(name) is not None for name in (
        "ANYWHERE_STATE_DIR", "ANYWHERE_SUBCHAT_CHROME_LOGIN_PROFILE",
        "ANYWHERE_SUBCHAT_STATE_DIR",
    )):
        # Windows cookie storage must inherit the user's private LocalAppData ACL.
        raise ValueError("Windows Subchat requires the default local profile and state paths")
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
    if (_selection_record(state).get("dedicated_browser_channel") is not None
            and browser != profile):
        raise ValueError("Browser profile override conflicts with the dedicated selection")
    return browser


def selected_browser_channel(state: Path) -> str:
    """Use the locally selected browser, rejecting a conflicting override."""
    selected = _selection_record(state).get("dedicated_browser_channel")
    configured = os.environ.get("ANYWHERE_SUBCHAT_BROWSER_CHANNEL")
    if selected is not None and configured is not None and selected != configured:
        raise ValueError("Browser channel override conflicts with the selected browser")
    channel = configured if configured is not None else selected or "chrome"
    if channel not in {"chrome", "msedge"}:
        raise ValueError("ANYWHERE_SUBCHAT_BROWSER_CHANNEL must be chrome or msedge")
    return channel


def selected_chrome_login(state: Path) -> tuple[Path | None, str | None]:
    """Read an explicit profile and account pin without inferring either one."""
    record = _selection_record(state)
    configured = os.environ.get("ANYWHERE_SUBCHAT_CHROME_SOURCE_PROFILE")
    if record.get("dedicated_browser_channel") is not None and configured is not None:
        raise ValueError("Chrome source override conflicts with the dedicated browser")
    profile_id = record.get("chrome_profile_id")
    if configured is not None and profile_id is not None:
        raise ValueError("Environment profile path cannot override a selected Chrome ID")
    source_value = configured if configured is not None else record.get("chrome_source_profile")
    source = None
    if configured is None and profile_id is not None:
        if not isinstance(profile_id, str):
            raise ValueError("Subchat Chrome profile ID is invalid")
        source = chrome_profile_by_id(profile_id)
    elif source_value is not None:
        if not isinstance(source_value, str):
            raise ValueError("Subchat Chrome source profile must be a path")
        source = Path(source_value).expanduser()
        if not source.is_absolute():
            raise ValueError("Subchat Chrome source profile must be an absolute path")
        if sys.platform == "darwin":
            source = selected_chrome_source(
                source, stage_root=state.parent / "staged-chrome-profiles")
        else:
            source = source.resolve()
    if source is not None and (source == state or source in state.parents
                               or state in source.parents):
        raise ValueError("Subchat Chrome source profile and state must be separate")
    account_override = os.environ.get("ANYWHERE_SUBCHAT_EXPECTED_ACCOUNT_ID")
    selected_account = record.get("expected_account_id")
    if (record.get("dedicated_browser_channel") is not None
            and account_override is not None and account_override != selected_account):
        raise ValueError("Account override conflicts with the selected browser account")
    account_id = account_override if account_override is not None else selected_account
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
        metadata = selection.lstat()
    except FileNotFoundError:
        metadata = None
    except OSError as error:
        raise ValueError("Invalid Subchat login selection") from error
    if metadata is not None:
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("Invalid Subchat login selection")
        if metadata.st_size > 4096:
            raise ValueError("Subchat login selection exceeds the size limit")
        try:
            record = json.loads(selection.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ValueError("Invalid Subchat login selection") from error
        if (not isinstance(record, dict)
                or not set(record).issubset({"chrome_source_profile", "chrome_profile_id",
                                             "dedicated_browser_channel",
                                             "dedicated_profile",
                                             "expected_account_id", "enable_background_send"})
                or sum(key in record for key in ("chrome_source_profile",
                                                  "chrome_profile_id",
                                                  "dedicated_browser_channel")) != 1
                or ("chrome_source_profile" in record and
                    (not isinstance(record["chrome_source_profile"], str)
                     or not record["chrome_source_profile"]))
                or ("chrome_profile_id" in record and
                    (not isinstance(record["chrome_profile_id"], str)
                     or not record["chrome_profile_id"]))
                or ("dedicated_browser_channel" in record and
                    (not isinstance(record["dedicated_browser_channel"], str)
                     or record["dedicated_browser_channel"] not in {"chrome", "msedge"}
                     or not isinstance(record.get("dedicated_profile"), str)
                     or not Path(str(record["dedicated_profile"])).is_absolute()
                     or not isinstance(record.get("expected_account_id"), str)
                     or not record["expected_account_id"]))
                or ("dedicated_profile" in record
                    and "dedicated_browser_channel" not in record)
                or ("enable_background_send" in record
                    and type(record["enable_background_send"]) is not bool)):
            raise ValueError("Subchat login selection requires one Chrome profile")
        if "chrome_profile_id" in record or "dedicated_browser_channel" in record:
            getuid = getattr(os, "getuid", None)
            if ((sys.platform != "win32" and metadata.st_mode & 0o077)
                    or (getuid is not None and metadata.st_uid != getuid())):
                raise ValueError("Subchat profile selection is not private")
    return record


def main() -> None:
    profile, state = plugin_paths()
    selection = _selection_record(state)
    selected_profile = selection.get("dedicated_profile")
    if selection.get("dedicated_browser_channel") is not None:
        if sys.platform != "win32":
            raise ValueError("Dedicated browser selection is supported on Windows only")
        if (not isinstance(selected_profile, str)
                or Path(selected_profile).resolve() != profile):
            raise ValueError("Browser profile override conflicts with the dedicated selection")
    browser_channel = selected_browser_channel(state)
    transport = os.environ.get("ANYWHERE_SUBCHAT_PLUGIN_TRANSPORT")
    if transport is None:
        # A local explicit send selection and account pin are required.
        source, pinned_account = selected_chrome_login(state)
        transport = ('browser-prepared-httpx'
                     if (pinned_account is not None
                         and selection.get('enable_background_send') is True
                         and ((sys.platform == 'darwin' and source is not None)
                              or (sys.platform == 'win32' and source is None
                                  and selection.get('dedicated_browser_channel') is not None)))
                     else 'http-read-only')
    if sys.platform == "win32" and transport != "http-read-only":
        if (transport != "browser-prepared-httpx"
                or selection.get("dedicated_browser_channel") is None
                or selection.get("enable_background_send") is not True
                or not selection.get("expected_account_id")):
            raise ValueError("Windows Subchat sending requires an explicitly enabled "
                             "dedicated browser selection")
    if transport == "http-read-only":
        source, account_id = selected_chrome_login(state)
        asyncio.run(run(None, state, mcp=True, http_only=True,
                        chrome_login_profile=profile if source is None else None,
                        chrome_login_source_profile=source, expected_account_id=account_id,
                        read_only_mcp=True, browser_channel=browser_channel))
    elif transport == "browser-send":
        if _selection_record(state).get("dedicated_browser_channel") is not None:
            raise ValueError("Browser-send override conflicts with the dedicated selection")
        browser = browser_send_profile(profile, state)
        asyncio.run(run(browser, state, mcp=True, http_read=True, minimized=True,
                        browser_channel=browser_channel))
    elif transport == "browser-prepared-httpx":
        source, account_id = selected_chrome_login(state)
        if sys.platform == "win32" and (source is not None or account_id is None):
            raise ValueError("Windows browser-prepared sending requires a dedicated profile "
                             "and a selected account ID")
        browser = browser_send_profile(profile, state)
        asyncio.run(run(browser, state, mcp=True, http_read=True, minimized=True,
                        httpx_generation=True, browser_source_profile=source,
                        expected_account_id=account_id, browser_channel=browser_channel))
    else:
        raise ValueError("ANYWHERE_SUBCHAT_PLUGIN_TRANSPORT must be http-read-only "
                         "or browser-send or browser-prepared-httpx")
