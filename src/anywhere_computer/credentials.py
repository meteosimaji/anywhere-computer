"""Credential values remain in the OS credential store, never config or arguments."""

import hashlib
import secrets
from pathlib import Path

import keyring
from keyring.backends.chainer import ChainerBackend
from keyring.errors import KeyringError

SERVICE = "Anywhere Computer"


def local_credential(directory: Path, *, create: bool = False) -> str:
    account = "local-agent-" + hashlib.sha256(str(directory.resolve()).encode()).hexdigest()[:24]
    try:
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
            raise RuntimeError(
                "An OS credential store is required; plaintext stores are unsupported"
            )
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
