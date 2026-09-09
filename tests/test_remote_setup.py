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
