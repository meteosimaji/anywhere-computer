"""Locate renderer programs in a portable macOS runtime or on a development host."""

import os
import shutil
import sys
from pathlib import Path

_BUNDLED = {
    "soffice": Path("LibreOffice.app/Contents/MacOS/soffice"),
    "pdfinfo": Path("bin/pdfinfo"),
    "pdftoppm": Path("bin/pdftoppm"),
}


def find_renderer(name: str) -> str | None:
    """Use a complete co-located bundle, never mix a partial one with host tools."""
    executable = Path(sys.executable).resolve()
    runtime = executable.parent.parent if executable.parent.name == "bin" else executable.parent
    if runtime.name == "runtime" and name in _BUNDLED:
        bundle = runtime.parent / "renderers" / "macos"
        if bundle.exists() or bundle.is_symlink():
            candidate = bundle / _BUNDLED[name]
            if (candidate.is_file() and not candidate.is_symlink()
                    and os.access(candidate, os.X_OK)):
                return str(candidate)
            return None
    return shutil.which(name)
