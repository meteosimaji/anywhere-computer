import os
import plistlib
import secrets
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import replace

import pytest

from anywhere_computer import startup_native as native
from anywhere_computer.autostart import _systemd_quote, current_definition, startup_definition


@pytest.mark.parametrize("override", [
    {}, {"ActiveState": "active"}, {"SubState": "running"}, {"Transient": "yes"},
    {"DropInPaths": "/foreign.conf"}, {"UnitFileState": "masked"}, {"ActiveState": ""},
])
def test_linux_absence_requires_stopped_unconfigured_unit(tmp_path, monkeypatch, override):
    definition = startup_definition(tmp_path, platform="linux", home=tmp_path,
                                    executable=str(tmp_path / "python"), user="fixture")
    values = {"LoadState": "not-found", "FragmentPath": "", "ActiveState": "inactive",
              "SubState": "dead", "Transient": "no", "DropInPaths": "", "UnitFileState": ""}
    values.update(override)
    monkeypatch.setattr(native.NativeStartup, "_systemd",
                        lambda *a: "\n".join(f"{key}={value}" for key, value in values.items()))
    assert native.NativeStartup(definition).query().present is bool(override)


def test_absent_native_registration_is_read_only(tmp_path):
    definition = current_definition(tmp_path / "uncreated")
    if sys.platform == "linux":
        try:
            native._checked(["/usr/bin/systemctl", "--user", "show-environment"])
        except RuntimeError:
            pytest.skip("No running user systemd manager")
    if sys.platform == "darwin":
        try:
            native._checked(["/bin/launchctl", "print", native._gui_domain()])
        except RuntimeError:
            pytest.skip("No available user GUI domain")
    assert native.NativeStartup(definition).query().present is False
    assert not (tmp_path / "uncreated").exists()


@pytest.mark.skipif(os.environ.get("GITHUB_ACTIONS") != "true",
                    reason="Native registration is confined to disposable GitHub runners")
def test_native_registration_runs_and_removes_only_its_fixture(tmp_path):
    if sys.platform == "linux":
        try:
            native._checked(["/usr/bin/systemctl", "--user", "show-environment"])
        except RuntimeError:
            pytest.skip("No running user systemd manager")
    if sys.platform == "darwin":
        try:
            native._checked(["/bin/launchctl", "print", native._gui_domain()])
        except RuntimeError:
            pytest.skip("No available user GUI domain")
    marker = tmp_path / "started"
    worker = tmp_path / "fixture_worker.py"
    worker.write_text(
        "import sys,time\nfrom pathlib import Path\n"
        "Path(sys.argv[1]).write_text('started')\ntime.sleep(90)\n", encoding="utf-8",
    )
    registration_id = secrets.token_hex(16)
    definition = current_definition(tmp_path, startup_id=registration_id)
    arguments = [sys.executable, str(worker), str(marker), "--startup-id", registration_id]
    if sys.platform == "darwin":
        document = plistlib.loads(definition.content)
        document["ProgramArguments"] = arguments
        content = plistlib.dumps(document, sort_keys=True)
    elif sys.platform == "linux":
        content = "\n".join(
            "ExecStart=:" + " ".join(_systemd_quote(arg) for arg in arguments)
            if line.startswith("ExecStart=") else line
            for line in definition.content.decode().split("\n")
        ).encode()
    else:
        root = ET.fromstring(definition.content)
        ns = {"t": native.TASK_NAMESPACE}
        action_arguments = root.find("t:Actions/t:Exec/t:Arguments", ns)
        assert action_arguments is not None
        action_arguments.text = subprocess.list2cmdline(arguments[1:])
        content = ET.tostring(root, encoding="utf-16", xml_declaration=True)
    definition = replace(definition, content=content)
    backend = native.NativeStartup(definition)
    assert not backend.query().present and not definition.path.exists()
    definition.path.parent.mkdir(parents=True, exist_ok=True)
    with definition.path.open("xb") as exported:
        exported.write(content)
    try:
        backend.install()
        deadline = time.monotonic() + 15
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        assert marker.read_text() == "started"
        snapshot = backend.query()
        if sys.platform == "win32" and not snapshot.matches:
            # Only this disposable fixture's generated XML; no production credentials.
            pytest.fail("Fixture readback mismatch\nEXPECTED:\n"
                        + definition.content.decode("utf-16") + "\nACTUAL:\n" + snapshot.raw)
        assert snapshot.present and snapshot.matches and snapshot.running and snapshot.enabled
        assert snapshot.fingerprint == backend.query().fingerprint
        backend.uninstall(snapshot)
        after = backend.query()
        assert not after.running and not after.enabled
    finally:
        try:
            remaining = backend.query()
            if remaining.present:
                backend.uninstall(remaining)
        finally:
            if definition.path.read_bytes() == content:
                definition.path.unlink()
            backend.files_removed()
    assert not definition.path.exists()
    assert not backend.query().present
