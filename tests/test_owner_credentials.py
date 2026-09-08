import json

import pytest
from test_client_tokens import MemoryVault

from anywhere_computer import cli
from anywhere_computer.client_tokens import ClientCredentialError
from anywhere_computer.credentials import SERVICE
from anywhere_computer.owner_credentials import OwnerCredentials


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


def test_owner_setup_rejects_piped_input_before_opening_vault(monkeypatch, capsys):
    monkeypatch.setattr(
        "sys.argv",
        [
            "anywhere",
            "owner-init",
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
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
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
