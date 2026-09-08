import asyncio
import json
import os
import subprocess
import sys

import pytest
from test_client_tokens import MemoryVault

from anywhere_computer import cloudflare_tunnel
from anywhere_computer.client_tokens import ClientCredentialError
from anywhere_computer.cloudflare_tunnel import TunnelCredential, run_tunnel_child
from anywhere_computer.http_service import configure_http
from anywhere_computer.locking import ProcessLock

TOKEN = "synthetic_tunnel_secret_0123456789"


def configured(directory, vault):
    asyncio.run(
        configure_http(
            directory,
            resource="https://computer.example/mcp",
            owner="owner",
            client="client",
            port=8768,
            scopes=frozenset({"files_read"}),
            redirects=frozenset({"http://127.0.0.1/oauth/callback"}),
        )
    )
    return TunnelCredential(directory, vault=vault)


def test_native_token_binding_rotation_readback_and_forget(tmp_path):
    vault = MemoryVault()
    credential = configured(tmp_path / "first", vault)
    credential.install(TOKEN)
    reopened = TunnelCredential(tmp_path / "first", vault=vault)
    assert reopened.read() == TOKEN
    other = configured(tmp_path / "second", vault)
    with pytest.raises(ClientCredentialError, match="missing"):
        other.read()
    vault.fail_write = vault.writes + 1
    with pytest.raises(ClientCredentialError, match="could not be verified") as failure:
        credential.install(TOKEN + "new")
    assert TOKEN not in str(failure.value)
    assert credential.read() == TOKEN
    credential.install(TOKEN + "new")
    assert reopened.read() == TOKEN + "new"
    with ProcessLock(credential.lock):
        with pytest.raises(TimeoutError):
            credential.install(TOKEN)
        with pytest.raises(TimeoutError):
            credential.forget()
    credential.forget()
    credential.forget()
    with pytest.raises(ClientCredentialError, match="missing"):
        reopened.read()
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert TOKEN.encode() not in path.read_bytes()


def test_child_receives_secret_only_through_pipe(monkeypatch, tmp_path, capsys):
    captured = {}
    real_popen = subprocess.Popen
    result = tmp_path / "child-result"
    code = (
        "import sys,json; from pathlib import Path; "
        "token=Path(sys.argv[1]).read_text(); "
        "Path(sys.argv[2]).write_text(json.dumps({'length':len(token)}))"
    )

    def child(command, **kwargs):
        captured.update(command=command, **kwargs)
        return real_popen([sys.executable, "-c", code, command[-1], str(result)], **kwargs)

    monkeypatch.setattr(cloudflare_tunnel.subprocess, "Popen", child)
    monkeypatch.setenv("TUNNEL_TOKEN", "other-secret")
    monkeypatch.setenv("TUNNEL_CONFIG", "other-config")
    assert run_tunnel_child("fake-cloudflared", TOKEN) == 0
    assert json.loads(result.read_text()) == {"length": len(TOKEN)}
    assert TOKEN not in repr(captured)
    assert "TUNNEL_TOKEN" not in captured["env"]
    assert "TUNNEL_CONFIG" not in captured["env"]
    assert captured["command"][-2] == "--token-file"
    assert "--no-autoupdate" in captured["command"]
    assert captured["stderr"] == subprocess.DEVNULL
    assert TOKEN not in capsys.readouterr().out


def test_child_that_ignores_pipe_is_stopped(monkeypatch):
    real_popen = subprocess.Popen
    owned = []

    def child(command, **kwargs):
        process = real_popen([sys.executable, "-c", "import time; time.sleep(60)"], **kwargs)
        owned.append(process)
        return process

    monkeypatch.setattr(cloudflare_tunnel.subprocess, "Popen", child)
    with pytest.raises(TimeoutError):
        run_tunnel_child("fake-cloudflared", TOKEN, handoff_timeout=0.1)
    assert owned[0].poll() is not None


@pytest.mark.parametrize("command", ["tunnel-token", "owner-init", "owner-change"])
def test_cli_secret_input_requires_console(tmp_path, command):
    owner_options = (
        [] if command == "tunnel-token" else
        ["--resource", "https://computer.example/mcp", "--owner", "probe"]
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "anywhere_computer.cli",
            command,
            "--state-dir",
            str(tmp_path),
            *owner_options,
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=5,
    )
    assert result.returncode == 1
    assert b"interactive terminal" in result.stderr
    assert not os.listdir(tmp_path)


def test_restart_rereads_vault_and_budget_is_bounded(tmp_path, monkeypatch, capsys):
    vault = MemoryVault()
    credential = configured(tmp_path, vault)
    credential.install(TOKEN)
    monkeypatch.setattr(cloudflare_tunnel, "secure_backend", lambda: vault)
    monkeypatch.setattr(cloudflare_tunnel, "cloudflared_executable", lambda: "fake")
    delays, received = [], []
    monkeypatch.setattr(cloudflare_tunnel.time, "sleep", delays.append)

    def run(executable, token):
        received.append(token)
        vault.set_password(cloudflare_tunnel.SERVICE, credential.account, TOKEN + "next")
        return 7

    monkeypatch.setattr(cloudflare_tunnel, "run_tunnel_child", run)
    assert cloudflare_tunnel.run_tunnel(tmp_path, restart_limit=2) == 1
    assert received == [TOKEN, TOKEN + "next", TOKEN + "next"]
    assert delays == [1, 2]
    assert TOKEN not in capsys.readouterr().out
    with ProcessLock(credential.lock):
        pass


@pytest.mark.parametrize("version,accepted", [(b"2025.3.9", False), (b"2026.2.0", True)])
def test_binary_version_gate(monkeypatch, version, accepted):
    monkeypatch.setattr(cloudflare_tunnel.shutil, "which", lambda _: "/fake/cloudflared")
    monkeypatch.setattr(
        cloudflare_tunnel.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args, 0, b"cloudflared version " + version
        ),
    )
    if accepted:
        assert cloudflare_tunnel.cloudflared_executable() == "/fake/cloudflared"
    else:
        with pytest.raises(RuntimeError, match="2025.4.0"):
            cloudflare_tunnel.cloudflared_executable()


def test_explicit_connector_never_searches_path(tmp_path, monkeypatch):
    selected = str(tmp_path / "selected 日本語 connector")
    monkeypatch.setattr(cloudflare_tunnel.shutil, "which",
                        lambda _: pytest.fail("Explicit connector must bypass PATH"))
    calls = []

    def check(command, **options):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, b"cloudflared version 2026.2.0")

    monkeypatch.setattr(cloudflare_tunnel.subprocess, "run", check)
    assert cloudflare_tunnel.cloudflared_executable(selected) == selected
    assert calls == [[selected, "--version"]]
    with pytest.raises(ValueError, match="absolute path"):
        cloudflare_tunnel.cloudflared_executable("relative/connector")

    def missing(*args, **options):
        raise FileNotFoundError("synthetic missing connector")

    monkeypatch.setattr(cloudflare_tunnel.subprocess, "run", missing)
    with pytest.raises(FileNotFoundError):
        cloudflare_tunnel.cloudflared_executable(selected)


def test_lost_credential_after_child_exit_is_not_retried(tmp_path, monkeypatch, capsys):
    vault = MemoryVault()
    credential = configured(tmp_path, vault)
    credential.install(TOKEN)
    monkeypatch.setattr(cloudflare_tunnel, "secure_backend", lambda: vault)
    monkeypatch.setattr(cloudflare_tunnel, "cloudflared_executable", lambda: "fake")
    delays, starts = [], []
    monkeypatch.setattr(cloudflare_tunnel.time, "sleep", delays.append)

    def run(executable, token):
        starts.append(True)
        vault.delete_password(cloudflare_tunnel.SERVICE, credential.account)
        return 7

    monkeypatch.setattr(cloudflare_tunnel, "run_tunnel_child", run)
    with pytest.raises(ClientCredentialError, match="missing"):
        cloudflare_tunnel.run_tunnel(tmp_path)
    assert starts == [True]
    assert delays == [1]  # The actual child exit; missing credentials cause no further retry.
    assert TOKEN not in capsys.readouterr().out


@pytest.mark.skipif(sys.platform != "win32", reason="Windows console handle integration")
def test_windows_real_console_input_is_accepted():
    code = (
        "import ctypes,sys; "
        "from anywhere_computer.credentials import has_interactive_input; "
        "kernel=ctypes.WinDLL('kernel32'); kernel.FreeConsole(); "
        "assert kernel.AllocConsole(); sys.stdin=open('CONIN$', 'r'); "
        "assert has_interactive_input(); print('console-accepted')"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert b"console-accepted" in result.stdout


def test_parent_loss_stops_live_connector_after_token_handoff(monkeypatch, tmp_path):
    from threading import Event, Timer

    stop = Event()
    original = subprocess.Popen
    children = []
    code = (
        "import sys,time; from pathlib import Path; "
        "Path(sys.argv[1]).read_bytes(); time.sleep(60)"
    )

    def child(command, **options):
        process = original([sys.executable, "-c", code, command[-1]], **options)
        children.append(process)
        return process

    monkeypatch.setattr(cloudflare_tunnel.subprocess, "Popen", child)
    timer = Timer(0.5, stop.set)
    timer.start()
    try:
        assert run_tunnel_child("fixture", TOKEN, stop=stop) == 130
        assert len(children) == 1 and children[0].poll() is not None
    finally:
        timer.cancel()
        timer.join()
        for process in children:
            if process.poll() is None:
                process.kill()
                process.wait(5)
