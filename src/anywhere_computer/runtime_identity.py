"""Identify the exact installed implementation independently of release labels."""

import hashlib
from pathlib import Path

ENGINE_API_VERSION = 1
REMOTE_RECEIPT_METADATA = "remote_receipt_metadata"
ENGINE_PROTOCOL_FEATURES = (REMOTE_RECEIPT_METADATA,)


def runtime_identity() -> str:
    digest = hashlib.sha256()
    root = Path(__file__).parent
    sources = [source for source in root.rglob("*")
               if source.is_file() and source.suffix in {".py", ".js"}]
    for source in sorted(sources):
        digest.update(source.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(source.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()
