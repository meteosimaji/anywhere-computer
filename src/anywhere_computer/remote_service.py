"""One foreground owner for a loopback HTTP service and its outbound connector."""

import asyncio
import os
import signal
import subprocess
import sys
import time
from contextlib import ExitStack
from pathlib import Path
from threading import Event

import psutil
from keyring.errors import KeyringError

from .client_tokens import ClientCredentialError, CredentialStoreUnavailable, CredentialVault
from .cloudflare_tunnel import TunnelCredential, cloudflared_executable
from .credentials import secure_backend
from .http_service import http_service, load_http_config
from .http_supervisor import _stop_child, supervise
from .locking import ProcessLock
from .owner_credentials import OwnerCredentials
from .remote_health import monitor_public_health
from .runtime_launch import python_module_command
from .state import prepare_directory
from .watch_status import WatchEvent, lifecycle_print, save_watch_observation


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
            lifecycle_print({
                "remote_http_listening": f"http://127.0.0.1:{running.config.port}",
                "resource": running.config.resource,
                "public_reachability": "unverified",
            })
            # No token crosses argv, environment, or stdout. The child rechecks
            # its native credential under its lifetime lock. If another runner
            # wins the preflight/start race, this child fails and HTTP unwinds.
            flags = 0
            if sys.platform == "win32":
                flags = subprocess.CREATE_NEW_PROCESS_GROUP
            child = subprocess.Popen(
                python_module_command(
                    "anywhere_computer.cli", "tunnel-run", "--state-dir",
                    str(directory.resolve()), "--watch-parent", *connector_arguments,
                ),
                stdin=subprocess.PIPE,
                start_new_session=os.name != "nt",
                creationflags=flags,
            )
            health_task = asyncio.create_task(monitor_public_health(directory))
            try:
                # Polling avoids an uncancellable executor thread blocked in
                # wait(), which would otherwise delay asyncio.run shutdown.
                while child.poll() is None:
                    if stop is not None and stop.is_set():
                        return 130
                    await asyncio.sleep(0.1)
                code = child.returncode
                assert code is not None
                lifecycle_print({"remote_connector_exited": code})
                return code if code >= 0 else 1
            finally:
                # Connector shutdown precedes HTTP shutdown. SIGINT/Ctrl+Break
                # lets the runner clean up its own cloudflared child first.
                health_task.cancel()
                try:
                    await health_task
                except asyncio.CancelledError:
                    pass
                finally:
                    stop_remote_connector(child)


def record_remote_startup(path: Path, event: WatchEvent, attempt: int | None = None) -> None:
    try:
        save_watch_observation(path, event, 0, 5, None, startup_attempt=attempt)
    except (OSError, ValueError):
        lifecycle_print({"watch_observation_saved": False})


def wait_for_remote_credentials(
    owner: OwnerCredentials, credential: TunnelCredential, *, status_path: Path | None = None,
) -> None:
    """Retry failed reads on the selected stores; never initialize or switch stores."""
    def observe_credentials(event: WatchEvent, attempt: int) -> None:
        if status_path is not None:
            record_remote_startup(status_path, event, attempt)

    for attempt in range(6):
        observe_credentials("credential_check", attempt + 1)
        try:
            owner.ensure_initialized()
            credential.read()
            observe_credentials("credentials_ready", attempt + 1)
            return
        except CredentialStoreUnavailable:
            observe_credentials("credential_store_unavailable", attempt + 1)
            if attempt == 5:
                raise
            delay = 2**attempt
            lifecycle_print({
                "remote_startup_state": "credential_store_unavailable",
                "retry_attempt": attempt + 1,
                "retry_in_seconds": delay,
            })
            try:
                time.sleep(delay)
            except KeyboardInterrupt:
                observe_credentials("interrupted", attempt + 1)
                raise
        except (ClientCredentialError, ValueError):
            observe_credentials("credential_rejected", attempt + 1)
            raise
        except KeyboardInterrupt:
            observe_credentials("interrupted", attempt + 1)
            raise


def _watch_remote_once(
    directory: Path, *, connector: str | None = None, vault: CredentialVault | None = None,
) -> int:
    """Restart a failed combined service with a bounded budget and owner pipe."""
    prepare_directory(directory)
    with ProcessLock(directory / "remote-watch.lock"):
        prior = signal.signal(signal.SIGTERM, signal.default_int_handler)
        try:
            stage: WatchEvent = "configuration_error"
            try:
                config = load_http_config(directory)
                stage = "credential_backend_error"
                owner = OwnerCredentials(directory, resource=config.resource, owner=config.owner,
                                         **({"vault": vault} if vault is not None else {}))
                credential = TunnelCredential(directory,
                                              **({"vault": vault} if vault is not None else {}))
                stage = "connector_check_error"
                executable = (cloudflared_executable() if connector is None
                              else cloudflared_executable(connector))
            except KeyboardInterrupt:
                record_remote_startup(directory / "remote-watch-status.json", "interrupted")
                raise
            except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired):
                record_remote_startup(directory / "remote-watch-status.json", stage)
                raise
            connector_arguments = [] if connector is None else ["--connector", executable]
            with ExitStack() as startup_locks:
                try:
                    for path in (directory / "http-watch.lock", credential.lock,
                                 directory / "http-server.lock"):
                        startup_locks.enter_context(ProcessLock(path))
                except TimeoutError:
                    record_remote_startup(
                        directory / "remote-watch-status.json", "startup_conflict",
                    )
                    raise
                wait_for_remote_credentials(
                    owner, credential, status_path=directory / "remote-watch-status.json",
                )
            return supervise(
                python_module_command(
                    "anywhere_computer.cli", "remote-serve", "--state-dir",
                    str(directory.resolve()), "--watch-parent", *connector_arguments,
                ),
                parent_pipe=True,
                status_path=directory / "remote-watch-status.json",
            )
        finally:
            signal.signal(signal.SIGTERM, prior)


def watch_remote(
    directory: Path, *, connector: str | None = None, persistent: bool = False,
    cooldown: float = 60,
) -> int:
    """OS-owned mode survives exhausted retry windows; uninstall is durable stop.

    Rejected configuration remains blocked without repeated credential prompts.
    Stop/restart after repairing configuration. Foreground behavior is unchanged.
    """
    if not persistent:
        return _watch_remote_once(directory, connector=connector)
    if cooldown < 1:
        raise ValueError("Persistent cooldown must be at least one second")
    prepare_directory(directory)
    with ProcessLock(directory / "persistent-watch.lock"):
        prior = signal.signal(signal.SIGTERM, signal.default_int_handler)
        vault: CredentialVault | None = None
        try:
            while True:
                try:
                    if vault is None:
                        try:
                            vault = secure_backend()
                        except (KeyringError, OSError):
                            record_remote_startup(
                                directory / "remote-watch-status.json", "credential_backend_error",
                            )
                            time.sleep(cooldown)
                            continue
                    code = _watch_remote_once(directory, connector=connector, vault=vault)
                    event: WatchEvent = "unexpected_child_exit" if code in {0, 130} else "cooldown"
                    record_remote_startup(directory / "remote-watch-status.json", event)
                except CredentialStoreUnavailable:
                    record_remote_startup(directory / "remote-watch-status.json", "cooldown")
                except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired):
                    # Do not recreate credentials or spin on permanent configuration errors.
                    record_remote_startup(directory / "remote-watch-status.json", "blocked")
                    while True:
                        time.sleep(cooldown)
                time.sleep(cooldown)
        except KeyboardInterrupt:
            record_remote_startup(directory / "remote-watch-status.json", "interrupted")
            return 130
        finally:
            signal.signal(signal.SIGTERM, prior)
