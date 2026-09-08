"""One foreground owner for a loopback HTTP service and its outbound connector."""

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from threading import Event

import psutil

from .client_tokens import CredentialStoreUnavailable
from .cloudflare_tunnel import TunnelCredential, cloudflared_executable
from .http_service import http_service, load_http_config
from .http_supervisor import _stop_child, supervise
from .locking import ProcessLock
from .owner_credentials import OwnerCredentials


def stop_remote_connector(child: subprocess.Popen[bytes]) -> None:
    """Observe descendants before stopping their launcher, then verify cleanup."""
    descendants: list[psutil.Process] = []
    try:
        if child.poll() is None:
            descendants = psutil.Process(child.pid).children(recursive=True)
    except psutil.NoSuchProcess:
        pass
    finally:
        try:
            if child.stdin is not None:
                child.stdin.close()
            _stop_child(child)
        finally:
            failed = False
            for descendant in reversed(descendants):
                try:
                    # psutil's Process retains its creation identity and checks
                    # PID reuse before sending signals, including after reparenting.
                    if not descendant.is_running():
                        continue
                    descendant.terminate()
                    try:
                        descendant.wait(timeout=5)
                    except psutil.TimeoutExpired:
                        descendant.kill()
                        descendant.wait(timeout=5)
                except psutil.NoSuchProcess:
                    pass
                except psutil.Error:
                    failed = True
            if failed:
                raise RuntimeError("Owned connector descendants did not stop cleanly")


async def serve_remote(
    directory: Path, *, stop: Event | None = None, connector: str | None = None,
) -> int:
    """Bind HTTP before launching the connector; unwind both on every normal exit.

    The connector retains its own bounded restart policy. This process does not
    restart itself after a crash and is not an OS background service installer.
    """
    credential = TunnelCredential(directory)
    executable = (cloudflared_executable() if connector is None
                  else cloudflared_executable(connector))
    connector_arguments = [] if connector is None else ["--connector", executable]
    # The HTTP watcher and combined owner are mutually exclusive. The service
    # context itself owns http-server.lock, also excluding standalone http-serve.
    with ProcessLock(directory / "http-watch.lock"):
        with ProcessLock(credential.lock):
            credential.read()
        async with http_service(directory) as running:
            print(json.dumps({
                "remote_http_listening": f"http://127.0.0.1:{running.config.port}",
                "resource": running.config.resource,
                "public_reachability": "unverified",
            }), flush=True)
            # No token crosses argv, environment, or stdout. The child rechecks
            # its native credential under its lifetime lock. If another runner
            # wins the preflight/start race, this child fails and HTTP unwinds.
            flags = 0
            if sys.platform == "win32":
                flags = subprocess.CREATE_NEW_PROCESS_GROUP
            child = subprocess.Popen(
                [sys.executable, "-m", "anywhere_computer.cli", "tunnel-run",
                 "--state-dir", str(directory.resolve()), "--watch-parent", *connector_arguments],
                stdin=subprocess.PIPE,
                start_new_session=os.name != "nt",
                creationflags=flags,
            )
            try:
                # Polling avoids an uncancellable executor thread blocked in
                # wait(), which would otherwise delay asyncio.run shutdown.
                while child.poll() is None:
                    if stop is not None and stop.is_set():
                        return 130
                    await asyncio.sleep(0.1)
                code = child.returncode
                assert code is not None
                print(json.dumps({"remote_connector_exited": code}), flush=True)
                return code if code >= 0 else 1
            finally:
                # Connector shutdown precedes HTTP shutdown. SIGINT/Ctrl+Break
                # lets the runner clean up its own cloudflared child first.
                stop_remote_connector(child)


def wait_for_remote_credentials(owner: OwnerCredentials, credential: TunnelCredential) -> None:
    """Retry failed reads on the selected stores; never initialize or switch stores."""
    for attempt in range(6):
        try:
            owner.ensure_initialized()
            credential.read()
            return
        except CredentialStoreUnavailable:
            if attempt == 5:
                raise
            delay = 2**attempt
            print(json.dumps({
                "remote_startup_state": "credential_store_unavailable",
                "retry_attempt": attempt + 1,
                "retry_in_seconds": delay,
            }), flush=True)
            time.sleep(delay)


def watch_remote(directory: Path, *, connector: str | None = None) -> int:
    """Restart a failed combined service with a bounded budget and owner pipe."""
    config = load_http_config(directory)
    owner = OwnerCredentials(directory, resource=config.resource, owner=config.owner)
    credential = TunnelCredential(directory)
    executable = (cloudflared_executable() if connector is None
                  else cloudflared_executable(connector))
    connector_arguments = [] if connector is None else ["--connector", executable]
    with ProcessLock(directory / "remote-watch.lock"):
        prior = signal.signal(signal.SIGTERM, signal.default_int_handler)
        try:
            with (
                ProcessLock(directory / "http-watch.lock"), ProcessLock(credential.lock),
                ProcessLock(directory / "http-server.lock"),
            ):
                wait_for_remote_credentials(owner, credential)
            return supervise(
                [sys.executable, "-m", "anywhere_computer.cli", "remote-serve",
                 "--state-dir", str(directory.resolve()), "--watch-parent", *connector_arguments],
                parent_pipe=True,
            )
        finally:
            signal.signal(signal.SIGTERM, prior)
