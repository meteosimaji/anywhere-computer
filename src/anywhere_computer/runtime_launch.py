"""Internal interpreters resolve installed code, never workspace Python modules."""

import os
import sys
from typing import Literal

InternalModule = Literal[
    "anywhere_computer", "anywhere_computer.cli", "anywhere_computer.regex_worker"
]


def python_module_command(
    module: InternalModule, *arguments: str, executable: str | None = None,
) -> list[str]:
    # -I excludes cwd, PYTHON* settings and user-site packages. The package must
    # be installed in this interpreter/venv; never fall back to workspace imports.
    # Keep venv symlinks unresolved so the child retains the selected environment.
    # Isolated children ignore PYTHONUTF8 and PYTHONDONTWRITEBYTECODE. Keep the
    # portable runtime byte-for-byte verifiable after its own child processes run.
    return [executable or os.path.abspath(sys.executable),
            "-B", "-I", "-X", "utf8", "-m", module, *arguments]
