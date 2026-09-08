"""One entry point for setup, connection and diagnosis on every supported OS."""

import argparse
import asyncio
import json
import sys
from pathlib import Path

from .connection import ensure_agent, exchange, serve
from .mcp_server import run_mcp
from .state import state_directory


def main() -> None:
    parser = argparse.ArgumentParser(prog="anywhere", description="Anywhere Computer")
    parser.add_argument("command", choices=["start", "serve", "mcp", "status", "doctor", "stop"])
    parser.add_argument("--state-dir", type=Path, default=None)
    args = parser.parse_args()
    directory = (args.state_dir or state_directory()).resolve()
    try:
        if args.command == "serve":
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
