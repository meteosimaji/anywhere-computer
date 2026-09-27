"""Confined Windows reads and create-only writes for delegated child tasks.

The existing upload handles pin every ancestor against rename and reject
reparse points. Replacement needs an atomic identity check that Win32's
path-based ``os.replace`` cannot provide, so it remains unavailable.
"""

from __future__ import annotations

import os
import re
import secrets
import stat
from pathlib import Path

from pydantic import JsonValue

from .files import MAX_READ_BYTES, sha256
from .models import WriteFile
from .upload_win32 import (
    file_identity_fd,
    open_regular_nofollow,
    pin_directory,
    pin_regular_handle_nofollow,
)

_DRIVE = re.compile(r"^[A-Za-z]:$")
_DEVICE_NAMES = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)),
                 *(f"lpt{i}" for i in range(1, 10))}


def _canonical_parts(path: str) -> tuple[str, ...]:
    """Reject Windows aliases, device paths, streams and noncanonical spelling."""
    if "/" in path or "\x00" in path:
        raise ValueError("Delegated path must be a canonical drive path")
    pieces = path.split("\\")
    if len(pieces) == 2 and _DRIVE.fullmatch(pieces[0]) and not pieces[1]:
        return (pieces[0].casefold(),)
    if (len(pieces) < 2 or _DRIVE.fullmatch(pieces[0]) is None
            or any(not part or part in {".", ".."} or ":" in part
                   or part.endswith((".", " "))
                   or part.split(".", 1)[0].casefold() in _DEVICE_NAMES
                   for part in pieces[1:])):
        raise ValueError("Delegated path must be a canonical drive path")
    return tuple(part.casefold() for part in pieces)


def _parent(path: str, roots: tuple[str, ...]) -> Path:
    parts = _canonical_parts(path)
    if not any(len(parts) > len(root_parts)
               and parts[:len(root_parts)] == root_parts
               for root_parts in (_canonical_parts(root) for root in roots)):
        raise ValueError("Delegated path is outside the permitted roots")
    return Path(path).parent


def read_content(path: str, roots: tuple[str, ...]) -> bytes:
    parent = _parent(path, roots)
    with pin_directory(parent):
        descriptor = open_regular_nofollow(Path(path))
        with os.fdopen(descriptor, "rb") as source:
            metadata = os.fstat(source.fileno())
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError("Only regular files can be read")
            content = source.read(MAX_READ_BYTES + 1)
    if len(content) > MAX_READ_BYTES:
        raise ValueError("File exceeds the 16 MiB read limit")
    return content


def create_file(args: WriteFile, roots: tuple[str, ...]) -> dict[str, JsonValue]:
    parent = _parent(args.path, roots)
    if args.mode != "create":
        raise ValueError("Delegated replacement and append are unavailable on Windows")
    if args.expected_sha256 is not None:
        raise ValueError("Target disappeared after it was read")
    content = args.text.encode()
    if len(content) > MAX_READ_BYTES:
        raise ValueError("Result exceeds the file size limit")
    target = Path(args.path)
    temporary = parent / (".anywhere-delegated-" + secrets.token_hex(16))
    with pin_directory(parent):
        if os.path.lexists(target):
            raise FileExistsError("Target already exists; use a read hash for replacement")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                             | getattr(os, "O_BINARY", 0), 0o600)
        try:
            with os.fdopen(descriptor, "wb") as destination:
                destination.write(content)
                destination.flush()
                os.fsync(destination.fileno())
                written_identity = file_identity_fd(destination.fileno())
                with pin_regular_handle_nofollow(temporary) as source:
                    if written_identity != source.identity:
                        raise ValueError("Delegated staging file changed")
                    source.link(target)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
    return {"path": args.path, "sha256": sha256(content), "bytes": len(content),
            "backup_id": None}
