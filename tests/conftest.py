"""Keep pytest's pre-created Windows test directories compatible with state ACL checks."""

import os
from pathlib import Path

import pytest

from anywhere_computer.private_directory import migrate_default_windows_state


@pytest.fixture
def tmp_path(tmp_path: Path) -> Path:
    """Secure a fresh pytest directory before tests use it as application state."""
    if os.name == "nt":
        migrate_default_windows_state(root=tmp_path, apply=True)
    return tmp_path
