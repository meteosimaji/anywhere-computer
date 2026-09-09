import asyncio

import pytest
from test_client_tokens import MemoryVault

from anywhere_computer import cloudflare_tunnel, owner_credentials, remote_setup
from anywhere_computer.client_tokens import ClientCredentialError
from anywhere_computer.credentials import SERVICE
from anywhere_computer.http_service import load_http_config

PASSWORD = "synthetic setup password"
TOKEN = "synthetic_setup_token_12345"


@pytest.fixture
def wizard(monkeypatch):
    vault = MemoryVault()
    monkeypatch.setattr(owner_credentials, "secure_backend", lambda: vault)
    monkeypatch.setattr(cloudflare_tunnel, "secure_backend", lambda: vault)
    monkeypatch.setattr(remote_setup, "has_interactive_input", lambda: True)
    monkeypatch.setattr(remote_setup, "cloudflared_executable", lambda: "/fixture/cloudflared")

    def answer(fields=(), secrets=()):
        # Most cases use the default native callback option.
        if len(fields) == 5:
            fields = [*fields, ""]
        values, hidden = iter(fields), iter(secrets)
        monkeypatch.setattr("builtins.input", lambda prompt: next(values))
        monkeypatch.setattr(remote_setup.getpass, "getpass", lambda prompt: next(hidden))

    return vault, answer


def test_setup_creates_explicit_grant_and_resumes_without_overwriting(tmp_path, wizard, capsys):
    vault, answer = wizard
    answer(["https://computer.example/mcp", "", "", "", ""], [PASSWORD, PASSWORD, TOKEN])
    first = remote_setup.setup_remote(tmp_path)
    assert first["local_setup_complete"] is True and first["service_started"] is False
    config = load_http_config(tmp_path)
    assert config.client == "anywhere-native" and config.owner == "owner"
    assert "files_read" in config.scopes and "files_write" not in config.scopes
    assert "terminal_start" not in config.scopes
    before = dict(vault.data)
    writes = vault.writes
    answer()  # Any unnecessary prompt would exhaust the empty iterator.
    assert remote_setup.setup_remote(tmp_path) == first
    assert load_http_config(tmp_path) == config
    assert vault.data == before and vault.writes == writes
    output = capsys.readouterr().out
    assert PASSWORD not in output and TOKEN not in output


def test_setup_resumes_saved_config_after_password_mismatch(tmp_path, wizard):
    _, answer = wizard
    answer(["https://computer.example/mcp", "chosen", "native", "8769", "files"],
           [PASSWORD, "different"])
    with pytest.raises(ValueError, match="did not match"):
        remote_setup.setup_remote(tmp_path)
    before = load_http_config(tmp_path)
    assert "files_write" in before.scopes and "terminal_start" not in before.scopes
    answer(secrets=[PASSWORD, PASSWORD, ""])
    partial = remote_setup.setup_remote(tmp_path)
    assert partial["owner_initialized"] is True and partial["tunnel_credential_saved"] is False
    assert partial["local_setup_complete"] is False
    answer(secrets=[TOKEN])
    assert remote_setup.setup_remote(tmp_path)["local_setup_complete"] is True
    assert load_http_config(tmp_path) == before


def test_setup_preserves_corrupt_existing_verifier_and_reports_error(tmp_path, wizard):
    vault, answer = wizard
    answer(["https://computer.example/mcp", "", "", "", ""], [PASSWORD, PASSWORD, TOKEN])
    remote_setup.setup_remote(tmp_path)
    config = load_http_config(tmp_path)
    owner = owner_credentials.OwnerCredentials(
        tmp_path, resource=config.resource, owner=config.owner
    )
    vault.set_password(SERVICE, owner.account, "corrupt fixture")
    before = dict(vault.data)
    answer()
    with pytest.raises(ClientCredentialError, match="invalid"):
        remote_setup.setup_remote(tmp_path)
    assert vault.data == before


def test_setup_does_not_run_without_interactive_input(tmp_path, monkeypatch):
    monkeypatch.setattr(remote_setup, "has_interactive_input", lambda: False)
    directory = tmp_path / "absent"
    with pytest.raises(ValueError, match="interactive"):
        remote_setup.setup_remote(directory)
    assert not directory.exists()


def test_tunnel_setup_insert_only_protects_concurrent_writer(tmp_path, wizard):
    _, answer = wizard
    answer(["https://computer.example/mcp", "", "", "", ""], [PASSWORD, PASSWORD, TOKEN])
    remote_setup.setup_remote(tmp_path)
    credential = cloudflare_tunnel.TunnelCredential(tmp_path)
    with pytest.raises(ClientCredentialError, match="already exist"):
        credential.install(TOKEN + "replacement", replace=False)
    assert credential.read() == TOKEN


def test_setup_scopes_are_current_explicit_tool_names():
    read = asyncio.run(remote_setup.setup_scopes("read-only"))
    files = asyncio.run(remote_setup.setup_scopes("files"))
    all_tools = asyncio.run(remote_setup.setup_scopes("all"))
    assert read < all_tools and files < all_tools
    assert "terminal_start" in all_tools and "files_write" in all_tools
    assert "operations_recent" not in all_tools
    assert "terminal_start" not in files and "files_write" not in read
    assert "workspace_open" in files & read & all_tools
    assert "settings_update" not in files
    with pytest.raises(ValueError):
        asyncio.run(remote_setup.setup_scopes("wildcard"))


def test_setup_reports_missing_executable_without_starting(tmp_path, wizard, monkeypatch):
    _, answer = wizard
    def unsupported():
        raise RuntimeError("synthetic unavailable or unsupported connector")

    monkeypatch.setattr(remote_setup, "cloudflared_executable", unsupported)
    answer(["https://computer.example/mcp", "", "", "", "all"], [PASSWORD, PASSWORD, TOKEN])
    result = remote_setup.setup_remote(tmp_path)
    assert result["local_setup_complete"] is False and result["service_started"] is False
    assert result["tunnel_credential_saved"] is True
    assert "Install cloudflared" in result["next_step"]


def test_setup_accepts_explicit_client_callback(tmp_path, wizard):
    _, answer = wizard
    answer(["https://computer.example/mcp", "", "browser-client", "", "read-only",
            "https://client.example/callback"], [PASSWORD, PASSWORD, TOKEN])
    remote_setup.setup_remote(tmp_path)
    config = load_http_config(tmp_path)
    assert config.client == "browser-client"
    assert config.redirects == frozenset({"https://client.example/callback"})


def test_setup_resume_preserves_issued_and_revoked_grants(tmp_path, wizard):
    from anywhere_computer.authorization import AuthorizationStore, pkce_s256

    vault, answer = wizard
    answer(["https://computer.example/mcp", "", "", "", ""], [PASSWORD, PASSWORD, TOKEN])
    remote_setup.setup_remote(tmp_path)
    config = load_http_config(tmp_path)
    store = AuthorizationStore(tmp_path / "http-server/authorization",
                               resource=config.resource, known_tools=config.scopes)
    verifier = "v" * 43
    callback = sorted(config.redirects)[0]
    tokens = []
    try:
        for _ in range(2):
            code = store.approve(owner=config.owner, device=config.device, client=config.client,
                                 redirect=callback, resource=config.resource,
                                 tools=frozenset({"files_read"}), challenge=pkce_s256(verifier))
            tokens.append(store.exchange_code(code=code, verifier=verifier, client=config.client,
                                              redirect=callback, resource=config.resource))
        revoked = store.verify(tokens[0].value, resource=config.resource)
        store.revoke(owner=config.owner, grant=revoked.grant_id)
        vault.set_password(SERVICE, "unrelated-client-profile", "synthetic client refresh value")
        before_db = list(store.db.iterdump())
        before_vault = dict(vault.data)
        answer()
        remote_setup.setup_remote(tmp_path)
        assert list(store.db.iterdump()) == before_db and vault.data == before_vault
        assert store.verify(tokens[0].value, resource=config.resource) is None
        assert store.verify(tokens[1].value, resource=config.resource) is not None
        renewed = store.refresh(refresh_token=tokens[1].refresh_value, client=config.client,
                                resource=config.resource)
        assert store.verify(renewed.value, resource=config.resource) is not None
    finally:
        store.close()


async def test_screen_setup_plan_commits_exact_reviewed_fields(tmp_path, monkeypatch):
    from anywhere_computer.http_service import HTTPServiceConfig, save_http_config

    def forbidden(*args, **kwargs):
        raise AssertionError("Planning must not request terminal input or credentials")

    monkeypatch.setattr("builtins.input", forbidden)
    monkeypatch.setattr(remote_setup.getpass, "getpass", forbidden)
    monkeypatch.setattr(owner_credentials, "secure_backend", forbidden)
    target = tmp_path / "new-setup"
    plan = await remote_setup.plan_remote_setup(
        resource="https://computer.example/mcp", mode="files",
    )
    assert not target.exists()
    assert "workspace_open" in plan.scopes and "terminal_start" not in plan.scopes
    # A setup screen can serialize its public draft; frozen concrete permissions
    # remain unchanged when the user later confirms it.
    restored = HTTPServiceConfig.model_validate_json(plan.model_dump_json())
    monkeypatch.setattr(remote_setup, "setup_scopes", forbidden)
    saved = await save_http_config(target, restored)
    assert saved == plan == load_http_config(target)
    before = (target / "http-server/config.json").read_bytes()
    with pytest.raises(ValueError, match="already configured"):
        await save_http_config(target, restored)
    assert (target / "http-server/config.json").read_bytes() == before


@pytest.mark.parametrize("fields", [
    {"resource": "http://computer.example/mcp"},
    {"resource": "https://computer.example/mcp?secret=value"},
    {"resource": "https://computer.example/mcp", "port": 0},
    {"resource": "https://computer.example/mcp", "redirects": frozenset()},
    {"resource": "https://computer.example/mcp", "mode": "wildcard"},
])
async def test_screen_setup_rejects_invalid_drafts(fields):
    with pytest.raises(ValueError):
        await remote_setup.plan_remote_setup(**fields)


def test_chatgpt_setup_uses_fixed_public_client_and_resumes(tmp_path, wizard, capsys):
    vault, answer = wizard
    answer(["https://computer.example/mcp", "all"], [PASSWORD, PASSWORD, TOKEN])
    first = remote_setup.setup_remote(tmp_path, client_kind="chatgpt")
    config = load_http_config(tmp_path)
    assert config.client == "anywhere-chatgpt"
    assert config.redirects == frozenset({remote_setup.CHATGPT_REDIRECT})
    assert "terminal_start" in config.scopes and "codex_plugin_call" in config.scopes
    assert first["chatgpt_connection"]["connected"] is False
    assert first["chatgpt_connection"]["client_secret_required"] is False
    assert first["service_started"] is False
    before, writes = dict(vault.data), vault.writes
    answer()
    assert remote_setup.setup_remote(tmp_path, client_kind="chatgpt") == first
    assert load_http_config(tmp_path) == config
    assert vault.data == before and vault.writes == writes
    output = capsys.readouterr().out
    assert PASSWORD not in output and TOKEN not in output


def test_chatgpt_setup_preserves_existing_native_configuration(tmp_path, wizard):
    vault, answer = wizard
    answer(["https://computer.example/mcp", "", "", "", ""], [PASSWORD, PASSWORD, TOKEN])
    remote_setup.setup_remote(tmp_path)
    config, before, writes = load_http_config(tmp_path), dict(vault.data), vault.writes
    answer()
    with pytest.raises(ValueError, match="another client"):
        remote_setup.setup_remote(tmp_path, client_kind="chatgpt")
    assert load_http_config(tmp_path) == config
    assert vault.data == before and vault.writes == writes


def test_setup_command_preserves_special_path_without_shell_execution(tmp_path):
    import json
    import os
    import subprocess
    import sys

    if os.name == "nt":
        pytest.skip("POSIX shell execution test")
    directory = tmp_path / "日本語 space ' $(touch INJECTED) `touch ALSO_INJECTED`"
    commands = remote_setup.setup_commands(directory, "chatgpt")
    import shlex
    arguments = shlex.split(commands["resume"])
    assert arguments == [sys.executable, "-I", "-m", "anywhere_computer.cli",
                         "chatgpt-setup", "--state-dir", str(directory.resolve())]
    # Replace the module invocation with a harmless argv recorder, keeping the generated
    # state-path quoting intact. The shell must pass it literally, never execute its text.
    recorder = "import json,sys; print(json.dumps(sys.argv[1:]))"
    command = commands["resume"].replace(
        "-m anywhere_computer.cli chatgpt-setup", "-c " + shlex.quote(recorder), 1,
    )
    observed = subprocess.run(["/bin/sh", "-c", command], cwd=tmp_path,
                              capture_output=True, text=True, check=True, timeout=10)
    assert json.loads(observed.stdout) == ["--state-dir", str(directory.resolve())]
    assert not (tmp_path / "INJECTED").exists()
    assert not (tmp_path / "ALSO_INJECTED").exists()


def test_setup_commands_use_literal_powershell_arguments(tmp_path, monkeypatch):
    monkeypatch.setattr(remote_setup.sys, "platform", "win32")
    path = tmp_path / "quote' $() ` % !"
    result = remote_setup.setup_commands(path, "native")
    assert result["shell"] == "PowerShell"
    assert result["resume"].startswith("& '")
    assert "'remote-setup' '--state-dir'" in result["resume"]
    assert "quote'' $() ` % !'" in result["resume"]
    assert "'remote-watch'" in result["start"]
    assert "'remote-doctor'" in result["diagnose"]
