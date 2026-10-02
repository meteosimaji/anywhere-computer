"""Host-only selection of a trusted, user-installed Linux Google Chrome binary."""

import hashlib
import os
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from .locking import ProcessLock
from .state import prepare_directory

_CONFIG = "browser.json"


class ChromeSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    executable: str
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    version: str = Field(pattern=r"^Google Chrome [0-9]+(?:\.[0-9]+){3}$")


def _linux() -> None:
    if sys.platform != "linux":
        raise ValueError("Explicit Chrome executable selection currently requires Linux")


def chrome_binary(value: str) -> Path:
    """Check metadata only; this is not a publisher signature or a launch test."""
    _linux()
    path = Path(value)
    if not path.is_absolute() or any(ord(char) < 32 for char in value):
        raise ValueError("Chrome executable must be an absolute path without control characters")
    path = path.resolve(strict=True)
    info = path.stat()
    if (not stat.S_ISREG(info.st_mode) or not 4 <= info.st_size <= 1024 * 1024 * 1024
            or not os.access(path, os.X_OK) or info.st_uid not in {0, os.getuid()}
            or info.st_mode & (0o022 | stat.S_ISUID | stat.S_ISGID)):
        raise ValueError("Chrome must be an executable file owned by this user or root, "
                         "without group/world write permission or setuid/setgid bits")
    with path.open("rb") as source:
        if source.read(4) != b"\x7fELF":
            raise ValueError("Select Chrome's ELF binary, not a launcher script")
    return path


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            size += len(block)
            if size > 1024 * 1024 * 1024:
                raise ValueError("Chrome binary exceeds the validation size limit")
            digest.update(block)
    return digest.hexdigest()


def _load(directory: Path) -> ChromeSelection | None:
    path = directory / _CONFIG
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if (not stat.S_ISREG(info.st_mode) or info.st_size > 8192
            or info.st_uid != os.getuid() or info.st_mode & 0o077):
        raise ValueError("Chrome selection must be a private, owner-held regular file")
    try:
        return ChromeSelection.model_validate_json(path.read_bytes())
    except ValueError:
        raise ValueError("Invalid Chrome selection; rerun browser-configure locally") from None


def selected_chrome(directory: Path | None) -> Path | None:
    if sys.platform != "linux" or directory is None:
        return None
    selection = _load(directory)
    if selection is None:
        return None
    path = chrome_binary(selection.executable)
    if _digest(path) != selection.sha256:
        raise ValueError("Selected Chrome changed; rerun browser-configure locally")
    return path


def browser_configuration(directory: Path | None) -> dict[str, JsonValue]:
    """No process launch or full binary hash during a status/doctor observation."""
    report: dict[str, JsonValue] = {
        "scope": "isolated_browser_only", "selection": "platform_default",
        "channel": "msedge" if sys.platform == "win32" else "chrome",
        "executable": None, "launch_verified": False,
        "publisher_verified": False, "binary_digest_verified": False,
    }
    if sys.platform != "linux" or directory is None:
        return report
    try:
        selection = _load(directory)
        if selection is not None:
            report.update(selection="configured", channel=None,
                          executable=str(chrome_binary(selection.executable)),
                          version_at_configuration=selection.version,
                          next_action="The saved binary hash is checked before each launch.")
    except (OSError, ValueError):
        report.update(selection="invalid", channel=None,
                      next_action="Rerun browser-configure with the trusted Chrome ELF binary, "
                                  "or --clear-chrome-executable. No fallback will be used.")
    return report


def configure_browser(directory: Path, *, executable: str | None = None,
                      clear: bool = False) -> dict[str, JsonValue]:
    """Explicit local setup executes only the selected binary's --version command."""
    _linux()
    if executable is None and not clear:
        return browser_configuration(directory)
    if executable is not None and clear:
        raise ValueError("Select or clear a Chrome executable, not both")
    selection = None
    if executable is not None:
        path = chrome_binary(executable)
        before = _digest(path)
        try:
            result = subprocess.run([str(path), "--version"], capture_output=True,
                                    timeout=5, check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise ValueError("Selected Chrome version check failed; selection was not saved") \
                from None
        observed = result.stdout.decode("utf-8", errors="replace").strip()
        if result.returncode != 0 or not re.fullmatch(
            r"Google Chrome [0-9]+(?:\.[0-9]+){3}", observed,
        ):
            raise ValueError("Selected executable did not report a Google Chrome version; "
                             "selection was not saved")
        if _digest(chrome_binary(str(path))) != before:
            raise ValueError("Selected Chrome changed during validation; selection was not saved")
        selection = ChromeSelection(executable=str(path), sha256=before, version=observed)
    prepare_directory(directory)
    with ProcessLock(directory / "browser-configuration.lock"):
        target = directory / _CONFIG
        if selection is None:
            target.unlink(missing_ok=True)
        else:
            temporary: str | None = None
            try:
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory,
                                                 prefix=".browser-", delete=False) as output:
                    temporary = output.name
                    output.write(selection.model_dump_json())
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, target)
            finally:
                if temporary is not None:
                    Path(temporary).unlink(missing_ok=True)
    return {**browser_configuration(directory), "changed": True,
            "next_action": "Applies to the next isolated browser_open; existing sessions "
                           "and ordinary Chrome profiles are unchanged."}
