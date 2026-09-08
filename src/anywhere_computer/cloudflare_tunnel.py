"""Optional outbound tunnel adapter; the HTTP/MCP core is provider independent."""

import hashlib
import hmac
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from threading import Event

from .client_tokens import ClientCredentialError, CredentialVault
from .credentials import SERVICE, secure_backend
from .http_service import load_http_config
from .http_supervisor import _stop_child
from .locking import ProcessLock
from .secret_pipe import SecretPipe, SecretPipeCleanupError


class TunnelCredential:
    def __init__(self, directory: Path, *, vault: CredentialVault | None = None) -> None:
        config = load_http_config(directory)
        self.directory = directory.resolve()
        self.resource = config.resource
        binding = json.dumps([str(self.directory), config.resource]).encode()
        self.account = "cloudflare-tunnel-" + hashlib.sha256(binding).hexdigest()
        self.lock = self.directory / "cloudflare-tunnel.lock"
        self.vault = vault if vault is not None else secure_backend()

    @staticmethod
    def validate(token: str) -> None:
        # Treat the provider's token as opaque. Do not decode, print or describe its fields.
        if not re.fullmatch(r"[A-Za-z0-9+/=_-]{16,4096}", token):
            raise ValueError("Invalid tunnel credential format")

    def read(self) -> str:
        try:
            token = self.vault.get_password(SERVICE, self.account)
        except Exception:
            raise ClientCredentialError("Tunnel credential store is unavailable") from None
        if token is None:
            raise ClientCredentialError("Tunnel credential is missing; run tunnel-token")
        self.validate(token)
        return token

    def is_installed(self) -> bool:
        try:
            token = self.vault.get_password(SERVICE, self.account)
        except Exception:
            raise ClientCredentialError("Tunnel credential store is unavailable") from None
        if token is None:
            return False
        self.validate(token)
        return True

    def install(self, token: str, *, replace: bool = True) -> None:
        self.validate(token)
        with ProcessLock(self.lock):
            if not replace and self.is_installed():
                raise ClientCredentialError(
                    "Tunnel credentials already exist; setup preserves them"
                )
            try:
                self.vault.set_password(SERVICE, self.account, token)
            except Exception:
                pass  # Read back an uncertain write once; never retry or restore an old token.
            try:
                installed = self.read()
            except (ClientCredentialError, ValueError):
                raise ClientCredentialError(
                    "Tunnel credential save could not be verified"
                ) from None
            if not hmac.compare_digest(installed, token):
                raise ClientCredentialError("Tunnel credential save could not be verified")

    def forget(self) -> None:
        with ProcessLock(self.lock):
            try:
                if self.vault.get_password(SERVICE, self.account) is not None:
                    self.vault.delete_password(SERVICE, self.account)
                if self.vault.get_password(SERVICE, self.account) is not None:
                    raise RuntimeError
            except Exception:
                raise ClientCredentialError(
                    "Tunnel credential removal could not be verified"
                ) from None


def _tunnel_environment() -> dict[str, str]:
    return {
        name: value for name, value in os.environ.items()
        if not name.upper().startswith(("TUNNEL_", "CF_TUNNEL_"))
    }


def cloudflared_executable(executable: str | None = None) -> str:
    if executable is None:
        executable = shutil.which("cloudflared")
    elif not Path(executable).is_absolute() or any(
        ord(c) < 32 or ord(c) == 127 for c in executable
    ):
        raise ValueError("Connector executable must be an absolute path without control characters")
    if executable is None:
        raise RuntimeError("Install the optional cloudflared binary before running tunnel-run")
    completed = subprocess.run(
        [executable, "--version"],
        capture_output=True,
        timeout=5,
        check=False,
        env=_tunnel_environment(),
    )
    match = re.search(rb"cloudflared version (\d{4})\.(\d{1,2})\.(\d{1,2})", completed.stdout)
    if completed.returncode or match is None or tuple(map(int, match.groups())) < (2025, 4, 0):
        raise RuntimeError("The tunnel adapter requires cloudflared 2025.4.0 or newer")
    return executable


def run_tunnel_child(
    executable: str, token: str, *, handoff_timeout: float = 15, stop: Event | None = None
) -> int:
    TunnelCredential.validate(token)
    with tempfile.TemporaryDirectory(prefix="anywhere-tunnel-") as raw:
        config_path = Path(raw) / "config.json"
        config_path.write_text("{}\n", encoding="ascii")
        with SecretPipe(token.encode("ascii")) as pipe:
            # Explicit config and filtered tunnel variables isolate existing cloudflared setups.
            flags = 0
            if sys.platform == "win32":
                flags = subprocess.CREATE_NEW_PROCESS_GROUP
            child = subprocess.Popen(
                [
                    executable,
                    "tunnel",
                    "--config",
                    str(config_path),
                    "--no-autoupdate",
                    "--metrics",
                    "127.0.0.1:0",
                    "run",
                    "--token-file",
                    pipe.path,
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=_tunnel_environment(),
                start_new_session=os.name != "nt",
                creationflags=flags,
            )
            token = ""
            try:
                print(json.dumps({"tunnel_child_started": child.pid}), flush=True)
                pipe.wait(handoff_timeout)
                # Receiving a token is not evidence of edge connectivity or public readiness.
                print(
                    json.dumps(
                        {"tunnel_credential_sent": True, "public_reachability": "unverified"}
                    ),
                    flush=True,
                )
                if stop is None:
                    return child.wait()
                while child.poll() is None:
                    if stop.wait(0.1):
                        return 130
                assert child.returncode is not None
                return child.returncode
            finally:
                _stop_child(child)


def run_tunnel(
    directory: Path, *, restart_limit: int = 5, stop: Event | None = None,
    connector: str | None = None,
) -> int:
    if not 0 <= restart_limit <= 5:
        raise ValueError("Invalid tunnel restart limit")
    credential = TunnelCredential(directory)
    executable = (cloudflared_executable() if connector is None
                  else cloudflared_executable(connector))
    with ProcessLock(credential.lock):
        credential.read()  # Fail before launching if native storage is unavailable.
        prior = signal.getsignal(signal.SIGTERM)

        def interrupted(signum: int, frame: object) -> None:
            raise KeyboardInterrupt

        signal.signal(signal.SIGTERM, interrupted)
        failures = 0
        try:
            while True:
                if stop is not None and stop.is_set():
                    return 130
                started = time.monotonic()
                try:
                    if stop is None:
                        code = run_tunnel_child(executable, credential.read())
                    else:
                        code = run_tunnel_child(executable, credential.read(), stop=stop)
                except (ClientCredentialError, SecretPipeCleanupError):
                    raise
                except (OSError, TimeoutError, RuntimeError):
                    # No provider output or secret-containing exception is forwarded.
                    code = 1
                if code in {0, 130}:
                    return code
                if time.monotonic() - started >= 300:
                    failures = 0
                if failures >= restart_limit:
                    print(json.dumps({"tunnel_stopped": "restart_limit"}), flush=True)
                    return 1
                delay = min(2**failures, 30)
                failures += 1
                print(
                    json.dumps(
                        {
                            "tunnel_restart_in_seconds": delay,
                            "restart_attempt": failures,
                            "exit_code": code,
                        }
                    ),
                    flush=True,
                )
                if stop is None:
                    time.sleep(delay)
                elif stop.wait(delay):
                    return 130
        finally:
            signal.signal(signal.SIGTERM, prior)
