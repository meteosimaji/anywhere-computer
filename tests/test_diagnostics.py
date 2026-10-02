import asyncio
import json
import os

import psutil
import pytest

from anywhere_computer import __version__
from anywhere_computer.connection import serve
from anywhere_computer.diagnostics import diagnose


async def test_diagnosis_does_not_initialize_missing_state(tmp_path):
    directory = tmp_path / "absent"
    assert (await diagnose(directory))["state"] == "stopped"
    assert not directory.exists()


@pytest.mark.parametrize("contents", ["[]", "null", "{broken", '{"pid":true}'])
async def test_malformed_endpoint_returns_diagnosis(tmp_path, contents):
    endpoint = tmp_path / "agent.json"
    endpoint.write_text(contents, encoding="utf-8")
    assert (await diagnose(tmp_path))["state"] == "invalid_endpoint"
    assert endpoint.read_text(encoding="utf-8") == contents


async def test_reused_pid_does_not_contact_process(tmp_path, monkeypatch):
    (tmp_path / "agent.json").write_text(
        json.dumps({"pid": os.getpid(), "process_started": 0}), encoding="utf-8"
    )

    def forbidden(*args, **kwargs):
        pytest.fail("Stale endpoint must not access credentials")

    monkeypatch.setattr("anywhere_computer.diagnostics.local_credential", forbidden)
    assert (await diagnose(tmp_path))["state"] == "stale_endpoint"


async def test_credentials_failure_is_sanitized(tmp_path, monkeypatch):
    (tmp_path / "agent.json").write_text(
        json.dumps({"pid": os.getpid(), "process_started": psutil.Process().create_time()}),
        encoding="utf-8",
    )

    def unavailable(*args, **kwargs):
        raise RuntimeError("secret-backend-detail")

    monkeypatch.setattr("anywhere_computer.diagnostics.local_credential", unavailable)
    result = await diagnose(tmp_path)
    assert result["state"] == "credential_unavailable"
    assert "secret-backend-detail" not in json.dumps(result)


async def test_live_diagnosis_does_not_restart_or_modify_agent(tmp_path, monkeypatch):
    secret = "disposable-test-credential"
    monkeypatch.setattr(
        "anywhere_computer.diagnostics.local_credential", lambda *a, **kw: secret
    )
    shutdown = asyncio.Event()
    task = asyncio.create_task(serve(tmp_path, credential=secret, shutdown=shutdown))
    try:
        for _ in range(300):
            if (tmp_path / "agent.json").exists():
                break
            if task.done():
                await task
            await asyncio.sleep(0.01)
        first = await diagnose(tmp_path)
        second = await diagnose(tmp_path)
        assert first["state"] == second["state"] == "ready"
        assert first["changed"] is False
        assert first["agent"]["instance_id"] == second["agent"]["instance_id"]
        assert secret not in json.dumps(first)
        monkeypatch.setattr(
            "anywhere_computer.diagnostics.local_credential", lambda *a, **kw: "wrong"
        )
        assert (await diagnose(tmp_path))["state"] == "unresponsive"
        monkeypatch.setattr(
            "anywhere_computer.diagnostics.local_credential", lambda *a, **kw: secret
        )
        monkeypatch.setattr("anywhere_computer.diagnostics.runtime_identity", lambda: "new")
        different = await diagnose(tmp_path)
        assert different["state"] == "different_build"
        assert different["update_readiness"]["state"] == "idle"
        assert "run anywhere start" in different["update_readiness"]["action"]
        assert different["runtime_comparison"] == {
            "state": "different",
            "version_matches": True,
            "runtime_id_matches": False,
            "source_implementation": "current_diagnostic_process",
            "connection_authorization": "authenticated_status_only",
            "feature_authorization": "unknown",
            "helper_and_os_permissions": "not_checked",
            "acceptance": "not_verified",
        }
        assert different["source_build"] == {
            "version": __version__, "runtime_id": "new",
        }
        assert not task.done()
    finally:
        shutdown.set()
        await asyncio.wait_for(task, 5)


async def test_same_version_different_runtime_reports_source_runtime_and_catalog_separately(
    tmp_path, monkeypatch,
):
    from anywhere_computer import diagnostics
    from anywhere_computer.models import Reply

    (tmp_path / "agent.json").write_text(json.dumps({
        "pid": os.getpid(), "process_started": psutil.Process().create_time(),
        "instance_id": "fixture-instance",
    }), encoding="utf-8")
    monkeypatch.setattr(diagnostics, "local_credential", lambda *_: "fixture-credential")
    monkeypatch.setattr(diagnostics, "runtime_identity", lambda: "source-runtime")

    async def fixture_exchange(_directory, tool, **_kwargs):
        if tool == "__status":
            return Reply(operation_id="a" * 32, state="completed", data={
                "state": "ready", "instance_id": "fixture-instance",
                "version": __version__, "runtime_id": "running-runtime",
                "update_blocked": True,
                "update_blockers": ["terminal_sessions", "other_active_resources"],
                "update_blocker_details": [{
                    "resource": "terminal_session", "id": "owned-session",
                    "state": "running", "stop_available": True,
                }],
                "capability_diagnostics": {
                    "skills": {"running_implementation": "absent", "acceptance": "not_verified"},
                    "audio_capture": {"running_implementation": "present", "helper": "unavailable"},
                },
            })
        assert tool == "__catalog"
        return Reply(operation_id="b" * 32, state="completed", data={"tools": [
            {"name": "audio_status"}, {"name": "audio_capture"},
        ]})

    monkeypatch.setattr(diagnostics, "exchange", fixture_exchange)
    result = await diagnostics.diagnose(tmp_path)
    assert result["state"] == "different_build"
    assert result["update_readiness"]["state"] == "blocked"
    assert "update_blocker_details" in result["update_readiness"]["action"]
    assert "owned-session" in json.dumps(result["agent"]["update_blocker_details"])
    assert "other_active_resources" in result["agent"]["update_blockers"]
    assert result["runtime_comparison"]["version_matches"] is True
    skills = result["agent"]["capability_diagnostics"]["skills"]
    assert skills["source_implementation"] == "present"
    assert skills["running_implementation"] == "absent"
    assert skills["connection_publication"] == "not_published"
    assert skills["connection_authorization"] == "not_observed"
    audio = result["agent"]["capability_diagnostics"]["audio_capture"]
    assert audio["source_implementation"] == "present"
    assert audio["running_implementation"] == "present"
    assert audio["connection_publication"] == "published"
    assert audio["helper"] == "unavailable"
    assert audio["acceptance"] == "not_verified"


async def test_filtered_catalog_does_not_imply_missing_running_implementation(
    tmp_path, monkeypatch,
):
    from anywhere_computer import diagnostics
    from anywhere_computer.models import Reply

    (tmp_path / "agent.json").write_text(json.dumps({
        "pid": os.getpid(), "process_started": psutil.Process().create_time(),
        "instance_id": "fixture-instance",
    }), encoding="utf-8")
    monkeypatch.setattr(diagnostics, "local_credential", lambda *_: "fixture-credential")
    monkeypatch.setattr(diagnostics, "runtime_identity", lambda: "source-runtime")

    async def fixture_exchange(_directory, tool, **_kwargs):
        if tool == "__status":
            return Reply(operation_id="a" * 32, state="completed", data={
                "state": "ready", "instance_id": "fixture-instance",
                "version": __version__, "runtime_id": "running-runtime",
                "capability_diagnostics": {
                    "audio_capture": {
                        "running_implementation": "present",
                        "connection_authorization": "denied",
                    },
                },
            })
        assert tool == "__catalog"
        return Reply(operation_id="b" * 32, state="completed", data={"tools": []})

    monkeypatch.setattr(diagnostics, "exchange", fixture_exchange)
    result = await diagnostics.diagnose(tmp_path)
    assert result["state"] == "different_build"
    assert result["update_readiness"]["state"] == "unknown"
    assert "did not report" in result["update_readiness"]["action"]
    assert result["runtime_comparison"]["version_matches"] is True
    skills = result["agent"]["capability_diagnostics"]["skills"]
    assert skills["source_implementation"] == "present"
    assert skills["running_implementation"] == "unknown"
    assert skills["connection_publication"] == "not_published"
    assert skills["connection_authorization"] == "not_observed"
    audio = result["agent"]["capability_diagnostics"]["audio_capture"]
    assert audio["running_implementation"] == "present"
    assert audio["connection_publication"] == "not_published"
    assert audio["connection_authorization"] == "denied"


@pytest.mark.parametrize("second_status", ["restarted", "unavailable"])
async def test_diagnosis_does_not_attribute_catalog_across_agent_change(
    tmp_path, monkeypatch, second_status,
):
    from anywhere_computer import diagnostics
    from anywhere_computer.models import Reply

    (tmp_path / "agent.json").write_text(json.dumps({
        "pid": os.getpid(), "process_started": psutil.Process().create_time(),
        "instance_id": "first-instance",
    }), encoding="utf-8")
    monkeypatch.setattr(diagnostics, "local_credential", lambda *_: "fixture-credential")
    calls = []

    async def fixture_exchange(_directory, tool, **_kwargs):
        calls.append(tool)
        if calls == ["__status"]:
            return Reply(operation_id="a" * 32, state="completed", data={
                "state": "ready", "instance_id": "first-instance",
                "runtime_id": "first-runtime", "version": __version__,
                "capability_diagnostics": {
                    "audio_capture": {"running_implementation": "present"},
                },
            })
        if tool == "__catalog":
            return Reply(operation_id="b" * 32, state="completed", data={"tools": [
                {"name": "audio_status"}, {"name": "audio_capture"},
            ]})
        assert tool == "__status"
        if second_status == "unavailable":
            raise ConnectionError("second status unavailable")
        return Reply(operation_id="c" * 32, state="completed", data={
            "state": "ready", "instance_id": "second-instance",
            "runtime_id": "second-runtime", "version": __version__,
        })

    monkeypatch.setattr(diagnostics, "exchange", fixture_exchange)
    result = await diagnose(tmp_path)
    assert calls == ["__status", "__catalog", "__status"]
    assert result["state"] == (
        "snapshot_unconfirmed" if second_status == "unavailable" else "endpoint_changed"
    )
    assert result["changed"] is False
    assert result["agent"]["capability_diagnostics"]["audio_capture"][
        "connection_publication"
    ] == "unknown"


def test_runtime_diagnosis_exposes_path_resolution_without_environment(tmp_path, monkeypatch):
    from anywhere_computer.diagnostics import runtime_environment

    monkeypatch.setenv('PATH', str(tmp_path))
    monkeypatch.setenv('SYNTHETIC_PRIVATE_TOKEN', 'must-not-appear')
    report = runtime_environment()
    assert report['executables_on_path'] == {'node': None, 'uv': None, 'codex': None}
    assert report['scope'] == 'diagnostic_process'
    assert report['browser_operation_verified'] is False
    assert 'must-not-appear' not in json.dumps(report)


def test_missing_sdk_diagnosis_has_action_without_loading_sdk(monkeypatch):
    from importlib.metadata import PackageNotFoundError

    from anywhere_computer import diagnostics

    def missing(name):
        raise PackageNotFoundError(name)

    monkeypatch.setattr(diagnostics, 'version', missing)
    report = diagnostics.runtime_environment()
    assert report['direct_mcp_dependency'] == 'missing'
    assert '--extra mcp' in report['direct_mcp_action']


def test_codex_diagnosis_uses_actual_override_without_executing(tmp_path, monkeypatch):
    from anywhere_computer.diagnostics import runtime_environment

    executable = tmp_path / 'selected-codex'
    executable.write_text('not executable: diagnosis must not launch it')
    monkeypatch.setenv('ANYWHERE_CODEX_EXECUTABLE', str(executable))
    result = runtime_environment()['codex_selection']
    assert result == {'source': 'explicit_override', 'state': 'resolved',
                      'path': str(executable.resolve()), 'execution_verified': False}


@pytest.mark.parametrize('override', ['relative-private-value', '/absent/codex'])
def test_bad_codex_override_is_not_replaced_by_path_lookup(monkeypatch, override):
    from anywhere_computer.diagnostics import runtime_environment

    monkeypatch.setenv('ANYWHERE_CODEX_EXECUTABLE', override)
    result = runtime_environment()['codex_selection']
    assert result['source'] == 'explicit_override'
    assert result['state'] == 'unresolved'
    assert result['path'] is None
    assert result['execution_verified'] is False
    assert override not in json.dumps(result)


@pytest.mark.parametrize("store_available", [True, False])
async def test_linux_startup_diagnosis_scopes_session_and_never_reads_credentials(
    tmp_path, monkeypatch, store_available,
):
    from anywhere_computer import diagnostics

    class SecureStore:
        def get_password(self, *args):
            pytest.fail("Startup diagnosis must not read or create credentials")

        set_password = get_password

    SecureStore.__module__ = "keyring.backends.SecretService"

    def select():
        if not store_available:
            raise RuntimeError("private DBus value")
        return SecureStore()

    monkeypatch.setattr(diagnostics.sys, "platform", "linux")
    monkeypatch.setattr(diagnostics, "secure_backend", select)
    monkeypatch.setattr(diagnostics, "linux_credential_services", lambda: {
        "state": "not_checked", "service_activation_requested": False,
    })
    monkeypatch.setenv("DISPLAY", "private-display-value")
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "private DBus value")
    monkeypatch.setenv("XDG_RUNTIME_DIR", "private runtime value")
    directory = tmp_path / "missing"
    result = await diagnostics.diagnose(directory)
    assert result["state"] == "stopped"
    assert not directory.exists()
    details = result["runtime_environment"]["linux_prerequisites"]
    assert details["scope"] == "diagnostic_process_only"
    assert details["session_type"] == "x11"
    assert details["display_environment_present"] is True
    assert details["wayland_environment_present"] is False
    assert details["session_bus_environment_present"] is True
    assert details["desktop_session_verified"] is False
    assert details["credential_store"]["selection"] == (
        "selected" if store_available else "unavailable"
    )
    assert details["credential_store"]["unlock_state"] == "not_checked"
    assert details["credential_store"]["plaintext_fallback"] is False
    for name in ("gui_native", "audio_capture", "documents_preview"):
        assert details["platform_support"][name] == "unsupported_platform"
    assert "private" not in json.dumps(details)


def test_linux_bus_probe_distinguishes_activatable_wallet_without_starting_it(monkeypatch):
    from anywhere_computer import diagnostics

    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "private-session-address")
    monkeypatch.setattr(diagnostics.shutil, "which", lambda _: "/usr/bin/gdbus")
    def missing_binding(_):
        raise ImportError("private dependency detail")

    monkeypatch.setattr(diagnostics, "import_module", missing_binding)
    calls = []

    def call(argv, **kwargs):
        from types import SimpleNamespace

        calls.append((argv, kwargs))
        output = (b"(['org.kde.kwalletd5', 'org.kde.kwalletd6'],)"
                  if argv[-1].endswith("ListActivatableNames") else
                  b"(['org.freedesktop.DBus', 'private-unrelated-service'],)")
        return SimpleNamespace(returncode=0, stdout=output)

    monkeypatch.setattr(diagnostics.subprocess, "run", call)
    report = diagnostics.linux_credential_services()
    assert report["state"] == "observed"
    assert report["services"] == {
        "org.freedesktop.secrets": {"running": False, "activatable": False},
        "org.kde.kwalletd5": {"running": False, "activatable": True},
        "org.kde.kwalletd6": {"running": False, "activatable": True},
    }
    assert report["kwallet_python_dbus_available"] is False
    assert report["service_activation_requested"] is False
    assert "private" not in json.dumps(report)
    assert len(calls) == 2
    assert [argv[-1] for argv, _ in calls] == [
        "org.freedesktop.DBus.ListNames", "org.freedesktop.DBus.ListActivatableNames",
    ]
    assert all(kwargs == {"capture_output": True, "timeout": 2, "check": False}
               for _, kwargs in calls)


@pytest.mark.parametrize("import_failure", [None, ImportError, OSError])
def test_kwallet_requires_an_importable_binding_even_without_bus_probe(monkeypatch, import_failure):
    from anywhere_computer import diagnostics

    def binding(name):
        assert name == "dbus"
        if import_failure is not None:
            raise import_failure("private-extension-error")
        return object()

    monkeypatch.setattr(diagnostics, "import_module", binding)
    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
    report = diagnostics.linux_credential_services()
    assert report["kwallet_python_dbus_available"] is (import_failure is None)
    assert report["state"] == "not_checked"
    assert "private-extension-error" not in json.dumps(report)


def test_linux_wallet_advertisement_is_not_usable_without_python_binding(monkeypatch):
    from anywhere_computer import diagnostics

    def unavailable():
        raise RuntimeError("No secure Python backend")

    monkeypatch.setattr(diagnostics, "secure_backend", unavailable)
    monkeypatch.setattr(diagnostics, "linux_credential_services", lambda: {
        "services": {"org.kde.kwalletd5": {"running": False, "activatable": True}},
        "kwallet_python_dbus_available": False,
    })
    report = diagnostics.linux_prerequisites()
    assert report["credential_store"]["selection"] == "unavailable"
    assert report["credential_store"]["reason"] == "kwallet_python_binding_unavailable"
    assert "cannot import dbus" in report["credential_store"]["next_action"]
    assert report["credential_store"]["credential_created"] is False


@pytest.mark.parametrize("failure", ["no_bus", "no_gdbus", "timeout", "malformed"])
def test_linux_bus_probe_failure_never_claims_missing_services(monkeypatch, failure):
    from types import SimpleNamespace

    from anywhere_computer import diagnostics

    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "private-bus")
    monkeypatch.setattr(diagnostics.shutil, "which", lambda _: (
        None if failure == "no_gdbus" else "/usr/bin/gdbus"
    ))
    if failure == "no_bus":
        monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS")

    def run(*args, **kwargs):
        import subprocess

        if failure in {"no_bus", "no_gdbus"}:
            pytest.fail("No bus environment or utility must not launch a probe")
        if failure == "timeout":
            raise subprocess.TimeoutExpired(args[0], 2, output=b"private-detail")
        return SimpleNamespace(returncode=0, stdout=b"private-invalid-response")

    monkeypatch.setattr(diagnostics.subprocess, "run", run)
    report = diagnostics.linux_credential_services()
    assert report["state"] in {"unavailable", "not_checked"}
    assert all(value == {"running": "unknown", "activatable": "unknown"}
               for value in report["services"].values())
    assert "private" not in json.dumps(report)


@pytest.mark.parametrize("tool", ["busctl", "dbus-send"])
def test_linux_bus_probe_uses_existing_read_only_fallback(monkeypatch, tool):
    from types import SimpleNamespace

    from anywhere_computer import diagnostics

    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "private-address")
    monkeypatch.setattr(diagnostics.shutil, "which", lambda name: (
        "/usr/bin/" + tool if name == tool else None
    ))
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        activated = argv[-1].endswith("ListActivatableNames")
        names = ["org.kde.kwalletd5", "org.kde.kwalletd6"] if activated else [":1.4"]
        if tool == "busctl":
            output = "as " + str(len(names)) + " " + " ".join(json.dumps(name) for name in names)
        else:
            output = 'method return time=0 sender=org.freedesktop.DBus reply_serial=1\n   array [\n'
            output += "".join('      string "' + name + '"\n' for name in names) + "   ]\n"
        return SimpleNamespace(returncode=0, stdout=output.encode())

    monkeypatch.setattr(diagnostics.subprocess, "run", run)
    report = diagnostics.linux_credential_services()
    assert report["state"] == "observed"
    assert report["probe_tool"] == tool
    assert report["services"]["org.kde.kwalletd5"] == {"running": False, "activatable": True}
    assert report["services"]["org.freedesktop.secrets"] == {"running": False, "activatable": False}
    assert report["service_activation_requested"] is False
    assert len(calls) == 2
    assert all(kwargs == {"capture_output": True, "timeout": 2, "check": False}
               for _, kwargs in calls)
    assert "private" not in json.dumps(report)


@pytest.mark.parametrize("tool,output", [
    ("gdbus", "(@as [],)"), ("busctl", "as 0"),
    ("dbus-send", "method return time=0\n   array [\n   ]\n"),
])
def test_bus_probe_accepts_empty_arrays(tool, output):
    from anywhere_computer.diagnostics import _credential_bus_names

    assert _credential_bus_names(tool, output) == set()


@pytest.mark.parametrize("tool,output", [
    ("gdbus", "('private-invalid-scalar',)"), ("busctl", 'as 2 "only-one"'),
    ("dbus-send", 'method return time=0\n   array [\n      int32 1\n   ]\n'),
])
def test_bus_probe_rejects_invalid_shape(tool, output):
    from anywhere_computer.diagnostics import _credential_bus_names

    with pytest.raises(ValueError):
        _credential_bus_names(tool, output)
