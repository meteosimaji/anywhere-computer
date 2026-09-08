"""Bounded file reads and conflict-aware atomic edits, implemented from scratch."""

import base64
import binascii
import hashlib
import os
import stat
import tempfile
from pathlib import Path
from typing import Literal

from pydantic import JsonValue

from .locking import ProcessLock
from .models import (
    EditFile,
    ListDirectory,
    MoveFile,
    ReadBinary,
    ReadFile,
    RestoreFile,
    WriteBinary,
    WriteFile,
)

MAX_READ_BYTES = 16 * 1024 * 1024
MAX_BINARY_READ_BYTES = 1024 * 1024 * 1024


def absolute_path(value: str) -> Path:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        raise ValueError("Use an absolute path to identify the target unambiguously")
    return candidate


def read_bytes(path: Path) -> bytes:
    # O_NONBLOCK lets fstat reject FIFOs without waiting for a writer to connect.
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as source:
        metadata = os.fstat(source.fileno())
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("Only regular files can be read")
        content = source.read(MAX_READ_BYTES + 1)
    if len(content) > MAX_READ_BYTES:
        raise ValueError("File exceeds the 16 MiB read limit")
    return content


def sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def describe_metadata(metadata: os.stat_result) -> dict[str, JsonValue]:
    kinds = ((stat.S_ISREG, "file"), (stat.S_ISDIR, "directory"),
             (stat.S_ISLNK, "symlink"), (stat.S_ISFIFO, "fifo"),
             (stat.S_ISSOCK, "socket"), (stat.S_ISCHR, "character_device"),
             (stat.S_ISBLK, "block_device"))
    return {
        "kind": next((name for check, name in kinds if check(metadata.st_mode)), "unknown"),
        "size": metadata.st_size,
        "modified": metadata.st_mtime,
        "accessed": metadata.st_atime,
        "status_changed": metadata.st_ctime if os.name != "nt" else None,
        "created": getattr(metadata, "st_birthtime", None),
        "permissions": stat.filemode(metadata.st_mode),
        "mode_octal": oct(stat.S_IMODE(metadata.st_mode)),
        "permissions_scope": "mode_bits_not_effective_access_or_acl",
        "directory": stat.S_ISDIR(metadata.st_mode),
    }


def inspect_file(path_value: str) -> dict[str, JsonValue]:
    path = absolute_path(path_value)
    link = path.lstat()
    is_link = stat.S_ISLNK(link.st_mode)
    result: dict[str, JsonValue] = {
        "path": str(path), "symlink": is_link,
        "entry": describe_metadata(link),
    }
    if not is_link:
        return {**result, **describe_metadata(link), "metadata_subject": "entry"}
    result["link_target"] = os.readlink(path)
    try:
        target = path.stat()
    except FileNotFoundError:
        return {**result, "target_state": "missing", "metadata_subject": "target",
                "size": None, "modified": None, "directory": False}
    except OSError:
        return {**result, "target_state": "unavailable", "metadata_subject": "target",
                "size": None, "modified": None, "directory": False}
    return {**result, **describe_metadata(target), "target_state": "resolved",
            "metadata_subject": "target"}


class Files:
    def __init__(self, state: Path, *, locks: Path | None = None) -> None:
        self.locks = locks if locks is not None else state / "file-locks"
        self.locks.mkdir(exist_ok=True, mode=0o700)
        self.backups = state / "backups"
        self.backups.mkdir(exist_ok=True, mode=0o700)

    def read(self, args: ReadFile) -> dict[str, JsonValue]:
        path = absolute_path(args.path)
        content = read_bytes(path)
        lines = content.decode("utf-8").splitlines(keepends=True)
        start = max(0, len(lines) + args.offset) if args.offset < 0 else args.offset
        stop = min(len(lines), start + args.limit)
        return {
            "path": str(path),
            "text": "".join(lines[start:stop]),
            "offset": start,
            "next_offset": stop,
            "total_lines": len(lines),
            "sha256": sha256(content),
            "truncated": stop < len(lines),
        }

    def write(self, args: WriteFile) -> dict[str, JsonValue]:
        return self._write_bytes(args.path, args.text.encode(), args.mode, args.expected_sha256)

    def read_binary(self, args: ReadBinary) -> dict[str, JsonValue]:
        path = absolute_path(args.path)
        flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
        with os.fdopen(os.open(path, flags), "rb") as source:
            before = os.fstat(source.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise ValueError("Only regular files can be read")
            if before.st_size > MAX_BINARY_READ_BYTES:
                raise ValueError("File exceeds the 1 GiB binary read limit")
            if args.offset > before.st_size:
                raise ValueError("Byte offset exceeds the file size")
            stop = min(before.st_size, args.offset + args.limit)
            hasher = hashlib.sha256()
            collected = bytearray()
            position = 0
            while block := source.read(262144):
                end = position + len(block)
                if end > before.st_size:
                    raise ValueError("File changed during transfer; restart the download")
                hasher.update(block)
                left, right = max(position, args.offset), min(end, stop)
                if left < right:
                    collected.extend(block[left - position : right - position])
                position = end
            after = os.fstat(source.fileno())
            # Windows Python 3.12 stat ctime uses birthtime; fstat uses change time.
            # Compare ctime only between fd samples, never across the two APIs.
            current = path.stat()
            if (
                (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino)
                or current.st_size != before.st_size
                or current.st_mtime_ns != before.st_mtime_ns
                or position != before.st_size
                or after.st_size != before.st_size
                or after.st_mtime_ns != before.st_mtime_ns
                or after.st_ctime_ns != before.st_ctime_ns
            ):
                raise ValueError("File changed during transfer; restart the download")
        digest = hasher.hexdigest()
        if args.expected_sha256 is not None and args.expected_sha256 != digest:
            raise ValueError("File changed during transfer; restart the download")
        chunk = bytes(collected)
        return {
            "path": str(path),
            "data_base64": base64.b64encode(chunk).decode("ascii"),
            "offset": args.offset,
            "next_offset": stop,
            "total_bytes": position,
            "sha256": digest,
            "chunk_sha256": sha256(chunk),
            "eof": stop == position,
        }

    def write_binary(self, args: WriteBinary) -> dict[str, JsonValue]:
        try:
            content = base64.b64decode(args.data_base64, validate=True)
        except (ValueError, binascii.Error):
            raise ValueError("data_base64 must contain canonical base64") from None
        if len(content) > 262144 or base64.b64encode(content).decode("ascii") != args.data_base64:
            raise ValueError("Use canonical base64 for at most 256 KiB per chunk")
        return self._write_bytes(args.path, content, args.mode, args.expected_sha256)

    def _write_bytes(
        self,
        target: str,
        data: bytes,
        mode: Literal["create", "replace", "append"],
        expected_sha256: str | None,
    ) -> dict[str, JsonValue]:
        path = absolute_path(target)
        lock_name = sha256(str(path.resolve()).encode())
        with ProcessLock(self.locks / lock_name, timeout=5):
            if path.is_symlink():
                raise ValueError("Write to the real file path, not a symbolic link")
            exists = path.exists()
            if mode == "create" and exists:
                raise FileExistsError("Target already exists; use a read hash for replacement")
            original = read_bytes(path) if exists else b""
            if exists and expected_sha256 != sha256(original):
                raise ValueError("File changed or expected_sha256 is missing; read it again")
            if not exists and expected_sha256 is not None:
                raise ValueError("Target disappeared after it was read")
            content = (original if mode == "append" else b"") + data
            if len(content) > MAX_READ_BYTES:
                raise ValueError("Result exceeds the file size limit")
            backup_id = None
            if exists:
                backup_id = sha256(original)
                backup_path = self.backups / backup_id
                if backup_path.exists() or backup_path.is_symlink():
                    if backup_path.is_symlink() or read_bytes(backup_path) != original:
                        raise ValueError("Existing backup is invalid; no file was changed")
                else:
                    with backup_path.open("xb") as backup:
                        backup.write(original)
                        backup.flush()
                        os.fsync(backup.fileno())
            temporary_fd, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=".anywhere-")
            try:
                with os.fdopen(temporary_fd, "wb") as destination:
                    destination.write(content)
                    destination.flush()
                    os.fsync(destination.fileno())
                if exists:
                    os.chmod(temporary_name, stat.S_IMODE(path.stat().st_mode))
                    if read_bytes(path) != original:
                        raise ValueError("Concurrent modification detected before replacement")
                    os.replace(temporary_name, path)
                else:
                    # Exclusive create: never overwrite a file created during this operation.
                    os.link(temporary_name, path)
            finally:
                Path(temporary_name).unlink(missing_ok=True)
            return {
                "path": str(path),
                "sha256": sha256(content),
                "bytes": len(content),
                "backup_id": backup_id,
            }

    def restore(self, args: RestoreFile) -> dict[str, JsonValue]:
        backup_path = self.backups / args.backup_id
        if backup_path.is_symlink():
            raise ValueError("Backup must not be a symbolic link")
        original = read_bytes(backup_path)
        if sha256(original) != args.backup_id:
            raise ValueError("Backup content hash is invalid; no file was changed")
        restored = self._write_bytes(
            args.path,
            original,
            "replace" if args.expected_sha256 is not None else "create",
            args.expected_sha256,
        )
        return {**restored, "restored_backup_id": args.backup_id}

    def edit(self, args: EditFile) -> dict[str, JsonValue]:
        original = read_bytes(absolute_path(args.path))
        if sha256(original) != args.expected_sha256:
            raise ValueError("File changed; read it again")
        text = original.decode("utf-8")
        if text.count(args.old_text) != args.expected_matches:
            raise ValueError("Match count differs; no edit was applied")
        return self.write(
            WriteFile(
                path=args.path,
                mode="replace",
                expected_sha256=args.expected_sha256,
                text=text.replace(args.old_text, args.new_text),
            )
        )

    def list_directory(self, args: ListDirectory) -> dict[str, JsonValue]:
        root = absolute_path(args.path)
        entries: list[JsonValue] = []
        pending = [(root, 0)]
        truncated = False
        while pending:
            directory, level = pending.pop()
            with os.scandir(directory) as iterator:
                for entry in iterator:
                    if not args.include_hidden and entry.name.startswith("."):
                        continue
                    if len(entries) >= args.limit:
                        truncated = True
                        break
                    is_directory = entry.is_dir(follow_symlinks=False)
                    entries.append(
                        {
                            "path": entry.path,
                            "directory": is_directory,
                            "symlink": entry.is_symlink(),
                        }
                    )
                    if is_directory and level + 1 < args.depth:
                        pending.append((Path(entry.path), level + 1))
            if truncated:
                break
        return {"entries": entries, "truncated": truncated}

    def move(self, args: MoveFile) -> dict[str, JsonValue]:
        source, destination = absolute_path(args.source), absolute_path(args.destination)
        # Rename portability and external races need an OS-specific no-replace primitive.
        # Until then, support only exclusive hard-link/unlink of regular files.
        if source.is_symlink() or not source.is_file():
            raise ValueError("This version moves regular files only")
        os.link(source, destination)
        source.unlink()
        return {"path": str(destination)}
