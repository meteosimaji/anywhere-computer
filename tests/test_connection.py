import asyncio
import secrets
from pathlib import Path

import pytest

from anywhere_computer.connection import exchange, serve


@pytest.fixture
async def agent(tmp_path):
    credential = secrets.token_urlsafe(32)
    shutdown = asyncio.Event()
    task = asyncio.create_task(serve(tmp_path, credential=credential, shutdown=shutdown))
    for _ in range(300):
        if (tmp_path / "agent.json").exists():
            break
        if task.done():
            await task
        await asyncio.sleep(0.01)
    else:
        pytest.fail("Agent failed to publish endpoint")
    yield tmp_path, credential
    shutdown.set()
    await asyncio.wait_for(task, 10)
    # Windows mandatory locks prevent reading agent.lock while it is held.
    # Inspect every file after shutdown, including the released lock file.
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert credential.encode() not in path.read_bytes()


async def test_authenticated_rpc_and_catalog(agent):
    directory, credential = agent
    status = await exchange(directory, "__status", credential=credential)
    assert status.data["state"] == "ready"
    assert status.data["remote_ready"] is False
    catalog = await exchange(directory, "__catalog", credential=credential)
    assert len(catalog.data["tools"]) == 30
    assert "outputSchema" in catalog.data["tools"][0]
    with pytest.raises(ConnectionError):
        await exchange(directory, "__status", credential="wrong")
    again = await exchange(directory, "__status", credential=credential)
    assert again.data["instance_id"] == status.data["instance_id"]


async def test_local_transport_does_not_persist_credential(agent):
    directory, credential = agent
    await exchange(directory, "computer_status", credential=credential)
    for path in Path(directory).rglob("*"):
        if path.is_file() and path.name != "agent.lock":
            assert credential.encode() not in path.read_bytes()


async def test_service_refuses_shutdown_while_session_active(agent):
    import os
    import shlex
    import subprocess
    import sys

    directory, credential = agent
    argv = [sys.executable, "-u", "-c", "import time; time.sleep(20)"]
    command = subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)
    session = await exchange(
        directory,
        "terminal_start",
        {"command": command, "cwd": str(directory)},
        credential=credential,
    )
    stopped = await exchange(directory, "__stop", credential=credential)
    assert stopped.state == "failed"
    await exchange(
        directory,
        "terminal_stop",
        {"session_id": session.data["session_id"]},
        credential=credential,
    )
