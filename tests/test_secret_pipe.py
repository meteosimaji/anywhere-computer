import hashlib
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from anywhere_computer.secret_pipe import SecretPipe


@pytest.mark.parametrize("size", [1, 4096])
def test_real_child_reads_exact_payload_to_eof_and_pipe_is_removed(size):
    payload = os.urandom(size)
    with SecretPipe(payload) as channel:
        pipe_path = channel.path
        if os.name != "nt":
            assert stat.S_ISFIFO(os.stat(pipe_path).st_mode)
            assert stat.S_IMODE(os.stat(pipe_path).st_mode) == 0o600
            assert stat.S_IMODE(Path(pipe_path).parent.stat().st_mode) == 0o700
        code = (
            "import sys,hashlib; "
            "data=open(sys.argv[1], 'rb').read(); print(hashlib.sha256(data).hexdigest())"
        )
        child = subprocess.Popen(
            [sys.executable, "-c", code, pipe_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            channel.wait(5)
            output, errors = child.communicate(timeout=5)
            assert child.returncode == 0, errors
            assert output.decode().strip() == hashlib.sha256(payload).hexdigest()
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)
            if child.stdout:
                child.stdout.close()
            if child.stderr:
                child.stderr.close()
    assert not Path(pipe_path).exists()
    assert not channel._thread.is_alive()


def test_missing_consumer_is_cancelled_without_hanging_or_secret_file():
    started = time.monotonic()
    with SecretPipe(b"synthetic-secret") as channel:
        with pytest.raises(TimeoutError, match="did not read"):
            channel.wait(0.05)
    assert time.monotonic() - started < 4
    assert not channel._thread.is_alive()
    assert not Path(channel.path).exists()


def test_connected_consumer_that_never_reads_is_cancelled(tmp_path):
    marker = tmp_path / "connected"
    child = None
    try:
        with SecretPipe(b"x" * 4096) as channel:
            code = (
                "import sys,time; from pathlib import Path; "
                "f=open(sys.argv[1], 'rb'); Path(sys.argv[2]).touch(); time.sleep(60)"
            )
            child = subprocess.Popen([sys.executable, "-c", code, channel.path, str(marker)])
            deadline = time.monotonic() + 5
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert marker.exists()
            # Windows FlushFileBuffers remains blocked until consumption; context cancellation
            # must interrupt it. POSIX can finish after placing bytes in the kernel pipe buffer.
        assert child.poll() is None
        assert not channel._thread.is_alive()
    finally:
        if child is not None:
            child.kill()
            child.wait(timeout=5)


@pytest.mark.parametrize("payload", [b"", b"x" * 4097])
def test_payload_is_bounded(payload):
    with pytest.raises(ValueError, match="1–4096"):
        SecretPipe(payload)
