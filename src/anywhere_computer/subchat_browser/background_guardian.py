"""Stop one owned background Chrome instance if its Python owner disappears."""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from .background import _stop_profile_processes


def run(profile: Path, token: str) -> int:
    if sys.platform != 'darwin' or not profile.is_absolute() or not re.fullmatch(
        r'[0-9a-f]{32}', token
    ):
        return 2
    os.write(1, b'R')
    if os.read(0, 1) == b'D':
        return 0
    try:
        _stop_profile_processes(profile, token, startup_grace=True)
    except (OSError, RuntimeError):
        return 1
    return 0


if __name__ == '__main__':
    if len(sys.argv) != 3:
        raise SystemExit(2)
    raise SystemExit(run(Path(sys.argv[1]), sys.argv[2]))
