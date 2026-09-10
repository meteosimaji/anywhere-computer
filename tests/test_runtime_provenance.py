"""Subprocess checks must exercise this worktree, not another editable install."""

import hashlib
import subprocess
import sys
from pathlib import Path

from anywhere_computer.runtime_identity import runtime_identity

ROOT = Path(__file__).resolve().parents[1]


def test_isolated_subprocess_matches_the_current_worktree():
    expected = hashlib.sha256()
    for source in sorted((ROOT / "src/anywhere_computer").glob("*.py")):
        expected.update(source.name.encode())
        expected.update(b"\0")
        expected.update(source.read_bytes())
        expected.update(b"\0")
    completed = subprocess.run(
        [sys.executable, "-I", "-c",
         "from anywhere_computer.runtime_identity import runtime_identity; "
         "print(runtime_identity())"],
        capture_output=True, text=True, check=True, timeout=15,
    )
    assert runtime_identity() == completed.stdout.strip() == expected.hexdigest(), (
        "The isolated Python subprocess does not use this worktree's implementation. "
        "Run uv sync in this worktree and use its .venv/bin/python; PYTHONPATH alone "
        "does not select the implementation for isolated or SDK-spawned processes."
    )
