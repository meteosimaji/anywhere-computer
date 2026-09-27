"""Windows private credential files inside an ACL-checked state directory."""

from __future__ import annotations

import ctypes
import os
import stat
from ctypes import wintypes
from pathlib import Path

from .private_directory import (
    _reject_windows_reparse_paths,
    _windows_current_user_sid,
    create_private_directory,
)
from .upload_win32 import PinnedRegular, file_identity_fd


def require_windows_credential_path(state_dir: Path, path: Path) -> None:
    """Keep bearer credentials in a private child of this mailbox only."""
    if os.name != "nt":
        raise OSError("Windows credential ACLs require Windows")
    if not state_dir.is_absolute() or not path.is_absolute():
        raise ValueError("Peer state and credential paths must be absolute")
    if path.parent != state_dir / "credentials":
        raise ValueError("Windows peer credentials must be inside state/credentials")
    _reject_windows_reparse_paths(path)
    create_private_directory(state_dir)
    create_private_directory(path.parent)
    _reject_windows_reparse_paths(path)


def create_windows_private_file(path: Path) -> int:
    """Create an exclusive file with an explicit user owner and protected DACL."""
    if os.name != "nt":
        raise OSError("Windows credential ACLs require Windows")

    import msvcrt

    class SecurityAttributes(ctypes.Structure):
        _fields_ = [("length", wintypes.DWORD),
                    ("descriptor", wintypes.LPVOID),
                    ("inherit", wintypes.BOOL)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined, unused-ignore]
    security = ctypes.WinDLL("advapi32", use_last_error=True)  # type: ignore[attr-defined, unused-ignore]
    kernel.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel.LocalFree.restype = wintypes.HLOCAL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        ctypes.POINTER(SecurityAttributes), wintypes.DWORD, wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel.CreateFileW.restype = wintypes.HANDLE
    security.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.DWORD),
    ]
    security.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    user_sid = _windows_current_user_sid()
    descriptor = wintypes.LPVOID()
    sddl = (f"O:{user_sid}D:P(A;;FA;;;SY)(A;;FA;;;BA)"
            f"(A;;FA;;;{user_sid})")
    if not security.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        sddl, 1, ctypes.byref(descriptor), None
    ):
        raise ctypes.WinError(ctypes.get_last_error())  # type: ignore[attr-defined, unused-ignore]
    try:
        attributes = SecurityAttributes(ctypes.sizeof(SecurityAttributes), descriptor, 0)
        handle = kernel.CreateFileW(str(path), 0x40000000 | 0x00010000, 0x1 | 0x2 | 0x4,
                                    ctypes.byref(attributes), 1, 0x80, None)
        if handle == ctypes.c_void_p(-1).value or handle is None:
            raise ctypes.WinError(ctypes.get_last_error())  # type: ignore[attr-defined, unused-ignore]
        try:
            descriptor_fd: int = msvcrt.__dict__["open_osfhandle"](
                handle, os.O_WRONLY | getattr(os, "O_BINARY", 0)
            )
            handle = None
            return descriptor_fd
        finally:
            if handle is not None:
                kernel.CloseHandle(handle)
    finally:
        kernel.LocalFree(descriptor)


def link_windows_private_file(descriptor: int, target: Path) -> None:
    """Publish the open file itself, never resolving a mutable staging name."""
    if os.name != "nt":
        raise OSError("Windows handle publication requires Windows")

    import msvcrt
    handle = msvcrt.__dict__["get_osfhandle"](descriptor)
    PinnedRegular(handle, file_identity_fd(descriptor)).link(target)


def validate_windows_private_file(path: Path) -> None:
    """Reject broad, null, redirected, or foreign-owned credential ACLs."""
    if os.name != "nt":
        raise OSError("Windows credential ACLs require Windows")
    _reject_windows_reparse_paths(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise PermissionError("Peer credential must be an unlinked regular file")

    class AclSizeInformation(ctypes.Structure):
        _fields_ = [("ace_count", wintypes.DWORD),
                    ("bytes_in_use", wintypes.DWORD),
                    ("bytes_free", wintypes.DWORD)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined, unused-ignore]
    security = ctypes.WinDLL("advapi32", use_last_error=True)  # type: ignore[attr-defined, unused-ignore]
    kernel.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel.LocalFree.restype = wintypes.HLOCAL
    security.GetNamedSecurityInfoW.argtypes = [
        wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD,
        ctypes.POINTER(wintypes.LPVOID), wintypes.LPVOID,
        ctypes.POINTER(wintypes.LPVOID), wintypes.LPVOID,
        ctypes.POINTER(wintypes.LPVOID),
    ]
    security.GetNamedSecurityInfoW.restype = wintypes.DWORD
    security.GetAclInformation.argtypes = [
        wintypes.LPVOID, wintypes.LPVOID, wintypes.DWORD, ctypes.c_int,
    ]
    security.GetAclInformation.restype = wintypes.BOOL
    security.GetAce.argtypes = [wintypes.LPVOID, wintypes.DWORD,
                                ctypes.POINTER(wintypes.LPVOID)]
    security.GetAce.restype = wintypes.BOOL
    security.ConvertSidToStringSidW.argtypes = [
        wintypes.LPVOID, ctypes.POINTER(wintypes.LPWSTR),
    ]
    security.ConvertSidToStringSidW.restype = wintypes.BOOL

    def check(result: object) -> None:
        if not result:
            raise ctypes.WinError(ctypes.get_last_error())  # type: ignore[attr-defined, unused-ignore]

    def sid_string(sid: wintypes.LPVOID) -> str:
        rendered = wintypes.LPWSTR()
        check(security.ConvertSidToStringSidW(sid, ctypes.byref(rendered)))
        try:
            if rendered.value is None:
                raise PermissionError("Cannot inspect credential SID")
            return rendered.value
        finally:
            kernel.LocalFree(rendered)

    owner = wintypes.LPVOID()
    dacl = wintypes.LPVOID()
    descriptor = wintypes.LPVOID()
    status = security.GetNamedSecurityInfoW(
        str(path), 1, 0x00000005, ctypes.byref(owner), None,
        ctypes.byref(dacl), None, ctypes.byref(descriptor),
    )
    if status:
        raise PermissionError("Cannot inspect peer credential ACL") from ctypes.WinError(  # type: ignore[attr-defined, unused-ignore]
            status)
    try:
        expected = {"S-1-5-18", "S-1-5-32-544", _windows_current_user_sid()}
        if not owner or not dacl or sid_string(owner) != _windows_current_user_sid():
            raise PermissionError("Peer credential owner or DACL is unsafe")
        information = AclSizeInformation()
        check(security.GetAclInformation(dacl, ctypes.byref(information),
                                         ctypes.sizeof(information), 2))
        entries: list[str] = []
        for index in range(information.ace_count):
            ace = wintypes.LPVOID()
            check(security.GetAce(dacl, index, ctypes.byref(ace)))
            if ace.value is None:
                raise PermissionError("Invalid peer credential ACE")
            address = ace.value
            kind = ctypes.c_ubyte.from_address(address).value
            flags = ctypes.c_ubyte.from_address(address + 1).value
            size = ctypes.c_ushort.from_address(address + 2).value
            mask = ctypes.c_uint32.from_address(address + 4).value
            if kind != 0 or size < 16 or flags & ~0x10 or mask != 0x1F01FF:
                raise PermissionError("Peer credential ACL grants unexpected access")
            entries.append(sid_string(wintypes.LPVOID(address + 8)))
        if set(entries) != expected or len(entries) != len(expected):
            raise PermissionError("Peer credential ACL grants unexpected access")
    finally:
        kernel.LocalFree(descriptor)
