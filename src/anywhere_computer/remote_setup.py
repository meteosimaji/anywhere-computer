"""Resumable interactive setup using the existing configuration and native vaults."""

import asyncio
import getpass
import json
import subprocess
import tempfile
from pathlib import Path

from pydantic import JsonValue

from .authorization import LOCAL_ONLY_TOOLS
from .client_tokens import ClientCredentialError
from .cloudflare_tunnel import TunnelCredential, cloudflared_executable
from .credentials import has_interactive_input
from .engine import Engine
from .http_service import configure_http, load_http_config
from .locking import ProcessLock
from .owner_credentials import OwnerCredentials
from .state import prepare_directory


async def setup_scopes(mode: str) -> frozenset[str]:
    if mode not in {"read-only", "files", "all"}:
        raise ValueError("Choose read-only, files, or all")
    # Persist the exact current catalog, not a wildcard that expands on updates.
    with tempfile.TemporaryDirectory(prefix="anywhere-setup-catalog-") as raw:
        engine = Engine(Path(raw))
        try:
            return frozenset(
                name for name, tool in engine.tools.items()
                if name not in LOCAL_ONLY_TOOLS and (
                    mode == "all" or (mode == "read-only" and tool.read_only)
                    or (mode == "files" and name.startswith((
                        "files_", "directories_", "documents_", "upload_", "download_",
                        "search_", "operations_", "computer_status",
                    )))
                )
            )
        finally:
            await engine.close()


def setup_remote(directory: Path) -> dict[str, JsonValue]:
    """Save each completed step once; reruns preserve existing authorization."""
    if not has_interactive_input():
        raise ValueError("Remote setup requires an interactive terminal")
    prepare_directory(directory)
    with ProcessLock(directory / "remote-setup.lock"):
        print("Remote setup. Completed steps are preserved when you rerun this command.")
        destination = directory / "http-server"
        if destination.exists() or destination.is_symlink():
            config = load_http_config(directory)
        else:
            resource = input("Public HTTPS address ending in /mcp: ").strip()
            owner_name = input("Owner identifier [owner]: ").strip() or "owner"
            client = (
                input("OAuth client identifier [anywhere-native]: ").strip() or "anywhere-native"
            )
            port = int(input("Loopback port [8768]: ").strip() or "8768")
            print("Access: read-only; files (file changes); all (includes running commands).")
            mode = input("Access [read-only]: ").strip().casefold() or "read-only"
            scopes = asyncio.run(setup_scopes(mode))
            callbacks = input(
                "OAuth callback URLs (space-separated; Enter for native loopback): "
            ).split()
            config = asyncio.run(configure_http(
                directory, resource=resource, owner=owner_name, client=client, port=port,
                scopes=scopes,
                redirects=frozenset(callbacks or [
                    "http://127.0.0.1/oauth/callback", "http://[::1]/oauth/callback",
                ]),
            ))
        print(json.dumps({"resource": config.resource, "owner": config.owner,
                          "client": config.client, "port": config.port,
                          "allowed_tools": sorted(config.scopes)}, indent=2))
        owner = OwnerCredentials(directory, resource=config.resource, owner=config.owner)
        if not owner.is_initialized():
            password = getpass.getpass("Owner password (at least 16 characters, hidden): ")
            if password != getpass.getpass("Confirm owner password (hidden): "):
                raise ValueError("Owner passwords did not match; rerun remote-setup to resume")
            owner.initialize(password)
            if not owner.verify(password):
                raise ClientCredentialError("Owner credential readback could not be verified")
            password = ""
        credential = TunnelCredential(directory)
        if not credential.is_installed():
            token = getpass.getpass("Tunnel token (hidden; Enter to configure later): ").strip()
            if token:
                credential.install(token, replace=False)
            token = ""
        installed = credential.is_installed()
        try:
            cloudflared_executable()
            executable = True
        except (OSError, RuntimeError, subprocess.TimeoutExpired):
            executable = False
        return {
            "configuration_saved": True,
            "owner_initialized": True,
            "tunnel_credential_saved": installed,
            "connector_executable_available": executable,
            "local_setup_complete": installed and executable,
            "public_reachability": "unverified",
            "service_started": False,
            "next_step": (
                "Install cloudflared 2025.4.0 or newer, then rerun remote-setup."
                if not executable else
                "Rerun remote-setup to save the tunnel token." if not installed else
                "Check the provider route, then run remote-watch with this state directory."
            ),
        }
