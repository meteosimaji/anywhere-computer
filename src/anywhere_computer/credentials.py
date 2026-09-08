"""Credential values remain in the OS credential store, never config or arguments."""

import hashlib
import secrets
import sys
from pathlib import Path

import keyring
from keyring.backend import KeyringBackend
from keyring.backends.chainer import ChainerBackend
from keyring.errors import KeyringError

SERVICE = "Anywhere Computer"


def has_interactive_input() -> bool:
    """Windows NUL is a character device, but is not a console for hidden input."""
    if not sys.stdin.isatty():
        return False
    if sys.platform == "win32":
        import ctypes
        import msvcrt
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.GetConsoleMode.restype = wintypes.BOOL
        mode = wintypes.DWORD()
        try:
            handle = msvcrt.get_osfhandle(sys.stdin.fileno())
            return bool(kernel.GetConsoleMode(handle, ctypes.byref(mode)))
        except (OSError, ValueError):
            return False
    return True


def secure_backend() -> KeyringBackend:
    """Select a native OS credential store; never fall back to plaintext."""
    backend = keyring.get_keyring()
    secure_modules = (
        "keyring.backends.macOS",
        "keyring.backends.Windows",
        "keyring.backends.SecretService",
        "keyring.backends.kwallet",
    )
    if isinstance(backend, ChainerBackend):
        candidates = [
            candidate
            for candidate in backend.backends
            if type(candidate).__module__.startswith(secure_modules)
        ]
        if not candidates:
            raise RuntimeError("No OS credential store is available")
        backend = candidates[0]
    # Refuse third-party file-backed implementations, even if selected by the environment.
    module = type(backend).__module__
    if not module.startswith(secure_modules):
        raise RuntimeError("An OS credential store is required; plaintext stores are unsupported")
    return backend


def local_credential(directory: Path, *, create: bool = False) -> str:
    account = "local-agent-" + hashlib.sha256(str(directory.resolve()).encode()).hexdigest()[:24]
    try:
        backend = secure_backend()
        credential = backend.get_password(SERVICE, account)
        if credential is None and create:
            credential = secrets.token_urlsafe(32)
            backend.set_password(SERVICE, account, credential)
        if credential is None:
            raise RuntimeError("Agent is not initialized. Run: anywhere start")
        return credential
    except KeyringError as error:
        raise RuntimeError(
            "OS credential store is unavailable; unlock it and run anywhere doctor"
        ) from error
