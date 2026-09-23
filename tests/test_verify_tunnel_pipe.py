import importlib.util
import shutil
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "verify_tunnel_pipe", Path(__file__).resolve().parents[1] / "scripts/verify_tunnel_pipe.py",
)
assert SPEC is not None and SPEC.loader is not None
verify_tunnel_pipe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verify_tunnel_pipe)


def test_temporary_binary_cleanup_retries_windows_sharing_violation(monkeypatch):
    remove = shutil.rmtree
    attempts = []
    delays = []

    def locked_once(path):
        attempts.append(path)
        if len(attempts) == 1:
            error = PermissionError("binary is still mapped")
            error.winerror = 32
            raise error
        remove(path)

    monkeypatch.setattr(verify_tunnel_pipe.shutil, "rmtree", locked_once)
    monkeypatch.setattr(verify_tunnel_pipe.time, "sleep", delays.append)
    with verify_tunnel_pipe.temporary_verification_directory() as directory:
        (directory / "cloudflared.exe").write_bytes(b"fixture")
    assert not directory.exists()
    assert attempts == [directory, directory]
    assert delays == [0.1]


def test_temporary_binary_cleanup_propagates_other_permission_errors(monkeypatch):
    remove = shutil.rmtree

    def denied(path):
        remove(path)
        raise PermissionError("different permission failure")

    monkeypatch.setattr(verify_tunnel_pipe.shutil, "rmtree", denied)
    with pytest.raises(PermissionError, match="different permission failure"):
        with verify_tunnel_pipe.temporary_verification_directory() as directory:
            (directory / "cloudflared.exe").write_bytes(b"fixture")
    assert not directory.exists()
