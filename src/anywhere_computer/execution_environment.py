"""Find standard user-installed tools without running shell startup files."""

import os
import sys
from collections.abc import Mapping
from pathlib import Path


def _standard_bins() -> tuple[Path, ...]:
    home = Path.home()
    if sys.platform == "win32":
        roaming = Path(os.environ.get("APPDATA", str(home / "AppData/Roaming")))
        return (home / ".local/bin", roaming / "npm")
    if sys.platform == "darwin":
        return (Path("/opt/homebrew/bin"), Path("/usr/local/bin"), home / ".local/bin")
    return (home / ".local/bin",)


def with_tool_path(environment: Mapping[str, str]) -> dict[str, str]:
    """Copy the caller's environment, appending only existing standard bin directories.

    Preserve configured command precedence. In particular, an MCP caller passing a
    minimal environment does not inherit additional credentials from os.environ.
    Recompute on every spawn so installations after daemon startup are visible.
    """
    result = dict(environment)
    windows = sys.platform == "win32"
    separator = ";" if windows else ":"
    keys = [key for key in result if key.upper() == "PATH"] if windows else ["PATH"]
    key = keys[0] if keys else "PATH"
    existing = result.get(key, "")
    entries = existing.split(separator) if existing else []
    seen = {entry.casefold() if windows else entry for entry in entries}
    for directory in _standard_bins():
        if not directory.is_absolute() or not directory.is_dir():
            continue
        # Never introduce the working directory as a new executable search location.
        if directory.resolve() == Path.cwd().resolve():
            continue
        value = str(directory)
        identity = value.casefold() if windows else value
        if identity not in seen:
            entries.append(value)
            seen.add(identity)
    for duplicate in keys[1:]:
        result.pop(duplicate, None)
    if windows:
        result.pop(key, None)
        key = "PATH"
    result[key] = separator.join(entries)
    return result
