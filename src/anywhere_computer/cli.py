"""One entry point for setup, connection and diagnosis on every supported OS."""

import argparse
import asyncio
import getpass
import json
import signal
import sys
from pathlib import Path

from .autostart import preview_startup
from .client_tokens import ClientTokens
from .cloudflare_tunnel import TunnelCredential, run_tunnel
from .connection import ensure_agent, exchange, serve
from .credentials import has_interactive_input
from .devices import DeviceStore
from .diagnostics import diagnose
from .http_client import run_http_mcp
from .http_diagnostics import diagnose_http, diagnose_remote
from .http_service import (
    configure_http,
    enable_http_device,
    http_authorization_status,
    load_http_config,
    revoke_http_device,
    serve_http,
)
from .http_supervisor import watch_http
from .mcp_server import run_mcp
from .native_login import login
from .owner_credentials import OwnerCredentials
from .parent_liveness import watch_parent_pipe
from .remote_service import serve_remote, watch_remote
from .remote_setup import setup_remote
from .ssh_transport import run_ssh_mcp
from .state import state_directory
from .transfer_admin import list_transfers, release_transfer


def main() -> None:
    parser = argparse.ArgumentParser(prog="anywhere", description="Anywhere Computer")
    parser.add_argument(
        "command",
        choices=[
            "start",
            "serve",
            "mcp",
            "status",
            "doctor",
            "stop",
            "remote-mcp",
            "http-mcp",
            "owner-init",
            "owner-change",
            "login",
            "http-configure",
            "http-serve",
            "http-watch",
            "remote-serve",
            "remote-watch",
            "remote-setup",
            "remote-doctor",
            "autostart-preview",
            "http-show",
            "http-doctor",
            "http-revoke",
            "http-enable",
            "http-auth-status",
            "tunnel-token",
            "tunnel-run",
            "tunnel-forget",
            "transfers",
            "transfer-release",
            "devices",
            "device-add",
            "device-add-http",
            "device-rename",
            "device-remove",
            "device-status",
        ],
    )
    parser.add_argument("--state-dir", type=Path, default=None)
    parser.add_argument("--ssh-host", help="An existing SSH host alias")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--device", help="Registered device ID")
    selection.add_argument("--device-name", help="Registered device display name")
    parser.add_argument("--name", help="Device display name")
    parser.add_argument("--resource", help="Authorized HTTPS /mcp resource")
    parser.add_argument("--client-id", help="Registered public OAuth client ID")
    parser.add_argument("--owner", help="Owner identifier for initial authentication setup")
    parser.add_argument("--scope", action="append", help="Tool to authorize (repeat per tool)")
    parser.add_argument("--port", type=int, help="Stable loopback HTTP port (default: 8768)")
    parser.add_argument("--redirect-uri", action="append", help="Registered OAuth callback URL")
    parser.add_argument(
        "--profile", help="Authorized connection profile in the OS credential store"
    )
    parser.add_argument("--transfer-area", choices=["local", "http"])
    parser.add_argument("--transfer-kind", choices=["upload", "download"])
    parser.add_argument("--storage-id", help="Local storage ID returned by transfers")
    parser.add_argument("--after", help="Pagination cursor returned by transfers")
    parser.add_argument("--limit", type=int, help="Transfer page size, 1–100")
    parser.add_argument("--probe-public", action="store_true",
                        help="Also probe configured HTTPS metadata (remote-doctor only)")
    parser.add_argument("--watch-parent", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.watch_parent and args.command not in {"remote-serve", "tunnel-run"}:
        parser.error("--watch-parent is only valid for supervised remote children")
    if args.probe_public and args.command != "remote-doctor":
        parser.error("--probe-public is only valid for remote-doctor")
    transfer_command = args.command in {"transfers", "transfer-release"}
    if not transfer_command and any(value is not None for value in (
        args.transfer_area, args.transfer_kind, args.storage_id, args.after, args.limit,
    )):
        parser.error("Transfer options are only valid for transfers and transfer-release")
    if transfer_command and not all((args.transfer_area, args.transfer_kind)):
        parser.error("Specify --transfer-area and --transfer-kind explicitly")
    if args.command == "transfer-release" and (
        not args.storage_id or args.after is not None or args.limit is not None
    ):
        parser.error("transfer-release requires --storage-id and does not accept pagination")
    if args.command == "transfers" and args.storage_id is not None:
        parser.error("Use --after for transfers pagination, not --storage-id")
    has_device = args.device is not None or args.device_name is not None
    if args.resource is not None and args.command not in {
        "http-mcp",
        "owner-init",
        "owner-change",
        "login",
        "http-configure",
        "device-add-http",
    }:
        parser.error("--resource is only valid for HTTP connection setup")
    if args.client_id is not None and args.command not in {
        "http-mcp",
        "login",
        "http-configure",
        "device-add-http",
    }:
        parser.error("--client-id is only valid for HTTP connection setup")
    if args.profile is not None and args.command not in {"http-mcp", "login", "device-add-http"}:
        parser.error("--profile is only valid for HTTP client setup")
    if args.scope is not None and args.command not in {"login", "http-configure"}:
        parser.error("--scope is only valid for login and http-configure")
    if args.command == "login" and not args.scope:
        parser.error("login requires at least one --scope tool")
    if args.owner is not None and args.command not in {
        "owner-init",
        "owner-change",
        "http-configure",
    }:
        parser.error("--owner is only valid for owner setup and HTTP configuration")
    if (
        args.port is not None or args.redirect_uri is not None
    ) and args.command != "http-configure":
        parser.error("--port and --redirect-uri are only valid for http-configure")
    if args.command == "http-configure" and not all(
        (args.resource, args.owner, args.client_id, args.scope)
    ):
        parser.error("http-configure requires --resource, --owner, --client-id and --scope")
    if args.command in {"owner-init", "owner-change"} and not all((args.resource, args.owner)):
        parser.error("Owner setup requires --resource and --owner")
    if args.command in {"http-mcp", "login"}:
        metadata = (args.resource, args.client_id, args.profile)
        if has_device and any(value is not None for value in metadata):
            parser.error("Device selection cannot be combined with HTTP connection metadata")
        if not has_device and not all(metadata):
            parser.error("Select a device or supply --resource, --client-id and --profile")
    if args.ssh_host is not None and args.command not in {"remote-mcp", "device-add"}:
        parser.error("--ssh-host is only valid for remote-mcp and device-add")
    if args.name is not None and args.command not in {
        "device-add",
        "device-add-http",
        "device-rename",
    }:
        parser.error("--name is only valid for device registration and rename")
    if has_device and args.command not in {
        "remote-mcp",
        "http-mcp",
        "login",
        "device-rename",
        "device-remove",
        "device-status",
    }:
        parser.error("Device selection is not valid for this command")
    if args.command == "remote-mcp" and (bool(args.ssh_host) == has_device):
        parser.error("remote-mcp requires an SSH host or a registered device")
    if args.command == "device-add" and not (args.name and args.ssh_host):
        parser.error("device-add requires --name and --ssh-host")
    if args.command == "device-add-http" and not all(
        (args.name, args.resource, args.client_id, args.profile)
    ):
        parser.error("device-add-http requires --name, --resource, --client-id and --profile")
    if args.command in {"device-rename", "device-remove", "device-status"} and not has_device:
        parser.error("This command requires --device or --device-name")
    if args.command == "device-rename" and not args.name:
        parser.error("device-rename requires --name")
    directory = (args.state_dir or state_directory()).resolve()
    try:
        if args.command.startswith("device") or has_device:
            store = DeviceStore(directory)
            try:
                if args.device_name is not None:
                    args.device = store.named(args.device_name)["device_id"]
                if args.command == "devices":
                    print(json.dumps({"devices": store.list()}, ensure_ascii=False, indent=2))
                elif args.command == "device-add":
                    print(
                        json.dumps(
                            store.add(args.name, args.ssh_host), ensure_ascii=False, indent=2
                        )
                    )
                elif args.command == "device-add-http":
                    print(
                        json.dumps(
                            store.add_http(args.name, args.resource, args.client_id, args.profile),
                            ensure_ascii=False,
                            indent=2,
                        )
                    )
                elif args.command == "device-rename":
                    print(
                        json.dumps(
                            store.rename(args.device, args.name), ensure_ascii=False, indent=2
                        )
                    )
                elif args.command == "device-remove":
                    store.remove(args.device)
                    print(json.dumps({"removed_device_id": args.device}))
                elif args.command == "device-status":
                    device = store.get(args.device)
                    observation = (
                        asyncio.run(store.probe_http(args.device))
                        if device["transport"] == "http"
                        else store.probe(args.device)
                    )
                    print(json.dumps(observation, ensure_ascii=False, indent=2))
                    if observation["state"] != "ready":
                        raise SystemExit(1)
                else:
                    device = store.get(args.device)
                    if args.command == "remote-mcp":
                        if device["transport"] != "ssh":
                            raise ValueError("remote-mcp requires an SSH device")
                        args.ssh_host = device["ssh_host"]
                    else:
                        if device["transport"] != "http":
                            raise ValueError("HTTP connections require an HTTP device")
                        args.resource = device["resource"]
                        args.client_id = device["client_id"]
                        args.profile = device["profile"]
            finally:
                store.close()
            if args.command.startswith("device"):
                return
        if args.command == "autostart-preview":
            print(json.dumps(preview_startup(directory), ensure_ascii=False, indent=2))
        elif args.command == "transfers":
            print(json.dumps(list_transfers(
                directory, area=args.transfer_area, kind=args.transfer_kind,
                after=args.after, limit=args.limit if args.limit is not None else 100,
            ), ensure_ascii=False, indent=2))
        elif args.command == "transfer-release":
            print(json.dumps(release_transfer(
                directory, area=args.transfer_area, kind=args.transfer_kind,
                storage_id=args.storage_id,
            ), ensure_ascii=False, indent=2))
        elif args.command == "tunnel-token":
            if not has_interactive_input():
                raise ValueError("Tunnel token setup requires an interactive terminal")
            tunnel_credential = TunnelCredential(directory)
            tunnel_credential.install(getpass.getpass("Tunnel token (hidden): ").strip())
            print(json.dumps({"tunnel_credential_saved": True}))
        elif args.command == "tunnel-forget":
            TunnelCredential(directory).forget()
            print(json.dumps({"local_tunnel_credential_removed": True,
                              "provider_token_revoked": False}))
        elif args.command == "tunnel-run":
            if sys.platform == "win32":
                signal.signal(signal.SIGBREAK, signal.default_int_handler)
            stop = watch_parent_pipe() if args.watch_parent else None
            raise SystemExit(run_tunnel(directory, stop=stop))
        elif args.command == "http-configure":
            config = asyncio.run(
                configure_http(
                    directory,
                    resource=args.resource,
                    owner=args.owner,
                    client=args.client_id,
                    port=args.port if args.port is not None else 8768,
                    scopes=frozenset(args.scope),
                    redirects=frozenset(
                        args.redirect_uri
                        or ["http://127.0.0.1/oauth/callback", "http://[::1]/oauth/callback"]
                    ),
                )
            )
            print(config.model_dump_json(indent=2))
        elif args.command == "remote-doctor":
            diagnosis = asyncio.run(diagnose_remote(directory, probe_public=args.probe_public))
            print(json.dumps(diagnosis, ensure_ascii=False, indent=2))
            if diagnosis["state"] not in {
                "local_metadata_reachable", "local_and_public_metadata_reachable"
            }:
                raise SystemExit(1)
        elif args.command == "http-doctor":
            diagnosis = asyncio.run(diagnose_http(directory))
            print(json.dumps(diagnosis, ensure_ascii=False, indent=2))
            if diagnosis["state"] != "metadata_reachable":
                raise SystemExit(1)
        elif args.command == "http-show":
            print(load_http_config(directory).model_dump_json(indent=2))
        elif args.command == "http-revoke":
            revoke_http_device(directory)
            print(json.dumps({"http_device_revoked": True}))
        elif args.command == "http-enable":
            changed = enable_http_device(directory)
            print(json.dumps({"http_device_enabled": True, "changed": changed}))
        elif args.command == "http-auth-status":
            print(json.dumps(http_authorization_status(directory)))
        elif args.command == "http-watch":
            raise SystemExit(watch_http(directory))
        elif args.command == "remote-setup":
            print(json.dumps(setup_remote(directory), ensure_ascii=False, indent=2))
        elif args.command == "remote-watch":
            if sys.platform == "win32":
                signal.signal(signal.SIGBREAK, signal.default_int_handler)
            raise SystemExit(watch_remote(directory))
        elif args.command == "remote-serve":
            prior_terminate = signal.signal(signal.SIGTERM, signal.default_int_handler)
            try:
                if sys.platform == "win32":
                    signal.signal(signal.SIGBREAK, signal.default_int_handler)
                stop = watch_parent_pipe() if args.watch_parent else None
                raise SystemExit(asyncio.run(serve_remote(directory, stop=stop)))
            finally:
                signal.signal(signal.SIGTERM, prior_terminate)
        elif args.command == "http-serve":
            if sys.platform == "win32":
                signal.signal(signal.SIGBREAK, signal.default_int_handler)
            asyncio.run(serve_http(directory))
        elif args.command in {"owner-init", "owner-change"}:
            if not has_interactive_input():
                raise ValueError("Owner setup requires an interactive terminal")
            owner_credentials = OwnerCredentials(
                directory, resource=args.resource, owner=args.owner
            )
            current = (
                getpass.getpass("Current owner password: ")
                if args.command == "owner-change"
                else None
            )
            password = getpass.getpass("Owner password (at least 16 characters): ")
            if password != getpass.getpass("Confirm owner password: "):
                raise ValueError("Owner passwords did not match")
            if current is None:
                owner_credentials.initialize(password)
                print(json.dumps({"owner_initialized": True}))
            else:
                owner_credentials.change_password(current, password)
                print(json.dumps({"owner_password_changed": True}))
        elif args.command in {"http-mcp", "login"}:
            tokens = ClientTokens(
                directory, resource=args.resource, client=args.client_id, profile=args.profile
            )
            if args.command == "login":
                print("ブラウザーで接続を承認してください。", file=sys.stderr)
                asyncio.run(login(tokens, frozenset(args.scope)))
                print(json.dumps({"authorized": True, "profile": args.profile}))
            else:
                asyncio.run(run_http_mcp(tokens))
        elif args.command == "remote-mcp":
            raise SystemExit(run_ssh_mcp(args.ssh_host))
        elif args.command == "serve":
            asyncio.run(serve(directory))
        elif args.command == "start":
            print(json.dumps(ensure_agent(directory), indent=2))
        elif args.command == "doctor":
            diagnosis = asyncio.run(diagnose(directory))
            print(json.dumps(diagnosis, ensure_ascii=False, indent=2))
            if diagnosis["state"] != "ready":
                raise SystemExit(1)
        elif args.command == "mcp":
            ensure_agent(directory)
            asyncio.run(run_mcp(directory))
        elif args.command == "stop":
            reply = asyncio.run(exchange(directory, "__stop"))
            print(reply.model_dump_json(indent=2))
            if reply.state != "completed":
                raise SystemExit(1)
        else:
            reply = asyncio.run(exchange(directory, "__status", timeout=3))
            print(reply.model_dump_json(indent=2))
    except (KeyboardInterrupt, EOFError):
        raise SystemExit(130) from None
    except (OSError, ValueError, RuntimeError, TimeoutError) as error:
        print(f"Anywhere Computer: {error}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
