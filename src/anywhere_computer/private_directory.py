"""Create a private directory for the process user, including elevated Windows tokens."""

from __future__ import annotations

import os
from pathlib import Path


def create_private_directory(path: Path) -> None:
    """Create missing directories privately; never rewrite an existing ACL."""
    if os.name != "nt":
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        return
    _windows_create_private_directory(path)


def _directory_sddl(user_sid: str) -> str:
    return f"O:{user_sid}D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;FA;;;{user_sid})"


_EXISTING_ACL_MESSAGE = (
    "Existing private directory has an incompatible Windows ACL; stop the application "
    "and migrate its data with an explicit ACL repair before continuing: "
)


def _validate_private_acl(owner: str, protected: bool,
                          entries: list[tuple[int, int, int, str]], user_sid: str) -> bool:
    """Accept only our protected, inheritable full-control directory ACL."""
    expected = {"S-1-5-18", "S-1-5-32-544", user_sid}  # SYSTEM, Administrators, user
    return (owner == user_sid and protected and len(entries) == len(expected)
            and {sid for _, _, _, sid in entries} == expected
            and all(kind == 0 and flags == 0x03 and mask == 0x1F01FF
                    for kind, flags, mask, _ in entries))


def _reject_windows_reparse_paths(path: Path) -> None:
    """Reject redirects on the target or any existing ancestor."""
    for candidate in (path.absolute(), *path.absolute().parents):
        try:
            metadata = candidate.lstat()
        except FileNotFoundError:
            continue
        if getattr(metadata, "st_file_attributes", 0) & 0x400:  # FILE_ATTRIBUTE_REPARSE_POINT
            raise ValueError(f"Private directory path contains a reparse point: {candidate}")


def _migration_entries(root: Path) -> list[Path]:
    """Preflight the whole old state tree before changing any security descriptor."""
    _reject_windows_reparse_paths(root)
    if not root.is_dir():
        raise ValueError(f"Private directory is missing: {root}")
    entries = [root]

    def fail_walk(error: OSError) -> None:
        raise error

    for base, directories, files in os.walk(root, followlinks=False, onerror=fail_walk):
        for name in (*directories, *files):
            entry = Path(base) / name
            metadata = entry.lstat()
            if entry.is_symlink() or getattr(metadata, "st_file_attributes", 0) & 0x400:
                raise ValueError(f"Private directory contains a reparse point: {entry}")
            if not entry.is_dir() and not entry.is_file():
                raise ValueError(f"Private directory contains an unsupported entry: {entry}")
            if not entry.is_dir() and metadata.st_nlink > 1:
                raise ValueError(f"Private directory contains a hard-linked file: {entry}")
            entries.append(entry)
    return entries


def _validate_existing_windows_directory(path: Path, user_sid: str) -> None:
    """Inspect an existing ACL without changing the directory or its children."""
    import ctypes
    from ctypes import wintypes

    class AclSizeInformation(ctypes.Structure):
        _fields_ = [("ace_count", wintypes.DWORD),
                    ("bytes_in_use", wintypes.DWORD),
                    ("bytes_free", wintypes.DWORD)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    security = ctypes.WinDLL("advapi32", use_last_error=True)  # type: ignore[attr-defined]
    kernel.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel.LocalFree.restype = wintypes.HLOCAL
    security.GetNamedSecurityInfoW.argtypes = [
        wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD,
        ctypes.POINTER(wintypes.LPVOID), wintypes.LPVOID,
        ctypes.POINTER(wintypes.LPVOID), wintypes.LPVOID,
        ctypes.POINTER(wintypes.LPVOID),
    ]
    security.GetNamedSecurityInfoW.restype = wintypes.DWORD
    security.GetSecurityDescriptorControl.argtypes = [
        wintypes.LPVOID, ctypes.POINTER(wintypes.WORD), ctypes.POINTER(wintypes.DWORD),
    ]
    security.GetSecurityDescriptorControl.restype = wintypes.BOOL
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
            raise ctypes.WinError(ctypes.get_last_error())  # type: ignore[attr-defined]

    def sid_string(sid: wintypes.LPVOID) -> str:
        rendered = wintypes.LPWSTR()
        check(security.ConvertSidToStringSidW(sid, ctypes.byref(rendered)))
        try:
            if rendered.value is None:
                raise OSError("Could not inspect private directory SID")
            return rendered.value
        finally:
            kernel.LocalFree(rendered)

    owner = wintypes.LPVOID()
    dacl = wintypes.LPVOID()
    descriptor = wintypes.LPVOID()
    result = security.GetNamedSecurityInfoW(str(path), 1, 0x00000005,
                                            ctypes.byref(owner), None,
                                            ctypes.byref(dacl), None,
                                            ctypes.byref(descriptor))
    if result:
        raise PermissionError(_EXISTING_ACL_MESSAGE + str(path)) from ctypes.WinError(  # type: ignore[attr-defined]
            result)
    try:
        control = wintypes.WORD()
        revision = wintypes.DWORD()
        check(security.GetSecurityDescriptorControl(
            descriptor, ctypes.byref(control), ctypes.byref(revision)))
        entries: list[tuple[int, int, int, str]] = []
        if dacl:
            information = AclSizeInformation()
            check(security.GetAclInformation(dacl, ctypes.byref(information),
                                             ctypes.sizeof(information), 2))
            for index in range(information.ace_count):
                ace = wintypes.LPVOID()
                check(security.GetAce(dacl, index, ctypes.byref(ace)))
                if ace.value is None:
                    raise PermissionError(_EXISTING_ACL_MESSAGE + str(path))
                address = ace.value
                kind = ctypes.c_ubyte.from_address(address).value
                flags = ctypes.c_ubyte.from_address(address + 1).value
                size = ctypes.c_ushort.from_address(address + 2).value
                if kind != 0 or size < 16:  # Only ACCESS_ALLOWED_ACE has SID at +8.
                    raise PermissionError(_EXISTING_ACL_MESSAGE + str(path))
                mask = ctypes.c_uint32.from_address(address + 4).value
                entries.append((kind, flags, mask, sid_string(wintypes.LPVOID(address + 8))))
        if not owner or not _validate_private_acl(
            sid_string(owner), bool(control.value & 0x1000), entries, user_sid
        ):
            raise PermissionError(_EXISTING_ACL_MESSAGE + str(path))
    finally:
        kernel.LocalFree(descriptor)


def _windows_current_user_sid() -> str:
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    security = ctypes.WinDLL("advapi32", use_last_error=True)  # type: ignore[attr-defined]
    kernel.GetCurrentProcess.argtypes = []
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel.LocalFree.restype = wintypes.HLOCAL
    security.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                          ctypes.POINTER(wintypes.HANDLE)]
    security.OpenProcessToken.restype = wintypes.BOOL
    security.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                              wintypes.LPVOID, wintypes.DWORD,
                                              ctypes.POINTER(wintypes.DWORD)]
    security.GetTokenInformation.restype = wintypes.BOOL
    security.ConvertSidToStringSidW.argtypes = [wintypes.LPVOID,
                                                 ctypes.POINTER(wintypes.LPWSTR)]
    security.ConvertSidToStringSidW.restype = wintypes.BOOL

    def check(success: object) -> None:
        if not success:
            raise ctypes.WinError(ctypes.get_last_error())  # type: ignore[attr-defined]

    token = wintypes.HANDLE()
    check(security.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008,
                                    ctypes.byref(token)))  # TOKEN_QUERY
    try:
        needed = wintypes.DWORD()
        security.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))
        if not 0 < needed.value <= 65536:
            raise OSError("Could not determine private directory owner")
        buffer = ctypes.create_string_buffer(needed.value)
        check(security.GetTokenInformation(token, 1, buffer, needed.value,
                                           ctypes.byref(needed)))
        sid = ctypes.cast(buffer, ctypes.POINTER(wintypes.LPVOID))[0]
        rendered = wintypes.LPWSTR()
        check(security.ConvertSidToStringSidW(sid, ctypes.byref(rendered)))
        try:
            if rendered.value is None:
                raise OSError("Could not determine private directory owner")
            return rendered.value
        finally:
            kernel.LocalFree(rendered)
    finally:
        kernel.CloseHandle(token)


def _windows_create_private_directory(path: Path) -> None:

    import ctypes
    from ctypes import wintypes

    class SecurityAttributes(ctypes.Structure):
        _fields_ = [
            ("length", wintypes.DWORD),
            ("descriptor", wintypes.LPVOID),
            ("inherit", wintypes.BOOL),
        ]

    win_dll = ctypes.WinDLL  # type: ignore[attr-defined]
    win_error = ctypes.WinError  # type: ignore[attr-defined]
    get_last_error = ctypes.get_last_error  # type: ignore[attr-defined]
    kernel = win_dll("kernel32", use_last_error=True)
    security = win_dll("advapi32", use_last_error=True)
    declarations = [
        (kernel.LocalFree, [wintypes.HLOCAL], wintypes.HLOCAL),
        (
            kernel.CreateDirectoryW,
            [wintypes.LPCWSTR, ctypes.POINTER(SecurityAttributes)],
            wintypes.BOOL,
        ),
        (
            security.ConvertStringSecurityDescriptorToSecurityDescriptorW,
            [
                wintypes.LPCWSTR,
                wintypes.DWORD,
                ctypes.POINTER(wintypes.LPVOID),
                ctypes.POINTER(wintypes.DWORD),
            ],
            wintypes.BOOL,
        ),
    ]
    for function, arguments, result in declarations:
        function.argtypes, function.restype = arguments, result

    def check(success: object) -> None:
        if not success:
            raise win_error(get_last_error())

    user_sid = _windows_current_user_sid()

    # CPython's mkdir(0700) grants OWNER RIGHTS. An elevated token may choose
    # Administrators as the default owner, locking out the same unelevated user.
    # Specify both the owner and an inheritable ACE for the token's user SID.
    sddl = _directory_sddl(user_sid)
    descriptor = wintypes.LPVOID()
    check(
        security.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, 1, ctypes.byref(descriptor), None
        )
    )
    try:
        attributes = SecurityAttributes(ctypes.sizeof(SecurityAttributes), descriptor, 0)
        _reject_windows_reparse_paths(path)
        if not path.parent.exists():
            _windows_create_private_directory(path.parent)
        _reject_windows_reparse_paths(path)
        if not kernel.CreateDirectoryW(str(path), ctypes.byref(attributes)):
            error = get_last_error()
            if error != 183:  # ERROR_ALREADY_EXISTS; never reset an existing ACL.
                raise win_error(error)
            _reject_windows_reparse_paths(path)
            if not path.is_dir():
                raise FileExistsError(f"Private directory path is not a directory: {path}")
            _validate_existing_windows_directory(path, user_sid)
    finally:
        kernel.LocalFree(descriptor)


def migrate_default_windows_state(*, apply: bool = False, root: Path | None = None) -> int:
    """Preflight or repair the stopped alpha state tree, preserving every file."""
    if os.name != "nt":
        raise OSError("Windows state ACL migration requires Windows")
    import ctypes
    from ctypes import wintypes

    if root is None:
        local_appdata = os.environ.get("LOCALAPPDATA")
        if not local_appdata:
            raise OSError("LOCALAPPDATA is required for the default Windows state")
        root = Path(local_appdata) / "Anywhere Computer" / "Anywhere Computer"
    entries = _migration_entries(root)
    if not apply:
        return len(entries)

    user_sid = _windows_current_user_sid()
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    security = ctypes.WinDLL("advapi32", use_last_error=True)  # type: ignore[attr-defined]
    kernel.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel.LocalFree.restype = wintypes.HLOCAL
    security.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.DWORD),
    ]
    security.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    security.GetSecurityDescriptorOwner.argtypes = [
        wintypes.LPVOID, ctypes.POINTER(wintypes.LPVOID), ctypes.POINTER(wintypes.BOOL),
    ]
    security.GetSecurityDescriptorOwner.restype = wintypes.BOOL
    security.GetSecurityDescriptorDacl.argtypes = [
        wintypes.LPVOID, ctypes.POINTER(wintypes.BOOL), ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.BOOL),
    ]
    security.GetSecurityDescriptorDacl.restype = wintypes.BOOL
    security.SetNamedSecurityInfoW.argtypes = [
        wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.LPVOID, wintypes.LPVOID, wintypes.LPVOID,
    ]
    security.SetNamedSecurityInfoW.restype = wintypes.DWORD

    descriptor = wintypes.LPVOID()
    if not security.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        _directory_sddl(user_sid), 1, ctypes.byref(descriptor), None
    ):
        raise ctypes.WinError(ctypes.get_last_error())  # type: ignore[attr-defined]
    try:
        owner = wintypes.LPVOID()
        dacl = wintypes.LPVOID()
        defaulted = wintypes.BOOL()
        present = wintypes.BOOL()
        if not security.GetSecurityDescriptorOwner(
            descriptor, ctypes.byref(owner), ctypes.byref(defaulted)
        ) or not security.GetSecurityDescriptorDacl(
            descriptor, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(defaulted)
        ) or not present:
            raise ctypes.WinError(ctypes.get_last_error())  # type: ignore[attr-defined]
        # Children first; each successful descriptor is already final. A failed
        # or interrupted run can be repeated after the operator resolves it.
        for entry in reversed(entries):
            _reject_windows_reparse_paths(entry)
            result = security.SetNamedSecurityInfoW(
                str(entry), 1, 0x80000005, owner, None, dacl, None
            )  # SE_FILE_OBJECT, OWNER | DACL | PROTECTED_DACL
            if result:
                raise PermissionError(f"ACL migration stopped at {entry}") from ctypes.WinError(  # type: ignore[attr-defined]
                    result)
        _validate_existing_windows_directory(root, user_sid)
    finally:
        kernel.LocalFree(descriptor)
    return len(entries)


def _windows_migration_targets() -> list[Path]:
    """The two alpha state roots: installed engine and default Subchat Plugin."""
    local_appdata = os.environ.get("LOCALAPPDATA")
    user_profile = os.environ.get("USERPROFILE")
    if not local_appdata or not user_profile:
        raise OSError("LOCALAPPDATA and USERPROFILE are required for Windows ACL migration")
    candidates = [
        Path(user_profile) / ".anywhere-computer" / "state",
        Path(local_appdata) / "Anywhere Computer" / "Anywhere Computer",
    ]
    return [path for path in candidates if path.exists() or path.is_symlink()]


def migrate_installed_windows_state(*, apply: bool = False) -> list[tuple[Path, int]]:
    """Preflight both installed roots before any ACL in either root is changed."""
    targets = _windows_migration_targets()
    if not targets:
        raise FileNotFoundError("No installed alpha or Subchat state directory was found")
    counts = [(target, migrate_default_windows_state(root=target)) for target in targets]
    if apply:
        for target, _ in counts:
            migrate_default_windows_state(root=target, apply=True)
    return counts


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="Preflight or repair default Windows alpha state ACLs")
    parser.add_argument("--apply-stopped", action="store_true",
                        help="Apply after stopping Anywhere Computer and the dedicated browser")
    arguments = parser.parse_args()
    counts = migrate_installed_windows_state(apply=arguments.apply_stopped)
    for target, count in counts:
        verb = "Migrated" if arguments.apply_stopped else "Preflighted"
        print(f"{verb} {count} state entries in {target}")


if __name__ == "__main__":
    main()
