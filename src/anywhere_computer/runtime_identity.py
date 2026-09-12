"""Identify the exact installed Python implementation independently of release labels."""

import hashlib
from pathlib import Path

ENGINE_API_VERSION = 1


def runtime_identity() -> str:
    digest = hashlib.sha256()
    for source in sorted(Path(__file__).parent.glob("*.py")):
        digest.update(source.name.encode())
        digest.update(b"\0")
        digest.update(source.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()
