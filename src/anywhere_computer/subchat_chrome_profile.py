"""Read a selected macOS Chrome login without opening its live profile."""

from __future__ import annotations

import asyncio
import os
import shutil
import sqlite3
import sys
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, closing
from pathlib import Path
from urllib.parse import quote

from .subchat import SubchatAccessError


def chrome_user_data_root() -> Path:
    """The ordinary macOS Chrome profile store, never a caller-supplied directory."""
    return Path.home() / "Library/Application Support/Google/Chrome"


def chrome_profile_by_id(profile_id: str) -> Path:
    """Resolve a discovered profile ID without accepting a filesystem path."""
    if profile_id != "Default" and not (
        profile_id.startswith("Profile ") and profile_id[8:].isdigit()
    ):
        raise ValueError("Select Chrome Default or Profile N")
    root = chrome_user_data_root()
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Chrome profile store is unavailable")
    source = root / profile_id
    if source.is_symlink() or not source.is_dir():
        raise ValueError("Selected Chrome profile is unavailable")
    getuid = getattr(os, "getuid", None)
    if getuid is not None and source.stat().st_uid != getuid():
        raise ValueError("Selected Chrome profile belongs to another user")
    return source


def selected_chrome_source(source: Path, *, stage_root: Path | None = None) -> Path:
    """Accept only this user's Chrome store or a managed private snapshot."""
    if not source.is_absolute() or source.is_symlink() or source.parent.is_symlink():
        raise ValueError("Selected Chrome profile path is invalid")
    if source.parent == chrome_user_data_root():
        return chrome_profile_by_id(source.name)
    if (stage_root is None or source.parent.parent != stage_root
            or not source.parent.name.startswith("snapshot-")
            or source.parent.is_symlink() or stage_root.is_symlink()
            or not source.is_dir()):
        raise ValueError("Selected Chrome profile is outside the managed store")
    if source.name != "Default" and not (
        source.name.startswith("Profile ") and source.name[8:].isdigit()
    ):
        raise ValueError("Selected Chrome profile ID is invalid")
    getuid = getattr(os, "getuid", None)
    if getuid is not None and source.stat().st_uid != getuid():
        raise ValueError("Selected Chrome profile belongs to another user")
    return source


def _snapshot_profile(source: Path, destination: Path) -> None:
    if source.name != "Default" and not (
        source.name.startswith("Profile ") and source.name[8:].isdigit()
    ):
        raise ValueError("Select a Chrome profile directory such as Default or Profile 1")
    root = source.parent
    local_state = root / "Local State"
    cookie_database = source / "Network/Cookies"
    if (source.is_symlink() or root.is_symlink() or local_state.is_symlink()
            or (source / "Network").is_symlink()
            or cookie_database.is_symlink()):
        raise ValueError("Selected Chrome profile contains a symbolic link")
    if not cookie_database.is_file():
        cookie_database = source / "Cookies"
    if cookie_database.is_symlink():
        raise ValueError("Selected Chrome profile contains a symbolic link")
    if not local_state.is_file() or not cookie_database.is_file():
        raise ValueError("Selected Chrome profile has no Local State or Cookie database")

    target_profile = destination / source.name
    target_profile.mkdir(mode=0o700)
    for source_file, target in (
        (local_state, destination / "Local State"),
        (source / "Preferences", target_profile / "Preferences"),
        (source / "Secure Preferences", target_profile / "Secure Preferences"),
    ):
        if source_file.is_symlink():
            raise ValueError("Selected Chrome profile contains a symbolic link")
        if source_file.is_file():
            shutil.copyfile(source_file, target)
            target.chmod(0o600)

    target_database = target_profile / cookie_database.relative_to(source)
    target_database.parent.mkdir(mode=0o700, exist_ok=True)
    uri = f"file:{quote(str(cookie_database))}?mode=ro"
    try:
        with closing(sqlite3.connect(uri, uri=True, timeout=5)) as source_connection:
            with closing(sqlite3.connect(target_database)) as snapshot_connection:
                source_connection.backup(snapshot_connection)
        with closing(sqlite3.connect(target_database)) as snapshot_connection:
            snapshot_connection.execute("DELETE FROM cookies WHERE host_key NOT IN (?, ?)",
                             ("chatgpt.com", ".chatgpt.com"))
            remaining = snapshot_connection.execute(
                "SELECT COUNT(*) FROM cookies").fetchone()[0]
            snapshot_connection.commit()
            snapshot_connection.execute("VACUUM")
    except sqlite3.Error as error:
        raise ValueError("Selected Chrome Cookie database cannot be read") from error
    target_database.chmod(0o600)
    if remaining == 0:
        raise SubchatAccessError(401)


@asynccontextmanager
async def temporary_chrome_profile(source: Path) -> AsyncIterator[Path]:
    """Yield a private, disposable user-data root; never mutate the source profile."""
    if sys.platform != "darwin":
        raise ValueError("Existing Chrome profile snapshot is verified on macOS only")
    with tempfile.TemporaryDirectory(prefix="anywhere-subchat-chrome-") as temporary:
        destination = Path(temporary)
        destination.chmod(0o700)
        await asyncio.to_thread(_snapshot_profile, source, destination)
        yield destination
