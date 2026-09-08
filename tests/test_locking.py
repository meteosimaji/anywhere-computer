import subprocess
import sys

import pytest

from anywhere_computer.locking import ProcessLock


def test_lock_exclusion_and_release_after_process_death(tmp_path):
    path = tmp_path / "process.lock"
    child = subprocess.Popen(
        [
            sys.executable,
            "-u",
            "-c",
            "import sys,time; from pathlib import Path; "
            "from anywhere_computer.locking import ProcessLock; "
            "lock=ProcessLock(Path(sys.argv[1])); lock.__enter__(); "
            "print('locked',flush=True); time.sleep(30)",
            str(path),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout.readline().strip() == "locked"
        with pytest.raises(TimeoutError):
            with ProcessLock(path, timeout=0.05):
                pytest.fail("Two processes acquired the same lock")
    finally:
        child.kill()
        child.wait(timeout=5)
        child.stdout.close()
    with ProcessLock(path, timeout=1):
        assert path.exists()


def test_lock_instance_cannot_be_entered_twice(tmp_path):
    lock = ProcessLock(tmp_path / "lock")
    with lock:
        with pytest.raises(RuntimeError, match="already acquired"):
            lock.__enter__()
    with lock:
        pass
