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
