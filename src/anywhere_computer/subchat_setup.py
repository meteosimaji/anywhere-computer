"""Inspect and select an existing macOS Chrome login for the Subchat Plugin."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
from contextlib import AsyncExitStack
from pathlib import Path

import httpx

from .subchat import SubchatAccessError
from .subchat_browser.background import background_chrome_context, new_background_page
from .subchat_chrome_profile import temporary_chrome_profile
from .subchat_plugin import plugin_paths


class SetupInputError(ValueError):
    """A fixed, local validation message safe to show to the operator."""


def _source_profile(value: Path, state: Path) -> Path:
    if not value.is_absolute():
        raise SetupInputError("Select an absolute Chrome profile path")
    source = value.resolve()
    if (source.name != "Default" and not (
            source.name.startswith("Profile ") and source.name[8:].isdigit())):
        raise SetupInputError("Select Chrome Default or Profile N")
    if not source.is_dir() or source == state or source in state.parents or state in source.parents:
        raise SetupInputError("Selected Chrome profile is unavailable or overlaps the ledger")
    return source


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
    parser.add_argument("action", choices=("inspect", "select"))
    parser.add_argument("profile", type=Path, help="Absolute Chrome Default or Profile N path")
    parser.add_argument("--enable-background-send", action="store_true",
                        help="Explicitly enable browser-prepared sending after selection")
    parser.add_argument("--expect-account-id",
                        help="Account ID observed with inspect; required for select")
    args = parser.parse_args(argv)
    if args.action != "select" and args.enable_background_send:
        parser.error("--enable-background-send requires select")
    if args.action == "select" and not args.expect_account_id:
        parser.error("select requires --expect-account-id from a prior inspect")
    if args.action == "inspect" and args.expect_account_id is not None:
        parser.error("--expect-account-id requires select")
    try:
        try:
            _, state = plugin_paths()
        except ValueError:
            raise SetupInputError(
                "Subchat setup paths are invalid; check the Plugin path settings"
            ) from None
        source = _source_profile(args.profile, state)
        account_id = asyncio.run(inspect_account(source))
        if args.expect_account_id is not None and account_id != args.expect_account_id:
            raise SetupInputError("Selected Chrome profile has a different Chat account")
        if args.action == "select":
            save_selection(state, source, account_id, enable_send=args.enable_background_send)
        print(json.dumps({"account_id": account_id,
                          "selected": args.action == "select",
                          "background_send_enabled": (
                              args.action == "select" and args.enable_background_send),
                          "restart_required": args.action == "select"}))
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
