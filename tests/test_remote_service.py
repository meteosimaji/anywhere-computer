import asyncio
import subprocess
import sys
from contextlib import asynccontextmanager

import psutil
import pytest
from test_client_tokens import MemoryVault
from test_http_service import RESOURCE, setup

from anywhere_computer import remote_service
from anywhere_computer.cloudflare_tunnel import TunnelCredential
from anywhere_computer.http_diagnostics import diagnose_http
from anywhere_computer.http_service import http_service
from anywhere_computer.locking import ProcessLock
from anywhere_computer.owner_credentials import OwnerCredentials


@pytest.fixture
async def remote_profile(tmp_path, unused_tcp_port, monkeypatch):
    await setup(tmp_path, unused_tcp_port)
    owner = OwnerCredentials(tmp_path, resource=RESOURCE, owner="owner", vault=MemoryVault())
    owner.initialize("synthetic owner password")
    tunnel = TunnelCredential(tmp_path, vault=MemoryVault())
    tunnel.install("synthetic_token_12345")

    @asynccontextmanager
    async def service(directory):
        async with http_service(directory, credentials=owner) as running:
            yield running

    monkeypatch.setattr(remote_service, "http_service", service)
    monkeypatch.setattr(remote_service, "TunnelCredential", lambda directory: tunnel)
    monkeypatch.setattr(remote_service, "cloudflared_executable", lambda: "fixture")
    return tmp_path, tunnel


@pytest.mark.parametrize("exit_code", [0, 1, 130])
async def test_remote_connector_exit_closes_http(remote_profile, monkeypatch, exit_code):
    directory, _ = remote_profile
    original = subprocess.Popen
    children = []

    def launch(command, **options):
        assert command[3] == "tunnel-run"
        assert str(directory) in command
        child = original(
            [getattr(sys, "_base_executable", sys.executable), "-c",
             f"import time; time.sleep(0.5); raise SystemExit({exit_code})"], **options
        )
        children.append(child)
        return child

    monkeypatch.setattr(remote_service.subprocess, "Popen", launch)
    task = asyncio.create_task(remote_service.serve_remote(directory))
    try:
        while not children:
            if task.done():
                await task
            await asyncio.sleep(0.01)
        assert (await diagnose_http(directory))["state"] == "metadata_reachable"
        assert await task == exit_code
        assert children[0].poll() is not None
        assert (await diagnose_http(directory))["state"] == "unreachable"
        with (
            ProcessLock(directory / "http-watch.lock"),
            ProcessLock(directory / "http-server.lock"),
        ):
            pass
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_remote_cancellation_stops_owned_connector(remote_profile, monkeypatch):
    directory, _ = remote_profile
    original = subprocess.Popen
    children = []

    def launch(command, **options):
        child = original(
            [getattr(sys, "_base_executable", sys.executable), "-c",
             "import time; time.sleep(60)"], **options
        )
        children.append(child)
        return child

    monkeypatch.setattr(remote_service.subprocess, "Popen", launch)
    task = asyncio.create_task(remote_service.serve_remote(directory))
    try:
        while not children:
            if task.done():
                await task
            await asyncio.sleep(0.01)
        identity = psutil.Process(children[0].pid)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not identity.is_running()
        assert (await diagnose_http(directory))["state"] == "unreachable"
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize(
    "lock_name", ["http-watch.lock", "http-server.lock", "cloudflare-tunnel.lock"]
)
async def test_remote_refuses_existing_owner(remote_profile, monkeypatch, lock_name):
    directory, _ = remote_profile

    def forbidden(*args, **kwargs):
        pytest.fail("An existing owner must prevent connector launch")

    monkeypatch.setattr(remote_service.subprocess, "Popen", forbidden)
    with ProcessLock(directory / lock_name):
        with pytest.raises(TimeoutError):
            await remote_service.serve_remote(directory)
    assert (await diagnose_http(directory))["state"] == "unreachable"


async def test_remote_spawn_failure_closes_bound_http(remote_profile, monkeypatch):
    directory, _ = remote_profile

    def fail(*args, **kwargs):
        raise OSError("synthetic spawn failure")

    monkeypatch.setattr(remote_service.subprocess, "Popen", fail)
    with pytest.raises(OSError, match="synthetic spawn"):
        await remote_service.serve_remote(directory)
    assert (await diagnose_http(directory))["state"] == "unreachable"
    with ProcessLock(directory / "http-watch.lock"), ProcessLock(directory / "http-server.lock"):
        pass


async def test_remote_missing_token_does_not_start_http(remote_profile, monkeypatch):
    directory, tunnel = remote_profile
    tunnel.forget()

    def forbidden(*args, **kwargs):
        pytest.fail("Missing credential must prevent connector launch")

    monkeypatch.setattr(remote_service.subprocess, "Popen", forbidden)
    with pytest.raises(RuntimeError):
        await remote_service.serve_remote(directory)
    assert (await diagnose_http(directory))["state"] == "unreachable"


async def test_remote_bind_failure_preserves_existing_listener(remote_profile, monkeypatch):
    directory, _ = remote_profile
    from anywhere_computer.http_service import load_http_config

    config = load_http_config(directory)
    listener = await asyncio.start_server(lambda reader, writer: writer.close(), "127.0.0.1",
                                         config.port)

    def forbidden(*args, **kwargs):
        pytest.fail("Bind failure must prevent connector launch")

    monkeypatch.setattr(remote_service.subprocess, "Popen", forbidden)
    try:
        with pytest.raises(OSError):
            await remote_service.serve_remote(directory)
        assert listener.is_serving()
    finally:
        listener.close()
        await listener.wait_closed()
