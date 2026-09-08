"""Exclusive same-filesystem rename with small OS adapters and no overwrite fallback."""

import ctypes
import errno
import os
import sys
from pathlib import Path


def move_exclusive(source: Path, destination: Path) -> None:
    if os.name == "nt":
        os.rename(source, destination)
        return
    library = ctypes.CDLL(None, use_errno=True)
    result: int = 0
    old, new = os.fsencode(source), os.fsencode(destination)
    if b"\0" in old or b"\0" in new:
        raise ValueError("Paths must not contain NUL")
    if sys.platform == "darwin" and hasattr(library, "renamex_np"):
        rename = library.renamex_np
        rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        result = rename(old, new, 4)  # RENAME_EXCL
    elif sys.platform.startswith("linux") and hasattr(library, "renameat2"):
        rename = library.renameat2
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                          ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        result = rename(-100, old, -100, new, 1)  # AT_FDCWD, RENAME_NOREPLACE
    else:
        raise OSError(errno.ENOTSUP, "Exclusive rename is unavailable on this platform")
    if result != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), str(source), None, str(destination))
