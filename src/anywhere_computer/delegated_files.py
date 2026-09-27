"""Handle-confined file operations for locally delegated child tasks.

POSIX opens every component relative to a directory descriptor. Windows pins
the parent ancestry and permits reads and create-only writes. Both reject
symlinks, including in the configured root and its ancestors.
"""

import hashlib
import os
import secrets
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from pydantic import JsonValue

from .files import MAX_READ_BYTES, sha256
from .models import ReadFile, WriteFile

_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


def _require_flags() -> None:
    if os.name == "nt" or not _O_NOFOLLOW or not _O_DIRECTORY:
        raise ValueError("Descriptor-confined delegated files are unavailable on this host")


def _components(path: str) -> tuple[str, ...]:
    candidate = Path(path)
    if (not candidate.is_absolute() or any(part in {"..", ".", ""}
                                           for part in path.split("/")[1:])):
        raise ValueError("Delegated path must be a canonical absolute path")
    return tuple(part for part in candidate.parts if part != "/")


@contextmanager
def _directory(path: str) -> Iterator[int]:
    _require_flags()
    current = os.open("/", os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW)
    try:
        for component in _components(path):
            following = os.open(component, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW,
                                dir_fd=current)
            os.close(current)
            current = following
        yield current
    finally:
        os.close(current)


@contextmanager
def _parent(path: str, roots: tuple[str, ...]) -> Iterator[tuple[int, str]]:
    parts = _components(path)
    root_parts = sorted((_components(root) for root in roots), key=len, reverse=True)
    selected = next((root for root in root_parts
                     if len(parts) > len(root) and parts[:len(root)] == root), None)
    if selected is None:
        raise ValueError("Delegated path is outside the permitted roots")
    root = "/" + "/".join(selected)
    with _directory(root) as root_fd:
        current = os.dup(root_fd)
        try:
            for component in parts[len(selected):-1]:
                following = os.open(component, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW,
                                    dir_fd=current)
                os.close(current)
                current = following
            yield current, parts[-1]
        finally:
            os.close(current)


def _read_at(parent: int, name: str) -> tuple[bytes, os.stat_result]:
    flags = os.O_RDONLY | _O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(name, flags, dir_fd=parent)
    with os.fdopen(descriptor, "rb") as source:
        metadata = os.fstat(source.fileno())
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("Only regular files can be read")
        content = source.read(MAX_READ_BYTES + 1)
    if len(content) > MAX_READ_BYTES:
        raise ValueError("File exceeds the 16 MiB read limit")
    return content, metadata


def read(args: ReadFile, roots: tuple[str, ...]) -> dict[str, JsonValue]:
    if os.name == "nt":
        from .delegated_win32 import read_content

        content = read_content(args.path, roots)
    else:
        with _parent(args.path, roots) as (parent, name):
            content, _ = _read_at(parent, name)
    lines = content.decode("utf-8").splitlines(keepends=True)
    start = max(0, len(lines) + args.offset) if args.offset < 0 else args.offset
    stop = min(len(lines), start + args.limit)
    return {"path": args.path, "text": "".join(lines[start:stop]), "offset": start,
            "next_offset": stop, "total_lines": len(lines), "sha256": sha256(content),
            "truncated": stop < len(lines)}


def write(args: WriteFile, roots: tuple[str, ...], backup_dir: Path) -> dict[str, JsonValue]:
    if os.name == "nt":
        from .delegated_win32 import create_file

        return create_file(args, roots)
    data = args.text.encode()
    with _parent(args.path, roots) as (parent, name):
        try:
            original, metadata = _read_at(parent, name)
            exists = True
        except FileNotFoundError:
            original, metadata, exists = b"", None, False
        if exists and args.mode == "create":
            raise FileExistsError("Target already exists; use a read hash for replacement")
        if exists and args.expected_sha256 != sha256(original):
            raise ValueError("File changed or expected_sha256 is missing; read it again")
        if not exists and args.expected_sha256 is not None:
            raise ValueError("Target disappeared after it was read")
        content = (original if args.mode == "append" else b"") + data
        if len(content) > MAX_READ_BYTES:
            raise ValueError("Result exceeds the file size limit")
        backup_id = None
        if exists:
            backup_id = hashlib.sha256(original).hexdigest()
            backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            backup = backup_dir / backup_id
            try:
                with backup.open("xb") as destination:
                    destination.write(original)
                    destination.flush()
                    os.fsync(destination.fileno())
            except FileExistsError:
                if backup.is_symlink() or backup.read_bytes() != original:
                    raise ValueError("Existing backup is invalid; no file was changed") from None
        temporary = ".anywhere-delegated-" + secrets.token_hex(16)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW
        descriptor = os.open(temporary, flags, 0o600, dir_fd=parent)
        try:
            with os.fdopen(descriptor, "wb") as destination:
                destination.write(content)
                destination.flush()
                os.fsync(destination.fileno())
                if metadata is not None:
                    fchmod = getattr(os, "fchmod", None)
                    if fchmod is None:
                        raise ValueError("Descriptor permissions are unavailable on this host")
                    fchmod(destination.fileno(), stat.S_IMODE(metadata.st_mode))
            if exists and metadata is not None:
                current, current_metadata = _read_at(parent, name)
                if (current != original or (current_metadata.st_dev, current_metadata.st_ino)
                        != (metadata.st_dev, metadata.st_ino)):
                    raise ValueError("Concurrent modification detected before replacement")
                os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
            else:
                os.link(temporary, name, src_dir_fd=parent, dst_dir_fd=parent,
                        follow_symlinks=False)
        finally:
            try:
                os.unlink(temporary, dir_fd=parent)
            except FileNotFoundError:
                pass
    return {"path": args.path, "sha256": sha256(content), "bytes": len(content),
            "backup_id": backup_id}
