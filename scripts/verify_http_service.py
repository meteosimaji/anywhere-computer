"""Explicit local CLI service lifecycle probe with disposable native-keyring data."""

import argparse
import asyncio
import hashlib
import http.client
import json
import secrets
import signal
import socket
import sys
import tempfile
import time
from pathlib import Path

from anywhere_computer.engine import runtime_identity
from anywhere_computer.http_service import configure_http
from anywhere_computer.owner_credentials import OwnerCredentials


def metadata(port):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request("GET", "/.well-known/oauth-protected-resource")
        response = connection.getresponse()
        raw = response.read(16385)
        if response.status != 200 or len(raw) > 16384:
            raise RuntimeError("HTTP service metadata failed")
        return json.loads(raw)
    finally:
        connection.close()


async def verify(receipt):
    if sys.platform == "win32":
        raise RuntimeError("This CLI signal probe currently requires a POSIX host")
    report = {
        "completed": False,
        "started_at": time.time(),
        "runtime_id": runtime_identity(),
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "scope": "Local loopback CLI lifecycle on this host; no public endpoint",
    }
    process = None
    owner = None
    with tempfile.TemporaryDirectory(prefix="anywhere-service-") as temporary:
        directory = Path(temporary)
        try:
            with socket.socket() as reserved:
                reserved.bind(("127.0.0.1", 0))
                port = reserved.getsockname()[1]
            resource = "https://disposable-service.example/mcp"
            config = await configure_http(
                directory,
                resource=resource,
                owner="probe-owner",
                client="probe-client",
                port=port,
                scopes=frozenset({"files_read"}),
                redirects=frozenset(
                    {"http://127.0.0.1/oauth/callback", "http://[::1]/oauth/callback"}
                ),
            )
            owner = OwnerCredentials(directory, resource=resource, owner=config.owner)
            await asyncio.to_thread(owner.initialize, secrets.token_urlsafe(32))

            async def launch():
                child = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-m",
                    "anywhere_computer.cli",
                    "http-serve",
                    "--state-dir",
                    str(directory),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                return child

            async def ready(child):
                assert child.stdout is not None
                line = await asyncio.wait_for(child.stdout.readline(), 15)
                if not line:
                    raise RuntimeError("Service exited before readiness")
                state = json.loads(line)
                if state.get("http_listening") != f"http://127.0.0.1:{port}":
                    raise RuntimeError("Service announced an unexpected address")
                if state.get("public_reachability") != "unverified":
                    raise RuntimeError("Service overstated public readiness")
                result = await asyncio.to_thread(metadata, port)
                if result.get("resource") != resource:
                    raise RuntimeError("Service lost its resource binding")

            async def stop(child):
                child.send_signal(signal.SIGINT)
                code = await asyncio.wait_for(child.wait(), 15)
                if code != 130:
                    raise RuntimeError("Service did not exit cleanly after interruption")

            process = await launch()
            await ready(process)
            report["cli_started"] = True
            duplicate = await launch()
            try:
                duplicate_code = await asyncio.wait_for(duplicate.wait(), 15)
                if duplicate_code != 1:
                    raise RuntimeError("Duplicate service was not rejected")
            finally:
                if duplicate.returncode is None:
                    duplicate.kill()
                    await duplicate.wait()
            report["duplicate_start_rejected"] = True
            await stop(process)
            report["graceful_stop"] = True
            process = await launch()
            await ready(process)
            report["same_port_restart"] = True
            revoker = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "anywhere_computer.cli",
                "http-revoke",
                "--state-dir",
                str(directory),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                output, _ = await asyncio.wait_for(revoker.communicate(), 15)
                if revoker.returncode != 0 or json.loads(output) != {"http_device_revoked": True}:
                    raise RuntimeError("Live revoke command failed")
            finally:
                if revoker.returncode is None:
                    revoker.kill()
                    await revoker.wait()
            report["live_revoke_command"] = True
            await stop(process)
            report["completed"] = True
        finally:
            if process and process.returncode is None:
                process.kill()
                await process.wait()
            if owner:
                try:
                    await asyncio.to_thread(owner.forget)
                    report["owner_keyring_removed"] = True
                except Exception:
                    report["owner_keyring_removed"] = False
                    report["completed"] = False
            report["process_stopped"] = process is None or process.returncode is not None
            report["finished_at"] = time.time()
            receipt.parent.mkdir(parents=True, exist_ok=True)
            receipt.write_text(json.dumps(report, indent=2), encoding="utf-8")
    report["temporary_files_removed"] = not directory.exists()
    receipt.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    if not report["completed"]:
        raise RuntimeError("Service probe did not complete")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--receipt", type=Path, default=Path("dist/http-service-verification.json"))
    arguments = parser.parse_args()
    try:
        asyncio.run(verify(arguments.receipt))
    except Exception as error:
        print(json.dumps({"failed": True, "failure_type": type(error).__name__}), file=sys.stderr)
        raise SystemExit(1) from None
