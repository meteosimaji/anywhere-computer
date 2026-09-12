import asyncio
import subprocess
import sys
from contextlib import asynccontextmanager
from dataclasses import replace

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
        assert command[1:5] == ["-I", "-m", "anywhere_computer.cli", "tunnel-run"]
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


@pytest.mark.parametrize("enabled", [False, True])
async def test_shared_remote_service_owns_update_monitor(remote_profile, monkeypatch, enabled):
    directory, _ = remote_profile
    from anywhere_computer.release_supervisor import configure_automatic_updates

    if enabled:
        configure_automatic_updates(directory, enabled=True)
    original_service = remote_service.http_service
    original_popen = subprocess.Popen
    started, stopped = asyncio.Event(), asyncio.Event()

    @asynccontextmanager
    async def service(path):
        async with original_service(path) as running:
            yield replace(running, config=running.config.model_copy(update={
                'shared_agent_directory': str(directory / 'shared'),
            }))

    async def monitor(control):
        assert control == directory / 'shared'
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    def launch(command, **options):
        return original_popen([sys.executable, '-I', '-c', 'import time; time.sleep(60)'],
                              **options)

    monkeypatch.setattr(remote_service, 'http_service', service)
    monkeypatch.setattr(remote_service, 'monitor_release_updates', monitor)
    monkeypatch.setattr(remote_service.subprocess, 'Popen', launch)
    task = asyncio.create_task(remote_service.serve_remote(directory))
    try:
        if enabled:
            await asyncio.wait_for(started.wait(), timeout=5)
        else:
            await asyncio.sleep(0.3)
            assert not started.is_set()
        assert (await diagnose_http(directory))['state'] == 'metadata_reachable'
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert stopped.is_set() == enabled


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


async def test_remote_cleanup_includes_real_launcher_descendants(tmp_path):
    import json

    marker = tmp_path / "descendant.json"
    # Use the actual venv executable: on Windows this includes its redirector.
    # The nested process owns no application data or external credentials.
    code = (
        "import subprocess,sys,time,json,os,pathlib; "
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],"
        "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
        f"pathlib.Path({str(marker)!r}).write_text(json.dumps([os.getpid(),child.pid])); "
        "time.sleep(60)"
    )
    options = {}
    if sys.platform == "win32":
        options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    process = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               start_new_session=sys.platform != "win32", **options)
    identities = []
    try:
        async with asyncio.timeout(10):
            while not marker.exists():
                await asyncio.sleep(0.01)
            while True:
                try:
                    identities = [psutil.Process(pid) for pid in json.loads(marker.read_text())]
                    break
                except json.JSONDecodeError:
                    await asyncio.sleep(0.01)
        remote_service.stop_remote_connector(process)
        assert process.poll() is not None
        assert all(not identity.is_running() for identity in identities)
    finally:
        remote_service.stop_remote_connector(process)
        for identity in identities:
            if identity.is_running():
                identity.kill()
                identity.wait(5)


async def test_remote_loses_tunnel_lock_after_preflight(remote_profile, monkeypatch):
    directory, _ = remote_profile
    original = subprocess.Popen
    winner = ProcessLock(directory / "cloudflare-tunnel.lock")
    claimed = False

    def launch(command, **options):
        nonlocal claimed
        winner.__enter__()
        claimed = True
        # Exercise the actual runtime's kernel lock acquisition in the child.
        return original(
            [sys.executable, "-c",
             "from pathlib import Path; from anywhere_computer.locking import ProcessLock; "
             f"lock=ProcessLock(Path({str(winner.path)!r})); lock.__enter__()"], **options
        )

    monkeypatch.setattr(remote_service.subprocess, "Popen", launch)
    try:
        assert await remote_service.serve_remote(directory) != 0
        assert claimed and winner.fd is not None
        assert (await diagnose_http(directory))["state"] == "unreachable"
        with pytest.raises(TimeoutError), ProcessLock(winner.path):
            pass
    finally:
        winner.__exit__(None, None, None)


async def test_parent_loss_stops_remote_service(remote_profile, monkeypatch):
    from threading import Event

    directory, _ = remote_profile
    stop = Event()
    original = subprocess.Popen
    children = []

    def launch(command, **options):
        assert "--watch-parent" in command and options["stdin"] == subprocess.PIPE
        child = original([sys.executable, "-c", "import time; time.sleep(60)"], **options)
        children.append(child)
        stop.set()
        return child

    monkeypatch.setattr(remote_service.subprocess, "Popen", launch)
    assert await remote_service.serve_remote(directory, stop=stop) == 130
    assert children[0].poll() is not None
    assert children[0].stdin.closed
    assert (await diagnose_http(directory))["state"] == "unreachable"


async def test_remote_watch_preflight_and_parent_pipe(remote_profile, monkeypatch):
    directory, _ = remote_profile
    from types import SimpleNamespace

    monkeypatch.setattr(remote_service, "OwnerCredentials", lambda *args, **kwargs:
                        SimpleNamespace(ensure_initialized=lambda: None))
    calls = []

    def supervise(command, **options):
        calls.append((command, options))
        with pytest.raises(TimeoutError), ProcessLock(directory / "remote-watch.lock"):
            pass
        return 7

    monkeypatch.setattr(remote_service, "supervise", supervise)
    assert remote_service.watch_remote(directory) == 7
    assert len(calls) == 1 and calls[0][1] == {
        "parent_pipe": True, "status_path": directory / "remote-watch-status.json",
    }
    assert "remote-serve" in calls[0][0] and "--watch-parent" in calls[0][0]
    with ProcessLock(directory / "remote-watch.lock"):
        pass
