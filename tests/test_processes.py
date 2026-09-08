import subprocess
import sys

import psutil
import pytest

from anywhere_computer.models import ListProcesses, StopProcess
from anywhere_computer.processes import list_processes, stop_process


def test_process_inspection_and_identity_checked_termination():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        created = psutil.Process(child.pid).create_time()
        page = list_processes(ListProcesses(after_pid=child.pid - 1, limit=1))
        assert page["processes"][0]["pid"] == child.pid
        assert page["processes"][0]["created"] == created
        with pytest.raises(ValueError, match="identity"):
            stop_process(StopProcess(pid=child.pid, created=created + 1))
        assert child.poll() is None
        assert stop_process(StopProcess(pid=child.pid, created=created))["state"] == "exited"
    finally:
        if child.poll() is None:
            child.kill()
        child.wait()
