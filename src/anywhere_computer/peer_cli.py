"""Owner provisioning and dedicated local peer MCP stdio entry point."""

import argparse
import asyncio
import os
import secrets
import stat
import sys
import tempfile
from pathlib import Path

from .mcp_server import serve_stdio
from .peer_mailbox import Peer, PeerMailbox
from .peer_mcp import session


def _credential(path: Path) -> str:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("Peer credential must be a regular file")
    if os.name != "nt":
        # Windows' os typing omits getuid even when checking this POSIX branch.
        current_uid = getattr(os, "getuid")()  # noqa: B009
        if info.st_uid != current_uid or info.st_mode & 0o077:
            raise PermissionError("Peer credential must belong to this user with mode 0600")
    value = path.read_text(encoding="ascii").strip()
    if not value or len(value) > 256:
        raise ValueError("Invalid peer credential file")
    return value


def _publish_credential(path: Path) -> str:
    token = secrets.token_urlsafe(32)
    staged: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="ascii", dir=path.parent, prefix=".peer-", delete=False,
        ) as output:
            staged = Path(output.name)
            os.chmod(staged, 0o600)
            output.write(token + "\n")
            output.flush()
            os.fsync(output.fileno())
        try:
            os.link(staged, path)
        except FileExistsError:
            token = _credential(path)
        else:
            if os.name != "nt":
                directory_fd = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        return token
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)


async def _serve(directory: Path, credential_file: Path) -> None:
    token = _credential(credential_file)
    with PeerMailbox(directory) as mailbox:
        server = session(mailbox, token)
        mailbox.heartbeat(token)

        async def renew() -> None:
            while True:
                await asyncio.sleep(20)
                mailbox.heartbeat(token)

        task = asyncio.create_task(renew())
        try:
            await serve_stdio(server, sys.stdin.buffer, sys.stdout.buffer)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            mailbox.disconnect(token)


def main() -> None:
    parser = argparse.ArgumentParser(description="Local authenticated peer mailbox")
    parser.add_argument("--state-dir", type=Path, required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    enroll = commands.add_parser("enroll", help="Trusted owner provisioning on this computer")
    for name in ("peer-id", "owner", "account", "project", "runtime"):
        enroll.add_argument("--" + name, required=True)
    enroll.add_argument("--credential-file", type=Path, required=True)
    serve = commands.add_parser("serve", help="Serve peer tools over MCP stdio")
    serve.add_argument("--credential-file", type=Path, required=True)
    args = parser.parse_args()
    directory: Path = args.state_dir
    if not directory.is_absolute():
        parser.error("--state-dir must be absolute")
    if args.command == "serve":
        asyncio.run(_serve(directory, args.credential_file))
        return
    path: Path = args.credential_file
    if not path.is_absolute():
        parser.error("--credential-file must be absolute")
    peer = Peer(args.peer_id, args.owner, args.account, args.project, args.runtime)
    # Stage complete bytes on the same filesystem and publish with an exclusive
    # hard link. A crash before publication leaves no partial final credential;
    # a crash after publication can be reconciled by rerunning enrollment.
    token = _publish_credential(path)
    with PeerMailbox(directory) as mailbox:
        mailbox.enroll(peer, credential=token)
    print(path)


if __name__ == "__main__":
    main()
