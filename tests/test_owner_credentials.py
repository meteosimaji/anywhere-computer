import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from test_client_tokens import MemoryVault

from anywhere_computer import cli
from anywhere_computer.client_tokens import ClientCredentialError
from anywhere_computer.credentials import SERVICE
from anywhere_computer.owner_credentials import OwnerCredentials, OwnerPasswordChangeUnknown

OLD_PASSWORD = "synthetic old owner password"
NEW_PASSWORD = "synthetic new owner password 日本語"


@pytest.fixture
def configured_owner(tmp_path):
    owner = OwnerCredentials(
        tmp_path, resource="https://computer.example/mcp", owner="owner", vault=MemoryVault()
    )
    owner.initialize(OLD_PASSWORD)
    return owner


def test_password_change_reopens_with_fresh_salt_and_no_plaintext(configured_owner, tmp_path):
    owner = configured_owner
    original = dict(owner.vault.data)
    owner.change_password(OLD_PASSWORD, NEW_PASSWORD)
    assert owner.verify(NEW_PASSWORD) and not owner.verify(OLD_PASSWORD)
    assert original != owner.vault.data
    reopened = OwnerCredentials(
        tmp_path, resource=owner.resource, owner=owner.owner, vault=owner.vault
    )
    assert reopened.verify(NEW_PASSWORD)
    assert (
        json.loads(next(iter(original.values())))["salt"]
        != json.loads(next(iter(owner.vault.data.values())))["salt"]
    )
    for path in tmp_path.iterdir():
        assert OLD_PASSWORD.encode() not in path.read_bytes()
        assert NEW_PASSWORD.encode() not in path.read_bytes()
    assert OLD_PASSWORD not in repr(owner.vault.data)
    assert NEW_PASSWORD not in repr(owner.vault.data)


@pytest.mark.parametrize(
    "current,replacement",
    [
        ("incorrect", NEW_PASSWORD),
        (OLD_PASSWORD, "short"),
        (OLD_PASSWORD, "日" * 400),
        (OLD_PASSWORD, OLD_PASSWORD),
    ],
)
def test_rejected_change_preserves_verifier(configured_owner, current, replacement):
    owner = configured_owner
    original = dict(owner.vault.data)
    with pytest.raises(ValueError):
        owner.change_password(current, replacement)
    assert owner.vault.data == original


@pytest.mark.parametrize("fault", ["before-write", "after-write", "readback", "no-write"])
def test_password_write_outcome_is_reconciled_without_retry(configured_owner, monkeypatch, fault):
    owner = configured_owner
    vault = owner.vault
    original_write, original_read = vault.set_password, vault.get_password
    writes = []

    def write(service, account, value):
        writes.append(True)
        if fault in {"after-write", "readback"}:
            original_write(service, account, value)
        if fault != "no-write":
            raise RuntimeError("private backend error " + value)

    def read(service, account):
        if fault == "readback" and writes:
            raise RuntimeError("private backend error")
        return original_read(service, account)

    monkeypatch.setattr(vault, "set_password", write)
    monkeypatch.setattr(vault, "get_password", read)
    if fault == "after-write":
        owner.change_password(OLD_PASSWORD, NEW_PASSWORD)
        assert owner.verify(NEW_PASSWORD)
    else:
        expected = OwnerPasswordChangeUnknown if fault == "readback" else ClientCredentialError
        with pytest.raises(expected) as error:
            owner.change_password(OLD_PASSWORD, NEW_PASSWORD)
        assert "private backend" not in str(error.value)
        assert NEW_PASSWORD not in str(error.value)
        monkeypatch.setattr(vault, "get_password", original_read)
        assert owner.verify(NEW_PASSWORD if fault == "readback" else OLD_PASSWORD)
    assert len(writes) == 1


def test_parallel_changes_authenticate_again_after_lock(configured_owner):
    owner = configured_owner
    replacements = [NEW_PASSWORD, "another synthetic replacement"]

    def change(password):
        try:
            owner.change_password(OLD_PASSWORD, password)
            return password
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        accepted = [item for item in executor.map(change, replacements) if item]
    assert len(accepted) == 1
    assert owner.verify(accepted[0])


def test_relative_owner_keeps_resolved_binding_after_cwd_changes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    owner = OwnerCredentials(
        Path("state"), resource="https://computer.example/mcp", owner="owner", vault=MemoryVault()
    )
    assert owner.lock_path.is_absolute()
    absolute = OwnerCredentials(
        tmp_path / "state", resource=owner.resource, owner=owner.owner, vault=owner.vault
    )
    assert owner.lock_path == absolute.lock_path and owner.account == absolute.account
    monkeypatch.chdir(tmp_path.parent)
    owner.initialize(OLD_PASSWORD)
    absolute.change_password(OLD_PASSWORD, NEW_PASSWORD)
    assert owner.verify(NEW_PASSWORD)


def test_verification_waits_for_password_writer(configured_owner, monkeypatch):
    owner = configured_owner
    entered, release = threading.Event(), threading.Event()
    original = owner.vault.set_password
    # Real scrypt derivations precede the write. Allow slow CI scheduling without
    # changing the 100 ms assertion that verification stays blocked by the writer.
    deadline = 30

    def paused_write(*args):
        original(*args)
        entered.set()
        assert release.wait(deadline)

    monkeypatch.setattr(owner.vault, "set_password", paused_write)
    with ThreadPoolExecutor(max_workers=2) as executor:
        rotation = executor.submit(owner.change_password, OLD_PASSWORD, NEW_PASSWORD)
        try:
            assert entered.wait(deadline)
            verification = executor.submit(owner.verify, OLD_PASSWORD)
            with pytest.raises(TimeoutError):
                verification.result(timeout=0.1)
        finally:
            release.set()
        rotation.result(timeout=deadline)
        assert verification.result(timeout=deadline) is False


def test_owner_password_is_salted_and_never_written_to_files(tmp_path):
    vault = MemoryVault()
    password = "synthetic owner password"
    owner = OwnerCredentials(
        tmp_path, resource="https://computer.example/mcp", owner="owner", vault=vault
    )
    owner.initialize(password)
    assert owner.verify(password)
    assert not owner.verify("incorrect password")
    assert not owner.verify("")
    raw = next(iter(vault.data.values()))
    assert password not in raw
    assert len(json.loads(raw)["salt"]) == 64
    assert all(password.encode() not in p.read_bytes() for p in tmp_path.iterdir())
    with pytest.raises(ClientCredentialError, match="already exist"):
        owner.initialize("replacement password")
    assert owner.verify(password)
    owner.forget()
    with pytest.raises(ClientCredentialError, match="not been initialized"):
        owner.verify(password)


def test_invalid_owner_verifier_is_sanitized(tmp_path):
    vault = MemoryVault()
    owner = OwnerCredentials(
        tmp_path, resource="https://computer.example/mcp", owner="owner", vault=vault
    )
    vault.data[SERVICE, owner.account] = '{"secret":"do-not-echo"}'
    with pytest.raises(ClientCredentialError) as error:
        owner.verify("synthetic password")
    assert "do-not-echo" not in str(error.value)
    with pytest.raises(ValueError, match="at least 16"):
        owner.initialize("short")


@pytest.mark.parametrize("command", ["owner-init", "owner-change"])
def test_owner_setup_rejects_piped_input_before_opening_vault(monkeypatch, capsys, command):
    monkeypatch.setattr(
        "sys.argv",
        [
            "anywhere",
            command,
            "--resource",
            "https://computer.example/mcp",
            "--owner",
            "owner",
        ],
    )
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 1
    assert "interactive terminal" in capsys.readouterr().err


def test_owner_change_hidden_confirmation_and_mismatch(configured_owner, monkeypatch, capsys):
    owner = configured_owner
    monkeypatch.setattr(
        "sys.argv",
        [
            "anywhere",
            "owner-change",
            "--resource",
            owner.resource,
            "--owner",
            owner.owner,
        ],
    )
    monkeypatch.setattr(cli, "has_interactive_input", lambda: True)
    monkeypatch.setattr(cli, "OwnerCredentials", lambda *args, **kwargs: owner)
    answers = iter([OLD_PASSWORD, NEW_PASSWORD, "different confirmation"])
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: next(answers))
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 1
    assert owner.verify(OLD_PASSWORD)
    assert NEW_PASSWORD not in capsys.readouterr().err
    answers = iter([OLD_PASSWORD, NEW_PASSWORD, NEW_PASSWORD])
    cli.main()
    assert json.loads(capsys.readouterr().out) == {"owner_password_changed": True}
    assert owner.verify(NEW_PASSWORD)


def test_owner_setup_uses_hidden_confirmation(monkeypatch, tmp_path, capsys):
    vault = MemoryVault()
    owner = OwnerCredentials(
        tmp_path, resource="https://computer.example/mcp", owner="owner", vault=vault
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "anywhere",
            "owner-init",
            "--resource",
            owner.resource,
            "--owner",
            owner.owner,
            "--state-dir",
            str(tmp_path),
        ],
    )
    monkeypatch.setattr(cli, "has_interactive_input", lambda: True)
    monkeypatch.setattr(cli, "OwnerCredentials", lambda *args, **kwargs: owner)
    prompts = []

    def hidden_input(prompt):
        prompts.append(prompt)
        return "synthetic owner password"

    monkeypatch.setattr(cli.getpass, "getpass", hidden_input)
    cli.main()
    output = capsys.readouterr()
    assert json.loads(output.out) == {"owner_initialized": True}
    assert not output.err and len(prompts) == 2
    assert owner.verify("synthetic owner password")
