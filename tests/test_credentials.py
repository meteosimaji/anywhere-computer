from types import SimpleNamespace

import pytest

from anywhere_computer import credentials


def windows_error(code):
    # Match the optional pywin32-ctypes exception used by the Windows bundle.
    error_type = type("error", (Exception,), {
        "__module__": "win32ctypes.pywin32.pywintypes",
    })
    error = error_type("private provider detail")
    error.winerror = code
    return error


@pytest.mark.parametrize("create", [False, True])
def test_unavailable_windows_logon_is_actionable_without_creating_credentials(
    tmp_path, monkeypatch, create,
):
    reads = []
    writes = []

    def read(*args):
        reads.append(args)
        raise windows_error(1312)

    backend = SimpleNamespace(get_password=read, set_password=lambda *args: writes.append(args))
    monkeypatch.setattr(credentials, "secure_backend", lambda: backend)
    monkeypatch.setattr(credentials.sys, "platform", "win32")
    with pytest.raises(RuntimeError, match="WinError 1312") as caught:
        credentials.local_credential(tmp_path, create=create)
    assert "SSH logon" in str(caught.value)
    assert "private provider detail" not in str(caught.value)
    assert len(reads) == 1
    assert writes == []


@pytest.mark.parametrize("error", [windows_error(5), ValueError("implementation failure")])
def test_unrelated_credential_failures_are_not_reclassified(tmp_path, monkeypatch, error):
    def read(*args):
        raise error

    monkeypatch.setattr(credentials, "secure_backend", lambda: SimpleNamespace(get_password=read))
    monkeypatch.setattr(credentials.sys, "platform", "win32")
    with pytest.raises(type(error)) as caught:
        credentials.local_credential(tmp_path)
    assert caught.value is error
