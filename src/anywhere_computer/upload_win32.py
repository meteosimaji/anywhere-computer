"""Pinned Windows directory ancestry for exclusive upload publication.

Each handle omits FILE_SHARE_DELETE. Windows requires DELETE access to rename
or remove a directory, so retaining every ancestor handle keeps a canonical
path stable while a path-based hard link is created.
"""

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
def pin_directory(path: Path) -> Iterator[tuple[str, str]]:
    """Reject reparse ancestors and hold every component against rename."""
    if os.name != "nt" or not path.is_absolute():
        raise ValueError("A canonical Windows directory path is required")
    kernel = _kernel()
    handles: list[int] = []
    invalid = ctypes.c_void_p(-1).value
    try:
        for component in reversed((path, *path.parents)):
            handle = kernel.CreateFileW(
                str(component), 0x80, 0x1 | 0x2, None, 3,
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
        yield f"v:{identity.volume:016x}", "f:" + bytes(identity.file_id).hex()
    finally:
        for handle in reversed(handles):
            kernel.CloseHandle(handle)


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


@contextmanager
def pin_regular_nofollow(path: Path) -> Iterator[tuple[str, str]]:
    """Hold a regular file's name and identity, not exclusive content access."""
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
        identity = _FileIdInfo()
        if (not kernel.GetFileInformationByHandleEx(
                handle, 9, ctypes.byref(tag), ctypes.sizeof(tag))
                or not kernel.GetFileInformationByHandleEx(
                    handle, 18, ctypes.byref(identity), ctypes.sizeof(identity))):
            raise ValueError("Upload file identity is unavailable")
        if tag.attributes & (0x400 | 0x10):
            raise ValueError("Upload staging file is not a regular file")
        yield f"v:{identity.volume:016x}", "f:" + bytes(identity.file_id).hex()
    finally:
        kernel.CloseHandle(handle)
