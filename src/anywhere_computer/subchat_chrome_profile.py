"""Read a selected macOS Chrome login without opening its live profile."""

from __future__ import annotations

import asyncio
import shutil
import sqlite3
import sys
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, closing
from pathlib import Path
from urllib.parse import quote

from .subchat import SubchatAccessError


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
        await asyncio.to_thread(_snapshot_profile, source.resolve(), destination)
        yield destination
