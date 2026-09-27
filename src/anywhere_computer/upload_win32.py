"""Pinned Windows handles for confined, handle-based hard-link publication."""

from __future__ import annotations

import ctypes
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from ctypes import wintypes
from pathlib import Path
from typing import cast


class _FileIdInfo(ctypes.Structure):
    _fields_ = [("volume", ctypes.c_ulonglong), ("file_id", ctypes.c_ubyte * 16)]


class _AttributeTagInfo(ctypes.Structure):
    _fields_ = [("attributes", wintypes.DWORD), ("reparse_tag", wintypes.DWORD)]


class _FileLinkInfo(ctypes.Structure):
    _fields_ = [("replace_if_exists", ctypes.c_ubyte),
                ("root_directory", wintypes.HANDLE),
                ("file_name_length", wintypes.ULONG),
                ("file_name", wintypes.WCHAR * 1)]


class _IoStatusBlock(ctypes.Structure):
    _fields_ = [("status", wintypes.LONG), ("information", ctypes.c_size_t)]


def _kernel() -> ctypes.CDLL:
    if os.name != "nt":
        raise OSError("Windows directory handles are unavailable")
    factory = cast(Callable[..., ctypes.CDLL], ctypes.__dict__["WinDLL"])
    kernel = factory("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                   wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                                   wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.GetFileInformationByHandleEx.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                    wintypes.LPVOID, wintypes.DWORD]
    kernel.GetFileInformationByHandleEx.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    return kernel


@contextmanager
def pin_directory_handle(
    path: Path, *, create: bool = False,
) -> Iterator[tuple[tuple[str, str], int]]:
    """Reject reparse ancestors; retain the exact target directory handle."""
    if os.name != "nt" or not path.is_absolute():
        raise ValueError("A canonical Windows directory path is required")
    kernel = _kernel()
    handles: list[int] = []
    invalid = ctypes.c_void_p(-1).value
    try:
        for component in reversed((path, *path.parents)):
            access = 0x80 | 0x20 | (0x2 if create and component == path else 0)
            handle = kernel.CreateFileW(
                str(component), access, 0x1 | 0x2, None, 3,
                0x02000000 | 0x00200000, None,
            )
            if handle == invalid or handle is None:
                raise ValueError("Upload directory could not be pinned") from OSError(
                    ctypes.__dict__["get_last_error"]())
            handles.append(handle)
            tag = _AttributeTagInfo()
            if not kernel.GetFileInformationByHandleEx(
                    handle, 9, ctypes.byref(tag), ctypes.sizeof(tag)):
                raise ValueError("Upload directory attributes are unavailable")
            if tag.attributes & 0x400 or not tag.attributes & 0x10:
                raise ValueError("Upload directory contains a reparse point")
        identity = _FileIdInfo()
        if not kernel.GetFileInformationByHandleEx(
                handles[-1], 18, ctypes.byref(identity), ctypes.sizeof(identity)):
            raise ValueError("Upload directory identity is unavailable")
        yield ((f"v:{identity.volume:016x}", "f:" + bytes(identity.file_id).hex()),
               handles[-1])
    finally:
        for handle in reversed(handles):
            kernel.CloseHandle(handle)


@contextmanager
def pin_directory(path: Path) -> Iterator[tuple[str, str]]:
    with pin_directory_handle(path) as (identity, _handle):
        yield identity


def open_regular_nofollow(path: Path) -> int:
    """Open the exact target entry for read; reject symlinks and other reparse points."""
    if os.name != "nt":
        raise OSError("Windows file handles are unavailable")
    import msvcrt

    kernel = _kernel()
    invalid = ctypes.c_void_p(-1).value
    handle = kernel.CreateFileW(str(path), 0x80000000, 0x1 | 0x2,
                                None, 3, 0x00200000, None)
    if handle == invalid or handle is None:
        raise OSError(ctypes.__dict__["get_last_error"](),
                      "Upload destination could not be opened")
    try:
        tag = _AttributeTagInfo()
        if not kernel.GetFileInformationByHandleEx(
                handle, 9, ctypes.byref(tag), ctypes.sizeof(tag)):
            raise ValueError("Upload destination attributes are unavailable")
        if tag.attributes & (0x400 | 0x10):
            raise ValueError("Published destination must be a regular file")
        descriptor = cast(int, msvcrt.__dict__["open_osfhandle"](
            handle, os.O_RDONLY | getattr(os, "O_BINARY", 0)))
        handle = None
        return descriptor
    finally:
        if handle is not None:
            kernel.CloseHandle(handle)


def _identity_for_handle(kernel: ctypes.CDLL, handle: int) -> tuple[str, str]:
    identity = _FileIdInfo()
    if not kernel.GetFileInformationByHandleEx(
            handle, 18, ctypes.byref(identity), ctypes.sizeof(identity)):
        raise ValueError("File identity is unavailable")
    return f"v:{identity.volume:016x}", "f:" + bytes(identity.file_id).hex()


def file_identity_fd(descriptor: int) -> tuple[str, str]:
    """Get the identity of an already-open CRT file descriptor."""
    if os.name != "nt":
        raise OSError("Windows file handles are unavailable")
    import msvcrt

    handle = cast(int, msvcrt.__dict__["get_osfhandle"](descriptor))
    return _identity_for_handle(_kernel(), handle)


class PinnedRegular:
    def __init__(self, handle: int, identity: tuple[str, str]) -> None:
        self.handle = handle
        self.identity = identity

    def link(self, target: Path) -> None:
        """Create a no-overwrite link to this file object, never re-open its path."""
        if (os.name != "nt" or not target.is_absolute() or target.name in {"", ".", ".."}
                or ":" in target.name or target.name.endswith((".", " "))):
            raise ValueError("A canonical Windows link destination is required")
        name = target.name.encode("utf-16-le")
        if not name or len(name) > 65534:
            raise ValueError("Invalid hard-link destination name")
        with pin_directory_handle(target.parent, create=True) as (_identity, directory_handle):
            buffer_size = max(ctypes.sizeof(_FileLinkInfo), _FileLinkInfo.file_name.offset
                              + len(name))
            buffer = ctypes.create_string_buffer(buffer_size)
            info = ctypes.cast(buffer, ctypes.POINTER(_FileLinkInfo)).contents
            info.replace_if_exists = 0
            info.root_directory = directory_handle
            info.file_name_length = len(name)
            ctypes.memmove(ctypes.addressof(buffer) + _FileLinkInfo.file_name.offset,
                           name, len(name))
            factory = cast(Callable[..., ctypes.CDLL], ctypes.__dict__["WinDLL"])
            ntdll = factory("ntdll", use_last_error=True)
            ntdll.NtSetInformationFile.argtypes = [wintypes.HANDLE,
                                                   ctypes.POINTER(_IoStatusBlock),
                                                   wintypes.LPVOID, wintypes.ULONG,
                                                   ctypes.c_int]
            ntdll.NtSetInformationFile.restype = wintypes.LONG
            ntdll.RtlNtStatusToDosError.argtypes = [wintypes.LONG]
            ntdll.RtlNtStatusToDosError.restype = wintypes.ULONG
            iosb = _IoStatusBlock()
            status = ntdll.NtSetInformationFile(self.handle, ctypes.byref(iosb), buffer,
                                                buffer_size, 11)
            if status != 0:
                code = ntdll.RtlNtStatusToDosError(status)
                if code in {80, 183}:
                    raise FileExistsError(code, "Hard-link destination already exists",
                                          str(target))
                raise OSError(code, f"Handle-based hard link failed (NTSTATUS {status:#x})",
                              str(target))


@contextmanager
def pin_regular_handle_nofollow(path: Path) -> Iterator[PinnedRegular]:
    """Hold a non-reparse regular file object for handle-based publication."""
    if os.name != "nt":
        raise OSError("Windows file handles are unavailable")
    kernel = _kernel()
    invalid = ctypes.c_void_p(-1).value
    handle = kernel.CreateFileW(str(path), 0x80, 0x1 | 0x2,
                                None, 3, 0x00200000, None)
    if handle == invalid or handle is None:
        raise OSError(ctypes.__dict__["get_last_error"](),
                      "Upload staging file could not be pinned")
    try:
        tag = _AttributeTagInfo()
        if not kernel.GetFileInformationByHandleEx(
                handle, 9, ctypes.byref(tag), ctypes.sizeof(tag)):
            raise ValueError("Upload file attributes are unavailable")
        if tag.attributes & (0x400 | 0x10):
            raise ValueError("Upload staging file is not a regular file")
        yield PinnedRegular(handle, _identity_for_handle(kernel, handle))
    finally:
        kernel.CloseHandle(handle)


@contextmanager
def pin_regular_nofollow(path: Path) -> Iterator[tuple[str, str]]:
    """Compatibility wrapper for identity-only callers."""
    with pin_regular_handle_nofollow(path) as pinned:
        yield pinned.identity
