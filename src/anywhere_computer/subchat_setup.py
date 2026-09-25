"""Inspect, select, or stage a macOS Chrome login for Subchat."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import tempfile
from contextlib import AsyncExitStack, ExitStack
from pathlib import Path

import httpx

from .http_service import load_http_config
from .locking import ProcessLock
from .subchat import SubchatAccessError
from .subchat_browser.background import background_chrome_context, new_background_page
from .subchat_chrome_profile import _snapshot_profile, temporary_chrome_profile
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
    if not source.is_dir() or source == state or source in state.parents or state in source.parents:
        raise SetupInputError("Selected Chrome profile is unavailable or overlaps the ledger")
    return source


def _private_directory(path: Path) -> None:
    if sys.platform != "darwin":
        raise SetupInputError("Chrome profile staging is supported on macOS only")
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise SetupInputError("Subchat profile staging directory must not be a symlink")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir() or path.stat().st_uid != os.getuid():
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


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("inspect", "select", "stage"))
    parser.add_argument("profile", type=Path, help="Absolute Chrome Default or Profile N path")
    parser.add_argument("--enable-background-send", action="store_true",
                        help="Explicitly enable browser-prepared sending after selection")
    parser.add_argument("--expect-account-id",
                        help="Account ID observed with inspect; required for select and stage")
    parser.add_argument("--http-state-dir", type=Path,
                        help="For stage, update an existing stopped HTTPS service")
    args = parser.parse_args(argv)
    if args.action != "select" and args.enable_background_send:
        parser.error("--enable-background-send requires select")
    if args.action in {"select", "stage"} and not args.expect_account_id:
        parser.error(f"{args.action} requires --expect-account-id from a prior inspect")
    if args.action == "inspect" and args.expect_account_id is not None:
        parser.error("--expect-account-id requires select or stage")
    if args.http_state_dir is not None and args.action != "stage":
        parser.error("--http-state-dir requires stage")
    try:
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
        source = _source_profile(args.profile, state)
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
        if args.http_state_dir is not None:
            background_send_enabled = None
        elif args.action == "stage":
            background_send_enabled = record.get("enable_background_send") is True
        else:
            background_send_enabled = args.action == "select" and args.enable_background_send
        print(json.dumps({"account_id": args.expect_account_id if args.action == "stage"
                          else account_id,
                          "selected": args.action in {"select", "stage"},
                          "staged": args.action == "stage",
                          "background_send_enabled": background_send_enabled,
                          "restart_required": args.action != "inspect"}))
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
