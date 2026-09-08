"""One entry point for setup, connection and diagnosis on every supported OS."""

import argparse
import asyncio
import getpass
import json
import sys
from pathlib import Path

from .client_tokens import ClientTokens
from .connection import ensure_agent, exchange, serve
from .devices import DeviceStore
from .diagnostics import diagnose
from .http_client import run_http_mcp
from .http_service import configure_http, load_http_config, revoke_http_device, serve_http
from .mcp_server import run_mcp
from .native_login import login
from .owner_credentials import OwnerCredentials
from .ssh_transport import run_ssh_mcp
from .state import state_directory


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
            "login",
            "http-configure",
            "http-serve",
            "http-show",
            "http-revoke",
            "devices",
            "device-add",
            "device-rename",
            "device-remove",
            "device-status",
        ],
    )
    parser.add_argument("--state-dir", type=Path, default=None)
    parser.add_argument("--ssh-host", help="An existing SSH host alias")
    parser.add_argument("--device", help="Registered device ID")
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
    args = parser.parse_args()
    if args.resource is not None and args.command not in {
        "http-mcp",
        "owner-init",
        "login",
        "http-configure",
    }:
        parser.error("--resource is only valid for HTTP connection setup")
    if args.client_id is not None and args.command not in {"http-mcp", "login", "http-configure"}:
        parser.error("--client-id is only valid for http-mcp, login and http-configure")
    if args.profile is not None and args.command not in {"http-mcp", "login"}:
        parser.error("--profile is only valid for http-mcp and login")
    if args.scope is not None and args.command not in {"login", "http-configure"}:
        parser.error("--scope is only valid for login and http-configure")
    if args.command == "login" and not args.scope:
        parser.error("login requires at least one --scope tool")
    if args.owner is not None and args.command not in {"owner-init", "http-configure"}:
        parser.error("--owner is only valid for owner-init and http-configure")
    if (
        args.port is not None or args.redirect_uri is not None
    ) and args.command != "http-configure":
        parser.error("--port and --redirect-uri are only valid for http-configure")
    if args.command == "http-configure" and not all(
        (args.resource, args.owner, args.client_id, args.scope)
    ):
        parser.error("http-configure requires --resource, --owner, --client-id and --scope")
    if args.command == "owner-init" and not all((args.resource, args.owner)):
        parser.error("owner-init requires --resource and --owner")
    if args.command in {"http-mcp", "login"} and not all(
        (args.resource, args.client_id, args.profile)
    ):
        parser.error("http-mcp and login require --resource, --client-id and --profile")
    if args.ssh_host is not None and args.command not in {"remote-mcp", "device-add"}:
        parser.error("--ssh-host is only valid for remote-mcp and device-add")
    if args.name is not None and args.command not in {"device-add", "device-rename"}:
        parser.error("--name is only valid for device-add and device-rename")
    if args.device is not None and args.command not in {
        "remote-mcp",
        "device-rename",
        "device-remove",
        "device-status",
    }:
        parser.error("--device is not valid for this command")
    if args.command == "remote-mcp" and (bool(args.ssh_host) == bool(args.device)):
        parser.error("remote-mcp requires exactly one of --ssh-host or --device")
    if args.command == "device-add" and not (args.name and args.ssh_host):
        parser.error("device-add requires --name and --ssh-host")
    if args.command in {"device-rename", "device-remove", "device-status"} and not args.device:
        parser.error("This command requires --device")
    if args.command == "device-rename" and not args.name:
        parser.error("device-rename requires --name")
    directory = (args.state_dir or state_directory()).resolve()
    try:
        if args.command.startswith("device") or (args.command == "remote-mcp" and args.device):
            store = DeviceStore(directory)
            try:
                if args.command == "devices":
                    print(json.dumps({"devices": store.list()}, ensure_ascii=False, indent=2))
                elif args.command == "device-add":
                    print(
                        json.dumps(
                            store.add(args.name, args.ssh_host), ensure_ascii=False, indent=2
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
                    observation = store.probe(args.device)
                    print(json.dumps(observation, ensure_ascii=False, indent=2))
                    if observation["state"] != "ready":
                        raise SystemExit(1)
                else:
                    args.ssh_host = str(store.get(args.device)["ssh_host"])
            finally:
                store.close()
            if args.command != "remote-mcp":
                return
        if args.command == "http-configure":
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
        elif args.command == "http-show":
            print(load_http_config(directory).model_dump_json(indent=2))
        elif args.command == "http-revoke":
            revoke_http_device(directory)
            print(json.dumps({"http_device_revoked": True}))
        elif args.command == "http-serve":
            asyncio.run(serve_http(directory))
        elif args.command == "owner-init":
            if not sys.stdin.isatty():
                raise ValueError("Owner setup requires an interactive terminal")
            owner_credentials = OwnerCredentials(
                directory, resource=args.resource, owner=args.owner
            )
            password = getpass.getpass("Owner password (at least 16 characters): ")
            if password != getpass.getpass("Confirm owner password: "):
                raise ValueError("Owner passwords did not match")
            owner_credentials.initialize(password)
            print(json.dumps({"owner_initialized": True}))
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
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except (OSError, ValueError, RuntimeError, TimeoutError) as error:
        print(f"Anywhere Computer: {error}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
