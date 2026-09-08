"""One entry point for setup, connection and diagnosis on every supported OS."""

import argparse
import asyncio
import json
import sys
from pathlib import Path

from .connection import ensure_agent, exchange, serve
from .devices import DeviceStore
from .mcp_server import run_mcp
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
    args = parser.parse_args()
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
        if args.command == "remote-mcp":
            raise SystemExit(run_ssh_mcp(args.ssh_host))
        elif args.command == "serve":
            asyncio.run(serve(directory))
        elif args.command == "start":
            print(json.dumps(ensure_agent(directory), indent=2))
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
    except (OSError, ValueError, RuntimeError, TimeoutError) as error:
        print(f"Anywhere Computer: {error}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
