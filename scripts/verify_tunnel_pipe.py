"""Opt-in, pinned cloudflared compatibility check using an invalid synthetic token.

No provider credential or public connection is used. Downloading a test binary is
explicit and temporary; it never installs or upgrades the user's cloudflared.
"""

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

from anywhere_computer.secret_pipe import SecretPipe

VERSION = "2026.2.0"
DIGESTS = {
    "cloudflared-darwin-amd64.tgz": (
        "685688a260c324eb8d9c9434ca22f0ce4f504fd6acd0706787c4833de8d6eb17"
    ),
    "cloudflared-darwin-arm64.tgz": (
        "ba99c6f87320236b9f842c3ba4b9526f687560125b7b43a581201579543ca4ff"
    ),
    "cloudflared-linux-amd64": "176746db3be7dc7bd48f3dd287c8930a4645ebb6e6700f883fddda5a4c307c16",
    "cloudflared-windows-amd64.exe": (
        "b3279f2186a1c3c438ad5865e802bbbec26090c5d3fdb4ac1113f1143a94837a"
    ),
}


def download_binary(directory):
    architecture = "arm64" if platform.machine().lower() in {"arm64", "aarch64"} else "amd64"
    system = {"darwin": "darwin", "win32": "windows", "linux": "linux"}[sys.platform]
    suffix = ".tgz" if system == "darwin" else (".exe" if system == "windows" else "")
    asset = f"cloudflared-{system}-{architecture}{suffix}"
    if asset not in DIGESTS:
        raise RuntimeError("This verification has no pinned binary for the current platform")
    download = directory / asset
    address = f"https://github.com/cloudflare/cloudflared/releases/download/{VERSION}/{asset}"
    digest = hashlib.sha256()
    with urllib.request.urlopen(address, timeout=30) as source, download.open("xb") as target:
        size = 0
        while block := source.read(1024 * 1024):
            size += len(block)
            if size > 100 * 1024 * 1024:
                raise RuntimeError("Test binary download exceeds limit")
            digest.update(block)
            target.write(block)
    if digest.hexdigest() != DIGESTS[asset]:
        raise RuntimeError("Test binary checksum mismatch")
    executable = download
    if suffix == ".tgz":
        executable = directory / "cloudflared"
        with tarfile.open(download) as archive:
            member = archive.getmember("cloudflared")
            if not member.isfile() or member.size > 100 * 1024 * 1024:
                raise RuntimeError("Unexpected test binary archive")
            source = archive.extractfile(member)
            if source is None:
                raise RuntimeError("Test binary is missing")
            with source, executable.open("xb") as target:
                shutil.copyfileobj(source, target)
    executable.chmod(0o700)
    return str(executable), asset, digest.hexdigest()


def verify(download_for_test):
    with tempfile.TemporaryDirectory(prefix="anywhere-pipe-verification-") as raw:
        directory = Path(raw)
        if download_for_test:
            executable, asset, digest = download_binary(directory)
        else:
            executable = shutil.which("cloudflared")
            if executable is None:
                raise RuntimeError("No cloudflared binary; use --download-for-test explicitly")
            asset, digest = "installed", hashlib.sha256(Path(executable).read_bytes()).hexdigest()
        config = directory / "config.json"
        config.write_text("{}\n", encoding="ascii")
        with SecretPipe(b"synthetic_tunnel_secret_0123456789") as pipe:
            environment = {
                key: value
                for key, value in os.environ.items()
                if not key.upper().startswith(("TUNNEL_", "CF_TUNNEL_"))
            }
            child = subprocess.Popen(
                [
                    executable,
                    "tunnel",
                    "--config",
                    str(config),
                    "--no-autoupdate",
                    "run",
                    "--token-file",
                    pipe.path,
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
            )
            try:
                pipe.wait(10)
                output, errors = child.communicate(timeout=5)
                if (
                    child.returncode == 0
                    or b"Provided Tunnel token is not valid" not in output + errors
                ):
                    raise RuntimeError("cloudflared did not reach synthetic token validation")
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=5)
                if child.stdout:
                    child.stdout.close()
                if child.stderr:
                    child.stderr.close()
    return {
        "platform": sys.platform,
        "asset": asset,
        "sha256": digest,
        "pipe_read_and_eof": True,
        "synthetic_token_rejected": True,
        "temporary_directory_removed": not directory.exists(),
        "public_connection_tested": False,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--download-for-test", action="store_true")
    args = parser.parse_args()
    print(json.dumps(verify(args.download_for_test), indent=2))
