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


def _require_private_peer_storage() -> None:
    if sys.platform == "win32":
        raise RuntimeError("Local peer messaging requires private file ACLs on Windows")


def _credential(path: Path) -> str:
    _require_private_peer_storage()
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
    _require_private_peer_storage()
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


async def _serve(directory: Path, credential_file: Path, *,
                 session_id: str | None = None, thread_id: str | None = None) -> None:
    _require_private_peer_storage()
    token = _credential(credential_file)
    with PeerMailbox(directory) as mailbox:
        mailbox.bind(session_id=session_id, thread_id=thread_id)
        server = session(mailbox, token)
        mailbox.heartbeat(token)

        async def renew() -> None:
            while True:
                await asyncio.sleep(20)
                mailbox.heartbeat(token)

        task = asyncio.create_task(renew())
        serving = asyncio.create_task(serve_stdio(server, sys.stdin.buffer,
                                                  sys.stdout.buffer))
        try:
            done, _ = await asyncio.wait({task, serving}, return_when=asyncio.FIRST_COMPLETED)
            if task in done:
                # A failed lease renewal must not leave a running server that
                # appears to accept messages while its presence has expired.
                # The stdio reader uses a blocking thread; cancelling its
                # coroutine does not interrupt a silent connected pipe.
                failure = task.exception()
                if failure is not None:
                    print("Peer presence renewal failed; stopping server", file=sys.stderr,
                          flush=True)
                    os._exit(1)
            if serving in done:
                serving.result()
        finally:
            task.cancel()
            serving.cancel()
            await asyncio.gather(task, serving, return_exceptions=True)
            mailbox.disconnect(token)


def main() -> None:
    _require_private_peer_storage()
    parser = argparse.ArgumentParser(description="Local authenticated peer mailbox")
    parser.add_argument("--state-dir", type=Path, required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    enroll = commands.add_parser("enroll", help="Trusted owner provisioning on this computer")
    for name in ("peer-id", "owner", "account", "project", "runtime"):
        enroll.add_argument("--" + name, required=True)
    enroll.add_argument("--credential-file", type=Path, required=True)
    serve = commands.add_parser("serve", help="Serve peer tools over MCP stdio")
    serve.add_argument("--credential-file", type=Path, required=True)
    serve.add_argument("--session-id", help="Host session label claimed by this MCP process")
    serve.add_argument("--thread-id", help="Host thread label claimed by this MCP process")
    args = parser.parse_args()
    directory: Path = args.state_dir
    if not directory.is_absolute():
        parser.error("--state-dir must be absolute")
    if args.command == "serve":
        asyncio.run(_serve(directory, args.credential_file,
                           session_id=args.session_id, thread_id=args.thread_id))
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
