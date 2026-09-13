from dataclasses import replace
from types import SimpleNamespace

import pytest

from anywhere_computer import autostart
from anywhere_computer import startup_service as service
from anywhere_computer.startup_native import StartupSnapshot


def test_local_windows_startup_uses_same_environment_windowed_python(tmp_path, monkeypatch):
    console = tmp_path / "python.exe"
    windowed = tmp_path / "pythonw.exe"
    console.touch()
    monkeypatch.setattr(autostart, "sys",
                        SimpleNamespace(platform="win32", executable=str(console)))
    with pytest.raises(ValueError, match="requires pythonw.exe"):
        autostart.startup_interpreter("local")
    assert autostart.startup_interpreter("remote") == str(console)
    windowed.touch()
    assert autostart.startup_interpreter("local") == str(windowed)
    monkeypatch.setattr(autostart.sys, "executable", str(windowed))
    assert autostart.startup_interpreter("local") == str(windowed)


@pytest.fixture
def registration(tmp_path, monkeypatch):
    def missing_codex(_):
        raise FileNotFoundError("fixture has no Codex installation")
    monkeypatch.setattr(service, "codex_executable", missing_codex)
    original = service.current_definition
    state = tmp_path / "state"
    calls = []
    native = {"snapshot": StartupSnapshot(False), "lost_ack": False}

    def definition(directory, **options):
        value = original(directory, **options)
        return replace(value, path=tmp_path / "definitions" / value.path.name)

    class Backend:
        def __init__(self, definition):
            self.definition = definition

        def query(self):
            return native["snapshot"]

        def install(self):
            calls.append("install")
            native["snapshot"] = StartupSnapshot(True, True, True, True, "1" * 64)
            if native["lost_ack"]:
                raise RuntimeError("synthetic lost acknowledgment")

        def start(self, snapshot):
            calls.append("start")
            native["snapshot"] = replace(native["snapshot"], running=True)

        def uninstall(self, snapshot):
            calls.append("uninstall")
            native["snapshot"] = StartupSnapshot(False)

        def files_removed(self):
            calls.append("files_removed")

    monkeypatch.setattr(service, "current_definition", definition)
    monkeypatch.setattr(service, "NativeStartup", Backend)
    monkeypatch.setattr(service, "cloudflared_executable",
                        lambda p=None: str(tmp_path / "connector"))
    monkeypatch.setattr(service, "load_http_config", lambda d:
                        SimpleNamespace(resource="https://example.com/mcp", owner="owner"))
    monkeypatch.setattr(service, "OwnerCredentials", lambda *a, **k:
                        SimpleNamespace(ensure_initialized=lambda: None))
    monkeypatch.setattr(service, "TunnelCredential", lambda d:
                        SimpleNamespace(read=lambda: "synthetic credential"))
    return state, native, calls, definition


def test_registration_is_idempotent_and_removal_preserves_config(registration):
    directory, native, calls, definition = registration
    directory.mkdir()
    sentinel = directory / "credentials-placeholder"
    sentinel.write_text("untouched")
    first = service.install_startup(directory)
    record = service._record(directory)
    assert record is not None and record.native_fingerprint == "1" * 64
    generated = definition(directory, connector=record.connector, startup_id=record.startup_id,
                           executable=record.interpreter, policy_version=record.policy_version)
    assert generated.path.read_bytes() == generated.content
    assert record.startup_id.encode() in (
        generated.content if generated.platform != "win32"
        else generated.content.decode("utf-16").encode()
    )
    assert service.install_startup(directory) == first
    assert calls == ["install"]
    assert service.startup_status(directory)["native_running"] is True
    assert service.uninstall_startup(directory)["state"] == "uninstalled"
    assert calls == ["install", "uninstall", "files_removed"]
    assert sentinel.read_text() == "untouched"
    assert not generated.path.exists() and not (directory / "autostart.json").exists()
    assert service.uninstall_startup(directory)["changed"] is False


def test_foreign_native_registration_is_not_overwritten(registration):
    directory, native, calls, _ = registration
    native["snapshot"] = StartupSnapshot(True)
    with pytest.raises(ValueError, match="will not be overwritten"):
        service.install_startup(directory)
    assert calls == [] and not (directory / "autostart.json").exists()


def test_modified_definition_and_native_identity_are_preserved(registration):
    directory, native, calls, _ = registration
    result = service.install_startup(directory)
    from pathlib import Path

    path = Path(result["definition_path"])
    original = path.read_bytes()
    path.write_bytes(b"foreign definition")
    with pytest.raises(ValueError, match="changed"):
        service.uninstall_startup(directory)
    assert calls == ["install"] and path.read_bytes() == b"foreign definition"
    path.write_bytes(original)
    native["snapshot"] = StartupSnapshot(True, True, False, True, "2" * 64)
    with pytest.raises(ValueError, match="changed"):
        service.uninstall_startup(directory)
    assert calls == ["install"]


def test_lost_creation_ack_is_resolved_without_repeating_creation(registration):
    directory, native, calls, _ = registration
    native["lost_ack"] = True
    assert service.install_startup(directory)["state"] == "registered"
    assert calls == ["install"]
    assert service._record(directory).native_fingerprint == "1" * 64


def test_prepared_receipt_resumes_and_changed_connector_is_rejected(registration, monkeypatch):
    directory, native, calls, _ = registration
    original = service._save_registered
    monkeypatch.setattr(service, "_save_registered",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("synthetic interruption")))
    with pytest.raises(OSError):
        service.install_startup(directory)
    assert service._record(directory).native_fingerprint == ""
    monkeypatch.setattr(service, "_save_registered", original)
    assert service.install_startup(directory)["state"] == "registered"
    assert calls == ["install"]
    with pytest.raises(ValueError, match="connector differs"):
        service.install_startup(directory, connector="different")
    assert calls == ["install"]


def test_unconfirmed_shutdown_retains_receipt_and_can_resume(registration, monkeypatch):
    directory, native, calls, _ = registration
    service.install_startup(directory)
    original = service._wait_stopped
    monkeypatch.setattr(service, "_wait_stopped",
                        lambda *a: (_ for _ in ()).throw(RuntimeError("still stopping")))
    with pytest.raises(RuntimeError, match="still stopping"):
        service.uninstall_startup(directory)
    assert (directory / "autostart.json").exists()
    monkeypatch.setattr(service, "_wait_stopped", original)
    assert service.uninstall_startup(directory)["state"] == "uninstalled"
    assert calls.count("uninstall") == 1


def test_status_without_receipt_does_not_create_state(tmp_path):
    directory = tmp_path / "missing"
    assert service.startup_status(directory)["changed"] is False
    assert not directory.exists()


def test_legacy_startup_can_be_removed_but_not_reenabled(registration):
    import json

    directory, native, calls, definition = registration
    service.install_startup(directory)
    receipt = directory / "autostart.json"
    values = json.loads(receipt.read_text())
    values.pop("policy_version")
    values.pop("isolated_python")  # Exact old schema, not just a new False value.
    receipt.write_text(json.dumps(values))
    record = service._record(directory)
    legacy = definition(directory, connector=record.connector, startup_id=record.startup_id,
                        executable=record.interpreter, isolated_python=False)
    legacy.path.write_bytes(legacy.content)
    before = receipt.read_bytes()
    assert service.startup_status(directory)["python_isolation_upgrade_required"] is True
    with pytest.raises(ValueError, match="Legacy startup"):
        service.install_startup(directory)
    assert calls == ["install"] and receipt.read_bytes() == before
    assert legacy.path.read_bytes() == legacy.content
    assert service.uninstall_startup(directory)["state"] == "uninstalled"
    service.install_startup(directory)
    assert service._record(directory).isolated_python is True
    assert service.startup_status(directory)["python_isolation_upgrade_required"] is False


def test_initial_publication_preserves_racing_file(tmp_path, monkeypatch):
    destination = tmp_path / "receipt"
    original = service.os.link

    def competing_link(source, target):
        target.write_bytes(b"another owner")
        return original(source, target)

    monkeypatch.setattr(service.os, "link", competing_link)
    with pytest.raises(FileExistsError):
        service._create_file(destination, b"complete receipt")
    assert destination.read_bytes() == b"another owner"
    assert list(tmp_path.iterdir()) == [destination]


def test_initial_publication_failure_leaves_no_partial_file(tmp_path, monkeypatch):
    destination = tmp_path / "receipt"

    def interrupted_sync(descriptor):
        assert not destination.exists()
        raise OSError("synthetic interrupted write")

    monkeypatch.setattr(service.os, "fsync", interrupted_sync)
    with pytest.raises(OSError, match="interrupted"):
        service._create_file(destination, b"complete receipt")
    assert list(tmp_path.iterdir()) == []


def test_missing_definition_is_not_recreated_over_foreign_native(registration):
    from pathlib import Path

    directory, native, calls, _ = registration
    result = service.install_startup(directory)
    path = Path(result["definition_path"])
    path.unlink()
    native["snapshot"] = StartupSnapshot(True)
    with pytest.raises(ValueError, match="changed"):
        service.install_startup(directory)
    assert not path.exists()
    assert calls == ["install"]


def test_remaining_native_after_file_cleanup_retains_receipt(registration, monkeypatch):
    directory, native, calls, _ = registration
    service.install_startup(directory)

    def still_present(self):
        native["snapshot"] = StartupSnapshot(True)

    monkeypatch.setattr(service.NativeStartup, "files_removed", still_present)
    with pytest.raises(RuntimeError, match="removal was not confirmed"):
        service.uninstall_startup(directory)
    assert (directory / "autostart.json").exists()
    native["snapshot"] = StartupSnapshot(False)
    monkeypatch.setattr(service.NativeStartup, "files_removed", lambda self: None)
    assert service.uninstall_startup(directory)["state"] == "uninstalled"
    assert calls.count("uninstall") == 1


@pytest.mark.parametrize("during_install", [False, True])
def test_foreign_registration_race_removes_only_pending_definition(
    registration, monkeypatch, during_install,
):
    directory, native, calls, _ = registration
    original_create = service._create_file
    created_paths = []

    def create_and_race(path, content):
        original_create(path, content)
        if path.name != "autostart.json":
            created_paths.append(path)
            if not during_install:
                native["snapshot"] = StartupSnapshot(True)

    def install_and_race(self):
        native["snapshot"] = StartupSnapshot(True)
        raise RuntimeError("another task registered concurrently")

    monkeypatch.setattr(service, "_create_file", create_and_race)
    if during_install:
        monkeypatch.setattr(service.NativeStartup, "install", install_and_race)
    with pytest.raises(ValueError, match="changed"):
        service.install_startup(directory)
    assert len(created_paths) == 1 and not created_paths[0].exists()
    assert service._record(directory).native_fingerprint == ""
    assert native["snapshot"].present and calls == []


def test_startup_pins_codex_path_and_preserves_it_when_path_changes(registration, monkeypatch):
    directory, _, _, _ = registration
    selected = directory.parent / "Codex app" / "codex"
    monkeypatch.setattr(service, "codex_executable", lambda _: selected)
    service.install_startup(directory)
    record = service._record(directory)
    assert record.codex_executable == str(selected)
    content = service._definition(directory, record).content
    encoding = "utf-16" if record.platform == "win32" else "utf-8"
    assert "--codex-executable" in content.decode(encoding)
    monkeypatch.setattr(service, "codex_executable", lambda _: directory.parent / "different")
    service.install_startup(directory)
    assert service._record(directory).codex_executable == str(selected)
    assert service._definition(directory, service._record(directory)).content == content


def test_explicit_start_is_owned_and_does_not_restart_running(registration):
    directory, native, calls, _ = registration
    service.install_startup(directory)
    native['snapshot'] = replace(native['snapshot'], running=False)
    assert service.start_startup(directory)['native_running'] is True
    service.start_startup(directory)
    assert calls == ['install', 'start']


def test_policy_one_receipt_can_be_inspected_and_upgraded(registration):
    import json
    directory, native, calls, _ = registration
    service.install_startup(directory)
    receipt = directory / 'autostart.json'
    value = json.loads(receipt.read_text())
    value.pop('policy_version')
    receipt.write_text(json.dumps(value))
    old = service._record(directory)
    definition = service._definition(directory, old)
    definition.path.write_bytes(definition.content)
    assert service.startup_status(directory)['persistence_upgrade_required'] is True
    service.upgrade_startup(directory)
    assert service._record(directory).policy_version == 2
    assert calls == ['install', 'uninstall', 'files_removed', 'install']


def test_upgrade_failure_restores_verified_prior_registration(registration, monkeypatch):
    directory, native, calls, _ = registration
    service.install_startup(directory)
    old = service._record(directory)
    real = service._install_startup_locked
    attempts = []

    def fail_new(directory, **options):
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError('fixture deployment failed')
        return real(directory, **options)

    monkeypatch.setattr(service, '_install_startup_locked', fail_new)
    with pytest.raises(RuntimeError, match='previous registration restored'):
        service.upgrade_startup(directory)
    assert service._record(directory) == old
    assert service.startup_status(directory)['native_running'] is True


def test_uninstall_waits_for_matching_native_job_to_disappear(registration, monkeypatch):
    directory, native, calls, _ = registration
    service.install_startup(directory)
    queries = 0
    removing = False

    def uninstall(self, snapshot):
        nonlocal removing
        calls.append('uninstall')
        removing = True

    def query(self):
        nonlocal queries
        if removing:
            queries += 1
            if queries >= 3:
                native['snapshot'] = StartupSnapshot(False)
        return native['snapshot']

    monkeypatch.setattr(service.NativeStartup, 'uninstall', uninstall)
    monkeypatch.setattr(service.NativeStartup, 'query', query)
    assert service.uninstall_startup(directory)['state'] == 'uninstalled'
    assert calls.count('uninstall') == 1


def test_pre_utf8_receipt_remains_inspectable_and_upgradeable(registration, monkeypatch):
    import json

    from anywhere_computer import autostart

    directory, native, calls, definition = registration
    original = autostart.python_module_command

    def legacy_command(*args, **kwargs):
        command = original(*args, **kwargs)
        if "-X" in command:
            offset = command.index("-X")
            del command[offset:offset + 2]
        return command

    with monkeypatch.context() as old:
        old.setattr(autostart, "python_module_command", legacy_command)
        service.install_startup(directory)
    receipt = directory / "autostart.json"
    raw = json.loads(receipt.read_text())
    raw.pop("utf8_python", None)
    receipt.write_text(json.dumps(raw))
    assert service.startup_status(directory)["definition_exists"] is True
    service.upgrade_startup(directory)
    assert service._record(directory).utf8_python is True
    assert service.startup_status(directory)["definition_exists"] is True


def test_local_registration_needs_no_remote_setup_and_preserves_mode(registration, monkeypatch):
    directory, native, calls, definition = registration

    def unexpected(*args, **kwargs):
        pytest.fail("Local startup must not require remote provisioning")

    for name in (
        "cloudflared_executable", "load_http_config", "OwnerCredentials", "TunnelCredential",
    ):
        monkeypatch.setattr(service, name, unexpected)
    first = service.install_startup(directory, mode="local")
    record = service._record(directory)
    assert record.mode == "local" and record.connector is None
    assert first["state"] == "registered"
    assert service.install_startup(directory)["state"] == "registered"
    assert calls == ["install"]
    with pytest.raises(ValueError, match="mode differs"):
        service.install_startup(directory, mode="remote")
    assert calls == ["install"]
    assert service.upgrade_startup(directory)["state"] == "registered"
    assert service._record(directory).mode == "local"
    assert service.uninstall_startup(directory)["state"] == "uninstalled"


def test_local_management_removal_rejects_remote_receipt_under_lock(registration):
    directory, native, calls, _ = registration
    service.install_startup(directory)
    original = (directory / 'autostart.json').read_bytes()
    with pytest.raises(ValueError, match='Startup mode changed'):
        service.uninstall_startup(directory, expected_mode='local')
    assert calls == ['install']
    assert native['snapshot'].running
    assert (directory / 'autostart.json').read_bytes() == original
