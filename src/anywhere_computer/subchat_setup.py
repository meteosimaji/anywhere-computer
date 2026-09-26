"""Prepare a dedicated Windows browser or select a macOS Chrome login for Subchat."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
from contextlib import AsyncExitStack, ExitStack
from pathlib import Path

import httpx

from .http_service import load_http_config
from .locking import ProcessLock
from .subchat import SubchatAccessError
from .subchat_browser.background import background_chrome_context, new_background_page
from .subchat_chrome_profile import (
    _snapshot_profile,
    chrome_profile_by_id,
    chrome_user_data_root,
    temporary_chrome_profile,
)
from .subchat_plugin import _selection_record, plugin_paths


class SetupInputError(ValueError):
    """A fixed, local validation message safe to show to the operator."""


def _source_profile(value: Path, state: Path) -> Path:
    if not value.is_absolute():
        raise SetupInputError("Select an absolute Chrome profile path")
    source = value.resolve()
    if source != value or value.is_symlink() or value.parent.is_symlink():
        raise SetupInputError("Selected Chrome profile must not use symbolic links")
    if (source.name != "Default" and not (
            source.name.startswith("Profile ") and source.name[8:].isdigit())):
        raise SetupInputError("Select Chrome Default or Profile N")
    try:
        selected = chrome_profile_by_id(source.name)
    except ValueError:
        raise SetupInputError(
            "Selected Chrome profile is outside the standard Chrome store") from None
    if source != selected:
        raise SetupInputError("Selected Chrome profile is outside the standard Chrome store")
    if not source.is_dir() or source == state or source in state.parents or state in source.parents:
        raise SetupInputError("Selected Chrome profile is unavailable or overlaps the ledger")
    return source


def _private_directory(path: Path) -> None:
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise SetupInputError("Subchat profile staging directory must not be a symlink")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    getuid = getattr(os, "getuid", None)
    if (path.is_symlink() or not path.is_dir()
            or (getuid is not None and path.stat().st_uid != getuid())):
        raise SetupInputError("Subchat profile staging directory is unavailable")
    path.chmod(0o700)


async def stage_profile(source: Path, stage_root: Path, account_id: str) -> Path:
    """Publish an account-verified, filtered snapshot under app-owned state."""
    if stage_root == source or stage_root in source.parents or source in stage_root.parents:
        raise SetupInputError("Chrome source and staging directory must be separate")
    _private_directory(stage_root)
    temporary = Path(tempfile.mkdtemp(prefix=".staging-", dir=stage_root))
    published: Path | None = None
    try:
        await asyncio.to_thread(_snapshot_profile, source, temporary)
        if await inspect_account(temporary / source.name) != account_id:
            raise SetupInputError("Staged Chrome profile has a different Chat account")
        published = stage_root / ("snapshot-" + temporary.name.removeprefix(".staging-"))
        temporary.rename(published)
        return published / source.name
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def _save_http_selection(directory: Path, source: Path, account_id: str) -> None:
    config = load_http_config(directory)
    if config.subchat is None:
        raise SetupInputError("HTTP service has no Subchat selection")
    if config.subchat.account_id != account_id:
        raise SetupInputError("HTTP Subchat account pin differs from the selected account")
    updated_subchat = config.subchat.model_copy(update={"profile": str(source)})
    updated = type(config).model_validate_json(config.model_copy(
        update={"subchat": updated_subchat}).model_dump_json())
    destination = directory / "http-server/config.json"
    if destination.is_symlink():
        raise SetupInputError("HTTP configuration must not be a symbolic link")
    descriptor, temporary = tempfile.mkstemp(prefix=".config-profile-", dir=destination.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(updated.model_dump_json(indent=2) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _remove_previous_managed_snapshot(previous: Path, stage_root: Path) -> None:
    """Remove only a replaced snapshot that this command could have created."""
    snapshot = previous.parent
    if (snapshot.parent == stage_root and snapshot.name.startswith("snapshot-")
            and not snapshot.is_symlink() and snapshot.is_dir()):
        shutil.rmtree(snapshot)


async def inspect_account(source: Path) -> str:
    """Read only the account ID; keep copied cookies and tokens in private memory."""
    if sys.platform != "darwin":
        raise SetupInputError("Chrome profile inspection is supported on macOS only")
    try:
        from playwright.async_api import async_playwright
    except ModuleNotFoundError as error:
        if error.name != "playwright":
            raise
        raise SetupInputError("Install browser support with the browser extra") from None

    from .subchat_chrome_login import chrome_http_session

    async with AsyncExitStack() as resources:
        playwright = await resources.enter_async_context(async_playwright())
        snapshot = await resources.enter_async_context(temporary_chrome_profile(source))
        context = await resources.enter_async_context(background_chrome_context(
            playwright, snapshot, [f"--profile-directory={source.name}"]))
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False,
                                     transport=httpx.AsyncHTTPTransport(retries=0)) as client:
            session = await chrome_http_session(
                context, client, page_factory=lambda: new_background_page(context))
        return session.account_id


async def inspect_dedicated_account(profile: Path, channel: str) -> str:
    """Read the account from a stopped, dedicated Windows browser profile."""
    if sys.platform != "win32":
        raise SetupInputError("Dedicated browser setup is supported on Windows only")
    from playwright.async_api import async_playwright

    from .subchat_browser import CHROME_PROFILE_IGNORED_DEFAULT_ARGS
    from .subchat_chrome_login import chrome_http_session

    async with async_playwright() as driver:
        context = await driver.chromium.launch_persistent_context(
            str(profile), channel=channel, headless=True,
            ignore_default_args=list(CHROME_PROFILE_IGNORED_DEFAULT_ARGS))
        try:
            async with httpx.AsyncClient(trust_env=False, follow_redirects=False,
                                         transport=httpx.AsyncHTTPTransport(retries=0)) as client:
                session = await chrome_http_session(context, client)
                return session.account_id
        finally:
            await context.close()


async def prepare_dedicated_profile(profile: Path, channel: str) -> str:
    """Use the installed normal browser for login, then verify its account."""
    if sys.platform != "win32" or channel not in {"chrome", "msedge"}:
        raise SetupInputError("Dedicated browser setup is supported on Windows only")
    profile.mkdir(mode=0o700, parents=True, exist_ok=True)
    executable = "msedge.exe" if channel == "msedge" else "chrome.exe"
    arguments = subprocess.list2cmdline(
        [f"--user-data-dir={profile}", "https://chatgpt.com/"])
    await asyncio.to_thread(os.startfile, executable, "open", arguments=arguments,
                            cwd=str(profile))
    await asyncio.to_thread(input,
        "Sign in to ChatGPT, close all dedicated browser windows, then press Enter here: ")
    return await inspect_dedicated_account(profile, channel)


def save_dedicated_selection(state: Path, profile: Path, channel: str, account_id: str,
                             *, enable_send: bool) -> None:
    """Persist only the verified Windows browser choice and send consent."""
    if sys.platform != "win32" or channel not in {"chrome", "msedge"}:
        raise SetupInputError("Select a Windows Chrome or Edge browser")
    if not account_id or len(account_id) > 256 or any(ord(char) < 33 or ord(char) > 126
                                                    for char in account_id):
        raise SetupInputError("Observed Chat account ID is invalid")
    _private_directory(state.parent)
    selection = state.parent / "login-selection.json"
    if selection.is_symlink():
        raise SetupInputError("Subchat login selection must not be a symlink")
    content = json.dumps({"dedicated_browser_channel": channel,
                          "dedicated_profile": str(profile.resolve()),
                          "expected_account_id": account_id,
                          "enable_background_send": enable_send}, separators=(",", ":"))
    descriptor, temporary = tempfile.mkstemp(prefix=".login-selection-", dir=state.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, selection)
    finally:
        Path(temporary).unlink(missing_ok=True)


def save_selection(state: Path, source: Path, account_id: str, *, enable_send: bool) -> None:
    """Replace only the local selection; never persist browser credentials."""
    if not account_id or len(account_id) > 256 or any(ord(char) < 33 or ord(char) > 126
                                                    for char in account_id):
        raise SetupInputError("Observed Chat account ID is invalid")
    state.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    selection = state.parent / "login-selection.json"
    if selection.is_symlink():
        raise SetupInputError("Subchat login selection must not be a symlink")
    content = json.dumps({"chrome_source_profile": str(source),
                          "expected_account_id": account_id,
                          "enable_background_send": enable_send}, separators=(",", ":"))
    descriptor, temporary = tempfile.mkstemp(prefix=".login-selection-", dir=state.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, selection)
    finally:
        Path(temporary).unlink(missing_ok=True)


def save_profile_id_selection(state: Path, profile_id: str, account_id: str,
                              *, enable_send: bool) -> None:
    """Save an owner-selected Chrome ID, never an arbitrary source path."""
    chrome_profile_by_id(profile_id)
    if not account_id or len(account_id) > 256 or any(ord(char) < 33 or ord(char) > 126
                                                    for char in account_id):
        raise SetupInputError("Observed Chat account ID is invalid")
    _private_directory(state.parent)
    selection = state.parent / "login-selection.json"
    if selection.is_symlink():
        raise SetupInputError("Subchat login selection must not be a symlink")
    content = json.dumps({"chrome_profile_id": profile_id,
                          "expected_account_id": account_id,
                          "enable_background_send": enable_send}, separators=(",", ":"))
    descriptor, temporary = tempfile.mkstemp(prefix=".login-selection-", dir=state.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, selection)
    finally:
        Path(temporary).unlink(missing_ok=True)


async def discover_profiles() -> list[dict[str, str]]:
    """Inspect at most 20 ordinary Chrome profiles without touching live Chrome."""
    if sys.platform != "darwin":
        raise SetupInputError("Chrome profile discovery is supported on macOS only")
    root = chrome_user_data_root()
    if root.is_symlink() or not root.is_dir():
        return []
    available = sorted(
        (entry.name for entry in root.iterdir()
         if (entry.name == "Default" or
             (entry.name.startswith("Profile ") and entry.name[8:].isdigit()))
         and entry.is_dir() and not entry.is_symlink()),
        key=lambda name: (name != "Default", int(name[8:]) if name != "Default" else 0),
    )
    if len(available) > 20:
        raise SetupInputError("Chrome has too many profiles to inspect")
    results = []
    for profile_id in available:
        try:
            account_id = await inspect_account(chrome_profile_by_id(profile_id))
            results.append({"profile_id": profile_id, "account_id": account_id,
                            "state": "available"})
        except SubchatAccessError:
            results.append({"profile_id": profile_id, "state": "login_required"})
        except Exception:
            results.append({"profile_id": profile_id, "state": "unavailable"})
    return results


def revoke_profile_id_selection(state: Path) -> None:
    """Remove the selected-profile authorization for the next Plugin start."""
    selection = state.parent / "login-selection.json"
    if selection.is_symlink():
        raise SetupInputError("Subchat login selection must not be a symlink")
    selection.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("inspect", "select", "stage", "discover",
                                           "choose", "revoke", "prepare-dedicated",
                                           "inspect-dedicated", "choose-dedicated"))
    parser.add_argument("profile", nargs="?",
                        help="Chrome profile ID for choose; path for legacy actions")
    parser.add_argument("--enable-background-send", action="store_true",
                        help="Explicitly enable browser-prepared sending after selection")
    parser.add_argument("--expect-account-id",
                        help="Account ID observed with inspect; required for select and stage")
    parser.add_argument("--http-state-dir", type=Path,
                        help="For stage, update an existing stopped HTTPS service")
    parser.add_argument("--browser-channel", choices=("chrome", "msedge"),
                        help="Windows dedicated browser: Chrome or Microsoft Edge")
    args = parser.parse_args(argv)
    if args.action not in {"select", "choose", "choose-dedicated"} and args.enable_background_send:
        parser.error("--enable-background-send requires a selection action")
    if (args.action in {"select", "stage", "choose", "choose-dedicated"}
            and not args.expect_account_id):
        parser.error(f"{args.action} requires --expect-account-id from a prior inspect")
    if args.action in {"inspect", "discover", "revoke"} and args.expect_account_id is not None:
        parser.error("--expect-account-id requires select, choose, or stage")
    if args.http_state_dir is not None and args.action != "stage":
        parser.error("--http-state-dir requires stage")
    dedicated = args.action in {"prepare-dedicated", "inspect-dedicated",
                                "choose-dedicated"}
    if dedicated != (args.browser_channel is not None):
        parser.error("Dedicated Windows actions require --browser-channel")
    if (args.action in {"discover", "revoke", "prepare-dedicated",
                        "inspect-dedicated", "choose-dedicated"}) != (args.profile is None):
        parser.error("This action takes no profile; other actions require one")
    try:
        if dedicated:
            profile, state = plugin_paths()
            account_id = asyncio.run(
                prepare_dedicated_profile(profile, args.browser_channel)
                if args.action == "prepare-dedicated" else
                inspect_dedicated_account(profile, args.browser_channel))
            if args.expect_account_id is not None and account_id != args.expect_account_id:
                raise SetupInputError("Dedicated browser has a different Chat account")
            if args.action == "choose-dedicated":
                _private_directory(state.parent)
                with ProcessLock(state.parent / "profile-stage.lock"):
                    save_dedicated_selection(state, profile, args.browser_channel, account_id,
                                             enable_send=args.enable_background_send)
            print(json.dumps({"account_id": account_id, "browser_channel":
                              args.browser_channel,
                              "selected": args.action == "choose-dedicated",
                              "send_enabled": args.action == "choose-dedicated"
                              and args.enable_background_send,
                              "restart_required": args.action == "choose-dedicated"}))
            return
        if args.action == "discover":
            print(json.dumps({"profiles": asyncio.run(discover_profiles())}))
            return
        if args.http_state_dir is None:
            try:
                _, state = plugin_paths()
            except ValueError:
                raise SetupInputError(
                    "Subchat setup paths are invalid; check the Plugin path settings"
                ) from None
        else:
            if not args.http_state_dir.is_absolute():
                raise SetupInputError("Select an absolute HTTP state directory")
            state = args.http_state_dir.resolve()
        if args.action == "revoke":
            _private_directory(state.parent)
            with ProcessLock(state.parent / "profile-stage.lock"):
                revoke_profile_id_selection(state)
            print(json.dumps({"selected": False, "restart_required": True}))
            return
        if args.action == "choose":
            try:
                source = chrome_profile_by_id(args.profile)
            except ValueError as error:
                raise SetupInputError(str(error)) from None
        else:
            source = _source_profile(Path(args.profile), state)
        with ExitStack() as locks:
            if args.action == "stage":
                if args.http_state_dir is None:
                    _private_directory(state.parent)
                    if (state.parent / "profile-stage.lock").is_symlink():
                        raise SetupInputError("Subchat staging lock must not be a symlink")
                    locks.enter_context(ProcessLock(state.parent / "profile-stage.lock"))
                    if (state.parent / "login-selection.json").is_symlink():
                        raise SetupInputError("Subchat login selection must not be a symlink")
                    record = _selection_record(state)
                    pinned = record.get("expected_account_id")
                    if pinned is not None and pinned != args.expect_account_id:
                        raise SetupInputError(
                            "Plugin Subchat account pin differs from the selected account")
                    stage_root = state.parent / "staged-chrome-profiles"
                else:
                    if args.http_state_dir.is_symlink():
                        raise SetupInputError("HTTP state directory must not be a symlink")
                    if ((state / "http-watch.lock").is_symlink()
                            or (state / "http-server.lock").is_symlink()):
                        raise SetupInputError("HTTP service lock must not be a symlink")
                    locks.enter_context(ProcessLock(state / "http-watch.lock"))
                    locks.enter_context(ProcessLock(state / "http-server.lock"))
                    config = load_http_config(state)
                    if (config.subchat is None
                            or config.subchat.account_id != args.expect_account_id):
                        raise SetupInputError(
                            "HTTP Subchat account pin differs from the selected account")
                    stage_root = state / "http-server/staged-chrome-profiles"
                    previous_http_profile = Path(config.subchat.profile)
            if args.action == "stage":
                staged = asyncio.run(stage_profile(source, stage_root,
                                                   args.expect_account_id))
                try:
                    if args.http_state_dir is None:
                        save_selection(state, staged, args.expect_account_id,
                                       enable_send=record.get("enable_background_send") is True)
                    else:
                        _save_http_selection(state, staged, args.expect_account_id)
                except BaseException:
                    shutil.rmtree(staged.parent)
                    raise
                if args.http_state_dir is not None:
                    _remove_previous_managed_snapshot(previous_http_profile, stage_root)
            else:
                account_id = asyncio.run(inspect_account(source))
                if (args.expect_account_id is not None
                        and account_id != args.expect_account_id):
                    raise SetupInputError("Selected Chrome profile has a different Chat account")
                if args.action == "select":
                    save_selection(state, source, account_id,
                                   enable_send=args.enable_background_send)
                elif args.action == "choose":
                    _private_directory(state.parent)
                    with ProcessLock(state.parent / "profile-stage.lock"):
                        save_profile_id_selection(state, args.profile, account_id,
                                                  enable_send=args.enable_background_send)
        if args.http_state_dir is not None:
            background_send_enabled = None
        elif args.action == "stage":
            background_send_enabled = record.get("enable_background_send") is True
        else:
            background_send_enabled = (args.action in {"select", "choose"}
                                       and args.enable_background_send)
        print(json.dumps({"account_id": args.expect_account_id if args.action == "stage"
                          else account_id,
                          "selected": args.action in {"select", "choose", "stage"},
                          "staged": args.action == "stage",
                          "background_send_enabled": background_send_enabled,
                          "restart_required": args.action not in {"inspect", "discover"}}))
    except SubchatAccessError as error:
        reason = ("Login is missing or expired" if error.status == 401
                  else "Chat account access was denied")
        raise SystemExit(f"Subchat login inspection failed: {reason}") from None
    except SetupInputError as error:
        raise SystemExit(str(error)) from None
    except Exception as error:
        # Browser and provider exceptions may carry paths, URLs, or credentials.
        raise SystemExit(f"Subchat login inspection failed: {type(error).__name__}") from None


if __name__ == "__main__":
    main()
