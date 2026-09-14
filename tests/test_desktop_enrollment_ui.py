import shutil
import subprocess
from pathlib import Path

import pytest


def test_enrollment_ui_state_and_recovery():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the management UI state test")
    subprocess.run([node, str(Path(__file__).with_name("desktop_enrollment_ui.cjs"))],
                   check=True, timeout=20)


def test_management_device_ui_state_and_recovery():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the management UI state test")
    subprocess.run([node, str(Path(__file__).with_name("desktop_management_ui.cjs"))],
                   check=True, timeout=20)
