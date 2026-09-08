"""Disposable POSIX HTTP supervisor crash/restart proof using native credentials."""

import argparse
import asyncio
import hashlib
import json
import os
import secrets
import signal
import socket
import sys
import tempfile
from pathlib import Path

import psutil

from anywhere_computer.engine import runtime_identity
from anywhere_computer.http_diagnostics import diagnose_http
from anywhere_computer.http_service import configure_http
from anywhere_computer.owner_credentials import OwnerCredentials


async def verify(receipt):
    if os.name == "nt":
        raise RuntimeError("This kill/signal lifecycle probe requires POSIX")
    report = {
        "completed": False,
        "runtime_id": runtime_identity(),
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "scope": "Disposable local HTTP watcher; native keyring; no public endpoint",
    }
    watcher = None
    owner = None
    children = {}
    with tempfile.TemporaryDirectory(prefix="anywhere-http-watch-") as temporary:
        directory = Path(temporary)
        try:
            with socket.socket() as reserved:
                reserved.bind(("127.0.0.1", 0))
                port = reserved.getsockname()[1]
            config = await configure_http(
                directory,
                resource="https://watch-probe.example/mcp",
                owner="probe-owner",
                client="probe-client",
                port=port,
                scopes=frozenset({"files_read"}),
                redirects=frozenset({"http://127.0.0.1/oauth/callback"}),
            )
            owner = OwnerCredentials(directory, resource=config.resource, owner=config.owner)
            await asyncio.to_thread(owner.initialize, secrets.token_urlsafe(32))

            async def launch():
                return await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-m",
                    "anywhere_computer.cli",
                    "http-watch",
                    "--state-dir",
                    str(directory),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                    start_new_session=True,
                )

            async def ready(process):
                assert process.stdout is not None
                latest = None
                for _ in range(16):
                    raw = await asyncio.wait_for(process.stdout.readline(), 15)
                    if not raw:
                        raise RuntimeError("Watcher exited before readiness")
                    event = json.loads(raw)
                    if "http_child_started" in event:
                        latest = psutil.Process(event["http_child_started"])
                        if latest.ppid() != process.pid:
                            raise RuntimeError("Reported child does not belong to the watcher")
                        children[latest.pid] = latest.create_time()
                    if "http_listening" in event:
                        if latest is None or event["http_listening"] != f"http://127.0.0.1:{port}":
                            raise RuntimeError("Unexpected server announcement")
                        if (await diagnose_http(directory))["state"] != "metadata_reachable":
                            raise RuntimeError("Restarted server is not reachable")
                        return latest
                raise RuntimeError("Watcher did not announce readiness")

            watcher = await launch()
            first = await ready(watcher)
            report["initial_ready"] = True
            duplicate = await launch()
            try:
                if await asyncio.wait_for(duplicate.wait(), 15) != 1:
                    raise RuntimeError("Duplicate watcher was not rejected")
            finally:
                if duplicate.returncode is None:
                    duplicate.kill()
                    await duplicate.wait()
            report["duplicate_rejected"] = True
            first.kill()
            second = await ready(watcher)
            if first.pid == second.pid:
                raise RuntimeError("No replacement child was observed")
            report["killed_child_replaced"] = True
            watcher.send_signal(signal.SIGINT)
            if await asyncio.wait_for(watcher.wait(), 15) != 130:
                raise RuntimeError("Watcher did not stop on interruption")
            if second.is_running() or (await diagnose_http(directory))["state"] != "unreachable":
                raise RuntimeError("Server remained after watcher shutdown")
            report["owned_child_stopped"] = True
            report["completed"] = True
        finally:
            if watcher is not None and watcher.returncode is None:
                watcher.send_signal(signal.SIGINT)
                try:
                    await asyncio.wait_for(watcher.wait(), 15)
                except TimeoutError:
                    watcher.kill()
                    await watcher.wait()
            for pid, created in children.items():
                try:
                    child = psutil.Process(pid)
                    if child.create_time() == created:
                        child.kill()
                        await asyncio.to_thread(child.wait, timeout=5)
                except psutil.NoSuchProcess:
                    pass
            if owner is not None:
                await asyncio.to_thread(owner.forget)
                report["native_credential_removed"] = True
    report["temporary_directory_removed"] = not directory.exists()
    receipt.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", required=True, type=Path)
    args = parser.parse_args()
    asyncio.run(verify(args.receipt))
