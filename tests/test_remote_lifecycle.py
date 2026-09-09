"""Real CLI/process/HTTP/secret-pipe chain; synthetic vault and connector only."""

import asyncio
import json
import os
import site
import subprocess
import sys
import sysconfig
import time
import venv
from pathlib import Path

import httpx
import psutil
import pytest
from test_client_tokens import MemoryVault
from test_http_service import RESOURCE, authenticate, initialize, setup

from anywhere_computer.cloudflare_tunnel import TunnelCredential
from anywhere_computer.http_diagnostics import diagnose_http
from anywhere_computer.owner_credentials import OwnerCredentials


@pytest.mark.parametrize("kill_stage", ["remote-serve", "remote-watch"])
async def test_cli_chain_recovers_http_crash_and_cleans_up_owner_loss(
    tmp_path, unused_tcp_port, kill_stage
):
    state = tmp_path / "state"
    state.mkdir()
    await setup(state, unused_tcp_port)
    vault = MemoryVault()
    owner = OwnerCredentials(state, resource=RESOURCE, owner="owner", vault=vault)
    owner.initialize("synthetic owner password")
    TunnelCredential(state, vault=vault).install("synthetic_tunnel_token_12345")
    injections = tmp_path / "injections"
    injections.mkdir()
    selected = (str(injections / "selected 日本語 connector")
                if kill_stage == "remote-serve" else None)
    connector = injections / "connector.py"
    connector.write_text(
        "import sys,os,time,json\nfrom pathlib import Path\n"
        "assert Path(sys.argv[1]).read_bytes() == b'synthetic_tunnel_token_12345'\n"
        f"Path({str(tmp_path / 'connector.pid')!r}).write_text(json.dumps(os.getpid()))\n"
        "time.sleep(90)\n"
    )
    # Install fixture hooks into an owned disposable interpreter's trusted site,
    # never PYTHONPATH. Production -I must stay enabled for every child. No live
    # credential backend or source installation is changed; all values are synthetic.
    fixture_environment = tmp_path / "fixture-venv"
    venv.EnvBuilder(with_pip=False, symlinks=sys.platform != "win32").create(fixture_environment)
    fixture_site = Path(sysconfig.get_path(
        "purelib", vars={"base": str(fixture_environment), "platbase": str(fixture_environment)},
    ))
    fixture_site.mkdir(parents=True, exist_ok=True)
    (fixture_site / "test-dependencies.pth").write_text(
        "\n".join([str(Path(__file__).parents[1] / "src"), *site.getsitepackages()]) + "\n",
        encoding="utf-8",
    )
    interpreter = fixture_environment / (
        "Scripts/python.exe" if sys.platform == "win32" else "bin/python"
    )
    (fixture_site / "sitecustomize.py").write_text(f'''
import os, sys, json, subprocess
from pathlib import Path
from anywhere_computer import owner_credentials, cloudflare_tunnel, remote_service
class Vault:
    def __init__(self): self.data = {vault.data!r}
    def get_password(self, service, account): return self.data.get((service, account))
    def set_password(self, service, account, value): self.data[service, account] = value
    def delete_password(self, service, account): self.data.pop((service, account), None)
vault = Vault()
owner_credentials.secure_backend = lambda: vault
cloudflare_tunnel.secure_backend = lambda: vault
def selected_binary(value=None):
    assert value == {selected!r}, "connector path was lost or changed"
    return "fixture-cloudflared" if value is None else value
cloudflare_tunnel.cloudflared_executable = selected_binary
remote_service.cloudflared_executable = cloudflare_tunnel.cloudflared_executable
original = subprocess.Popen
def launch(command, *args, **options):
    if command[0] == {selected!r} or command[0] == "fixture-cloudflared":
        command = [sys.executable, {str(connector)!r}, command[-1]]
    return original(command, *args, **options)
subprocess.Popen = launch
for stage in ("remote-watch", "remote-serve", "tunnel-run"):
    if stage in sys.argv:
        Path({str(tmp_path)!r}, stage + ".pid").write_text(json.dumps(os.getpid()))
''', encoding="utf-8")
    environment = os.environ.copy()
    environment["PATH"] = str(injections)  # No installed provider binary may mask a missing stub.
    options = {}
    if sys.platform == "win32":
        options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    log_path = tmp_path / "chain.log"
    identities = []
    with log_path.open("wb") as log:
        process = subprocess.Popen(
            [str(interpreter), "-I", "-m", "anywhere_computer.cli", "remote-watch",
             "--state-dir", str(state), *([] if selected is None else ["--connector", selected])],
            stdin=subprocess.DEVNULL, stdout=log, stderr=log,
            start_new_session=sys.platform != "win32", env=environment, **options,
        )

        async def observed(stage, *, different=None):
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                try:
                    pid = json.loads((tmp_path / (stage + ".pid")).read_text())
                    if pid != different:
                        identity = psutil.Process(pid)
                        if identity.pid != process.pid and process.pid not in {
                            ancestor.pid for ancestor in identity.parents()
                        }:
                            await asyncio.sleep(0.05)
                            continue
                        identities.append(identity)
                        return identity
                except (FileNotFoundError, json.JSONDecodeError, psutil.NoSuchProcess):
                    pass
                if process.poll() is not None:
                    pytest.fail("CLI chain exited before readiness: " + log_path.read_text())
                await asyncio.sleep(0.05)
            pytest.fail("CLI chain readiness timed out: " + log_path.read_text())

        try:
            watcher = await observed("remote-watch")
            http = await observed("remote-serve")
            runner = await observed("tunnel-run")
            original_connector = await observed("connector")
            assert (await diagnose_http(state))["state"] == "metadata_reachable"
            target_file = tmp_path / "generated-document.txt"
            request = {
                "jsonrpc": "2.0", "id": "write", "method": "tools/call",
                "params": {
                    "name": "files_write",
                    "arguments": {"path": str(target_file), "text": "first result"},
                    "_meta": {"io.github.meteosimaji.anywhere-computer/operation_id": "a" * 32},
                },
            }
            async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{unused_tcp_port}", trust_env=False
            ) as client:
                token = await authenticate(client)
                headers = await initialize(client, token)
                reply = await client.post("/mcp", headers=headers, json=request)
                assert reply.status_code == 200 and not reply.json()["result"]["isError"]
            assert target_file.read_text() == "first result"
            # Kill the actual interpreter observed by the fixture, not a possible
            # Windows venv redirector returned by Popen.
            target = http if kill_stage == "remote-serve" else watcher
            target.kill()
            await asyncio.to_thread(target.wait, 10)
            await asyncio.to_thread(original_connector.wait, 30)
            await asyncio.to_thread(runner.wait, 30)
            if kill_stage == "remote-serve":
                replacement_http = await observed("remote-serve", different=http.pid)
                replacement_connector = await observed(
                    "connector", different=original_connector.pid
                )
                replacement_runner = await observed("tunnel-run", different=runner.pid)
                assert replacement_http.is_running() and replacement_connector.is_running()
                assert watcher.is_running()
                assert (await diagnose_http(state))["state"] == "metadata_reachable"
                target_file.write_text("external change after crash")
                async with httpx.AsyncClient(
                    base_url=f"http://127.0.0.1:{unused_tcp_port}", trust_env=False
                ) as client:
                    # The persisted grant survives; the runtime's MCP session is new.
                    headers = await initialize(client, token)
                    reply = await client.post("/mcp", headers=headers, json=request)
                    assert reply.status_code == 200 and not reply.json()["result"]["isError"]
                assert target_file.read_text() == "external change after crash"
                watcher.kill()
                await asyncio.to_thread(watcher.wait, 10)
                await asyncio.to_thread(replacement_connector.wait, 30)
                await asyncio.to_thread(replacement_runner.wait, 30)
                await asyncio.to_thread(replacement_http.wait, 30)
            else:
                await asyncio.to_thread(http.wait, 30)
            await asyncio.to_thread(process.wait, 15)
            assert (await diagnose_http(state))["state"] == "unreachable"
            assert "synthetic_tunnel_token_12345" not in log_path.read_text()
        finally:
            # Test failures also own all recorded identities and any descendants
            # still attached to the initial launcher; never search unrelated PIDs.
            try:
                identities.extend(psutil.Process(process.pid).children(recursive=True))
            except psutil.NoSuchProcess:
                pass
            for identity in reversed(identities):
                try:
                    if identity.is_running():
                        identity.kill()
                        await asyncio.to_thread(identity.wait, 5)
                except psutil.NoSuchProcess:
                    pass
            if process.poll() is None:
                process.kill()
                await asyncio.to_thread(process.wait, 5)
