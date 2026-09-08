"""Optional stdio transport through an existing, verified system SSH setup."""

import re
import shutil
import subprocess


def validate_ssh_host(host: str) -> None:
    # Host aliases only: options, user@host, remote paths and shell syntax are not accepted.
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,252}", host) is None:
        raise ValueError("Use a configured SSH host alias, without options or shell syntax")


def ssh_command(host: str, command: str = "mcp") -> list[str]:
    validate_ssh_host(host)
    if command not in {"mcp", "status"}:
        raise ValueError("Unsupported remote entry point")
    executable = shutil.which("ssh")
    if executable is None:
        raise RuntimeError("System SSH is not installed; configure OpenSSH before using this mode")
    return [
        executable,
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=3",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "ClearAllForwardings=yes",
        "-o",
        "ForwardAgent=no",
        "-o",
        "ForwardX11=no",
        "-o",
        "PermitLocalCommand=no",
        host,
        "anywhere",
        command,
    ]


def run_ssh_mcp(host: str) -> int:
    # Inherit stdio directly so JSON-RPC and large output are streamed without buffering.
    # No automatic reconnect/re-execution: the agent survives independently on the peer.
    return subprocess.run(ssh_command(host), check=False).returncode
