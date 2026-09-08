"""One foreground owner for a loopback HTTP service and its outbound connector."""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

from .cloudflare_tunnel import TunnelCredential, cloudflared_executable
from .http_service import http_service
from .http_supervisor import _stop_child
from .locking import ProcessLock


async def serve_remote(directory: Path) -> int:
    """Bind HTTP before launching the connector; unwind both on every normal exit.

    The connector retains its own bounded restart policy. This process does not
    restart itself after a crash and is not an OS background service installer.
    """
    credential = TunnelCredential(directory)
    cloudflared_executable()
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
                 "--state-dir", str(directory.resolve())],
                stdin=subprocess.DEVNULL,
                start_new_session=os.name != "nt",
                creationflags=flags,
            )
            try:
                # Polling avoids an uncancellable executor thread blocked in
                # wait(), which would otherwise delay asyncio.run shutdown.
                while child.poll() is None:
                    await asyncio.sleep(0.1)
                code = child.returncode
                assert code is not None
                print(json.dumps({"remote_connector_exited": code}), flush=True)
                return code if code >= 0 else 1
            finally:
                # Connector shutdown precedes HTTP shutdown. SIGINT/Ctrl+Break
                # lets the runner clean up its own cloudflared child first.
                _stop_child(child)
