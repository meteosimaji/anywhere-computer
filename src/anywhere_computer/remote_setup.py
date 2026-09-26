"""Resumable interactive setup using the existing configuration and native vaults."""

import asyncio
import getpass
import json
import secrets
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Literal

from pydantic import JsonValue

from .authorization import LOCAL_ONLY_TOOLS
from .client_tokens import ClientCredentialError
from .cloudflare_tunnel import TunnelCredential, cloudflared_executable
from .credentials import has_interactive_input
from .device_router import ROUTER_TOOLS
from .engine import Engine
from .http_service import HTTPServiceConfig, load_http_config, save_http_config
from .locking import ProcessLock
from .owner_credentials import OwnerCredentials
from .runtime_launch import python_module_command
from .state import prepare_directory

CHATGPT_CLIENT = "anywhere-chatgpt"
CHATGPT_REDIRECT = "https://chatgpt.com/connector_platform_oauth_redirect"


def setup_commands(
    directory: Path, client_kind: Literal["native", "chatgpt"],
) -> dict[str, JsonValue]:
    """Copyable commands contain public paths only, with explicit shell quoting."""
    commands: dict[str, JsonValue] = {
        "shell": "PowerShell" if sys.platform == "win32" else "POSIX shell",
    }
    for name, action in (
        ("resume", "chatgpt-setup" if client_kind == "chatgpt" else "remote-setup"),
        ("start", "remote-watch"), ("diagnose", "remote-doctor"),
    ):
        arguments = python_module_command(
            "anywhere_computer.cli", action, "--state-dir", str(directory.resolve()),
        )
        # PowerShell single-quoted strings do not expand $, %, backticks or subexpressions.
        commands[name] = (
            "& " + " ".join("'" + argument.replace("'", "''") + "'" for argument in arguments)
            if sys.platform == "win32" else shlex.join(arguments)
        )
    return commands


async def setup_scopes(mode: str) -> frozenset[str]:
    if mode not in {"read-only", "files", "all"}:
        raise ValueError("Choose read-only, files, or all")
    # Persist the exact current catalog, not a wildcard that expands on updates.
    with tempfile.TemporaryDirectory(prefix="anywhere-setup-catalog-") as raw:
        # TemporaryDirectory creates its root with the platform default ACL.
        # Let the state layer create a private child instead of adopting it.
        engine = Engine(Path(raw) / "catalog-state")
        try:
            return frozenset(
                name for name, tool in engine.tools.items()
                if name not in LOCAL_ONLY_TOOLS and (
                    mode == "all" or (mode == "read-only" and tool.read_only)
                    or (mode == "files" and name.startswith((
                        "files_", "directories_", "documents_", "upload_", "download_",
                        "search_", "operations_", "computer_status", "workspace_open",
                    )))
                )
            ) | (ROUTER_TOOLS if mode == "all" else frozenset())
        finally:
            await engine.close()


async def plan_remote_setup(
    *, resource: str, owner: str = "owner", client: str = "anywhere-native",
    port: int = 8768, mode: str = "read-only", redirects: frozenset[str] | None = None,
) -> HTTPServiceConfig:
    """Create an immutable, validated plan without credentials or persistent setup.

    Native setup screens and the CLI share this entry point. Commit the returned
    plan with save_http_config; do not recompute permissions after displaying it.
    """
    return HTTPServiceConfig(
        resource=resource, owner=owner, client=client, port=port,
        device=secrets.token_hex(16), scopes=await setup_scopes(mode),
        redirects=redirects if redirects is not None else frozenset({
            "http://127.0.0.1/oauth/callback", "http://[::1]/oauth/callback",
        }),
    )


def setup_remote(
    directory: Path, *, client_kind: Literal["native", "chatgpt"] = "native",
) -> dict[str, JsonValue]:
    """Save each completed step once; reruns preserve existing authorization."""
    if not has_interactive_input():
        raise ValueError("Remote setup requires an interactive terminal")
    if client_kind not in {"native", "chatgpt"}:
        raise ValueError("Unsupported setup client")
    prepare_directory(directory)
    with ProcessLock(directory / "remote-setup.lock"):
        print("Remote setup. Completed steps are preserved when you rerun this command.")
        destination = directory / "http-server"
        if destination.exists() or destination.is_symlink():
            config = load_http_config(directory)
            if client_kind == "chatgpt" and (
                config.client != CHATGPT_CLIENT
                or config.redirects != frozenset({CHATGPT_REDIRECT})
            ):
                raise ValueError(
                    "Existing configuration belongs to another client; it was preserved. "
                    "Use a separate state directory for ChatGPT setup."
                )
        else:
            resource = input("Public HTTPS address ending in /mcp: ").strip()
            if client_kind == "chatgpt":
                owner_name, client, port = "owner", CHATGPT_CLIENT, 8768
            else:
                owner_name = input("Owner identifier [owner]: ").strip() or "owner"
                client = (
                    input("OAuth client identifier [anywhere-native]: ").strip()
                    or "anywhere-native"
                )
                port = int(input("Loopback port [8768]: ").strip() or "8768")
            print("Access: read-only; files (file changes); all (includes running commands).")
            mode = input("Access [read-only]: ").strip().casefold() or "read-only"
            callbacks = [CHATGPT_REDIRECT] if client_kind == "chatgpt" else input(
                "OAuth callback URLs (space-separated; Enter for native loopback): "
            ).split()
            plan = asyncio.run(plan_remote_setup(
                resource=resource, owner=owner_name, client=client, port=port, mode=mode,
                redirects=frozenset(callbacks) if callbacks else None,
            ))
            config = asyncio.run(save_http_config(directory, plan))
        print(json.dumps({"resource": config.resource, "owner": config.owner,
                          "client": config.client, "port": config.port,
                          "allowed_tools": sorted(config.scopes)}, indent=2))
        owner = OwnerCredentials(directory, resource=config.resource, owner=config.owner)
        if not owner.is_initialized():
            password = getpass.getpass("Owner password (at least 8 characters, hidden): ")
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
        result: dict[str, JsonValue] = {
            "commands": setup_commands(directory, client_kind),
            "configuration_saved": True,
            "owner_initialized": True,
            "tunnel_credential_saved": installed,
            "connector_executable_available": executable,
            "local_setup_complete": installed and executable,
            "public_reachability": "unverified",
            "service_started": False,
            "next_step": (
                "Install cloudflared 2025.4.0 or newer, then run commands.resume."
                if not executable else
                "Run commands.resume to save the tunnel token." if not installed else
                "Check the provider route, then run commands.start. "
                "Leave that terminal open; use commands.diagnose in another terminal."
            ),
        }
        if client_kind == "chatgpt":
            result["chatgpt_connection"] = {
                "mcp_url": config.resource, "authentication": "OAuth",
                "client_id": CHATGPT_CLIENT, "token_endpoint_auth_method": "none",
                "redirect_uri": CHATGPT_REDIRECT, "client_secret_required": False,
                "connected": False,
                "instructions": "After starting the service and verifying public HTTPS, "
                "add this MCP in ChatGPT Developer mode using the predefined client ID. "
                "Complete owner login and consent. Saved setup alone is not a connection.",
            }
        return result
