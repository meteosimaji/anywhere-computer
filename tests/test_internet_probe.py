import asyncio
import dataclasses
import hashlib
import json
import os
import runpy
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest
from test_client_tokens import MemoryVault

from anywhere_computer.cloudflare_tunnel import TunnelCredential
from anywhere_computer.downloads import DOWNLOAD_TOOLS
from anywhere_computer.http_service import configure_http
from anywhere_computer.models import Request


@pytest.mark.parametrize('pending_download', [False, True])
async def test_internet_probe_only_exposes_disposable_file(tmp_path, monkeypatch, pending_download):
    helper = runpy.run_path(str(Path(__file__).parents[1] / "scripts/verify_internet.py"))
    engine, target = helper["restricted_engine"](tmp_path)
    download_invocations = []
    if pending_download:
        monkeypatch.setattr('anywhere_computer.engine.OBSERVER_WAIT_SECONDS', 0.001)
        original = engine.tools['download_begin']
        async def delayed(args):
            download_invocations.append(args.path)
            await asyncio.sleep(0.03)
            return await original.handler(args)
        engine.tools['download_begin'] = dataclasses.replace(original, handler=delayed)
    outside = tmp_path / "not-exposed.txt"
    outside.write_text("private fixture", encoding="utf-8")

    async def call(tool, **arguments):
        reply = await engine.execute(
            Request(operation_id=uuid.uuid4().hex, tool=tool, arguments=arguments)
        )
        return await helper['complete_probe_operation'](engine, reply)

    try:
        assert set(engine.tools) == {"files_read", "files_write", "operations_get"} | DOWNLOAD_TOOLS
        for tool, args in [
            ("files_read", {"path": str(outside)}),
            ("files_write", {"path": str(outside), "text": "bad"}),
            ("files_write", {"path": str(target), "text": "x" * 1025}),
            ("download_begin", {"path": str(outside), "transfer_id": uuid.uuid4().hex}),
        ]:
            result = await call(tool, **args)
            assert result.state == "failed" and "Probe only permits" in result.error
        assert outside.read_text(encoding="utf-8") == "private fixture"
        assert (
            await call("terminal_start", command="echo forbidden", cwd=str(tmp_path))
        ).state == "failed"
        assert (await call("files_write", path=str(target), text="probe")).state == "completed"
        assert (await call("files_read", path=str(target))).data["text"] == "probe"
        binary = target.with_name("download.bin")
        prepared = await call("download_begin", path=str(binary), transfer_id=uuid.uuid4().hex)
        assert prepared.state == "completed" and prepared.data["total_bytes"] == 17 * 1024**2
        if pending_download:
            assert download_invocations == [str(outside), str(binary)]
        if os.name != "nt":
            target.unlink()
            target.symlink_to(outside)
            assert (await call("files_read", path=str(target))).state == "failed"
            assert (await call("files_write", path=str(target), text="bad")).state == "failed"
            binary.unlink()
            binary.symlink_to(outside)
            assert (
                await call("download_begin", path=str(binary), transfer_id=uuid.uuid4().hex)
            ).state == "failed"
    finally:
        await engine.close()


async def test_probe_cleanup_drains_final_child_identity_before_stopping_it():
    helper = runpy.run_path(str(Path(__file__).parents[1] / "scripts/verify_internet.py"))
    child_code = "import time; time.sleep(60)"
    parent_code = (
        "import subprocess,sys,json,time; "
        f"child=subprocess.Popen([sys.executable,'-c',{child_code!r}], "
        "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
        "print(json.dumps({'child':child.pid}),flush=True); time.sleep(60)"
    )
    # This fixture tests a direct runner/child relationship. Windows virtualenv
    # python.exe is a redirector, so use the base interpreter for this stdlib-only
    # fixture; otherwise the redirector introduces an unobserved extra process.
    parent = await asyncio.create_subprocess_exec(
        getattr(sys, "_base_executable", sys.executable),
        "-c",
        parent_code,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    identities = {}
    child = None
    try:
        line = await asyncio.wait_for(parent.stdout.readline(), 10)
        child = psutil.Process(json.loads(line)["child"])
        created = child.create_time()
        assert child.ppid() == parent.pid

        async def final_event():
            await parent.stdout.read()
            await asyncio.sleep(0.05)  # Lifecycle reader delivers its last observed event late.
            identities[child.pid] = created

        drain = asyncio.create_task(final_event())
        await helper["stop_probe_tunnel"](parent, identities, drain)
        assert parent.returncode is not None and drain.done()
        assert not psutil.pid_exists(child.pid)
    finally:
        if parent.returncode is None:
            parent.kill()
            await parent.wait()
        if child and child.is_running():
            child.kill()
            await asyncio.to_thread(child.wait, 5)


async def test_probe_cleanup_does_not_kill_a_reused_pid_identity():
    helper = runpy.run_path(str(Path(__file__).parents[1] / "scripts/verify_internet.py"))
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "import time; time.sleep(60)"
    )
    try:
        actual = psutil.Process(process.pid)
        await helper["stop_probe_tunnel"](None, {process.pid: actual.create_time() - 1})
        assert process.returncode is None and actual.is_running()
    finally:
        process.kill()
        await process.wait()


async def test_constant_probe_rejects_normal_or_changed_profile_before_launch(
    tmp_path, monkeypatch
):
    helper = runpy.run_path(str(Path(__file__).parents[1] / "scripts/verify_internet.py"))
    load = helper["load_probe_tunnel"]
    config = await configure_http(
        tmp_path,
        resource="https://probe.example/mcp",
        owner="probe",
        client="probe",
        port=18768,
        scopes=frozenset({"files_read"}),
        redirects=frozenset({"http://127.0.0.1/oauth/callback"}),
    )
    with pytest.raises(ValueError, match="dedicated provisioning marker"):
        load(tmp_path)
    vault = MemoryVault()
    credential = TunnelCredential(tmp_path, vault=vault)
    token = "synthetic_tunnel_token_0123456789"
    credential.install(token)
    monkeypatch.setitem(load.__globals__, "TunnelCredential", lambda path: credential)
    marker = {
        "purpose": "Anywhere Computer isolated constant HTTPS development probe",
        "phase": "route_ready_for_probe",
        "hostname": "probe.example",
        "port": 18768,
        "probe_binding": {
            "state_directory": str(tmp_path.resolve()),
            "resource": config.resource,
            "device": config.device,
            "port": config.port,
            "credential_account": credential.account,
            "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
        },
    }
    path = tmp_path / "provisioning.json"
    path.write_text(json.dumps(marker))
    assert load(tmp_path) == config
    credential.install(token + "changed")
    with pytest.raises(ValueError, match="dedicated probe enrollment"):
        load(tmp_path)
    credential.install(token)
    marker["probe_binding"]["state_directory"] = str(tmp_path / "elsewhere")
    path.write_text(json.dumps(marker))
    with pytest.raises(ValueError, match="dedicated binding"):
        load(tmp_path)


async def test_cleanup_continues_after_connector_failure_and_never_records_secret(monkeypatch):
    helper = runpy.run_path(str(Path(__file__).parents[1] / "scripts/verify_internet.py"))
    cleanup = helper["cleanup_probe"]
    calls = []

    async def fail_connector(*args):
        raise OSError("synthetic-secret-must-not-be-recorded")

    async def close_adapter():
        calls.append("adapter")

    async def close_engine():
        calls.append("engine")

    monkeypatch.setitem(cleanup.__globals__, "stop_probe_tunnel", fail_connector)
    report = {"completed": True}
    await cleanup(
        report,
        remote_client=None,
        tunnel=None,
        children={},
        drain=None,
        client_tokens=SimpleNamespace(forget=lambda: calls.append("client-secret")),
        owner_credentials=SimpleNamespace(forget=lambda: calls.append("owner-secret")),
        adapter=SimpleNamespace(close=close_adapter),
        store=SimpleNamespace(
            revoke_device=lambda **kwargs: calls.append("revoked"),
            close=lambda: calls.append("store"),
        ),
        engine=SimpleNamespace(close=close_engine),
    )
    assert set(calls) == {"revoked", "client-secret", "owner-secret", "adapter", "store", "engine"}
    assert report["client_keyring_removed"] and report["owner_keyring_removed"]
    assert report["completed"] is False and not report["owned_connector_processes_stopped"]
    assert "synthetic-secret" not in json.dumps(report)


def test_probe_runtime_binding_rejects_wrong_interpreter(tmp_path):
    helper = runpy.run_path(str(Path(__file__).parents[1] / "scripts/verify_internet.py"))
    with pytest.raises(ValueError, match="interpreter"):
        helper["require_runtime_root"](tmp_path)


def test_probe_runtime_binding_detects_application_overlay(tmp_path, monkeypatch):
    helper = runpy.run_path(str(Path(__file__).parents[1] / "scripts/verify_internet.py"))
    monkeypatch.setattr(sys, "executable", str(tmp_path / "python"))
    modules = [module for name, module in sys.modules.items()
               if name == "anywhere_computer" or name.startswith("anywhere_computer.")]
    for index, module in enumerate(modules):
        monkeypatch.setattr(module, "__file__", str(tmp_path / f"module{index}.py"))
    assert helper["require_runtime_root"](tmp_path) == len(modules)
    monkeypatch.setattr(modules[-1], "__file__", str(tmp_path.parent / "overlay.py"))
    with pytest.raises(ValueError, match="application import"):
        helper["require_runtime_root"](tmp_path)


async def test_probe_pending_result_has_bounded_recovery_without_mutation_replay():
    from anywhere_computer.models import Reply
    helper = runpy.run_path(str(Path(__file__).parents[1] / 'scripts/verify_internet.py'))
    identity = 'a' * 32
    calls = []
    async def execute(request):
        calls.append(request)
        return Reply(operation_id=request.operation_id, state='completed', data={
            'operation_id': identity, 'state': 'running', 'data': {}, 'error': None,
        })
    with pytest.raises(TimeoutError):
        await helper['complete_probe_operation'](SimpleNamespace(execute=execute),
            Reply(operation_id=identity, state='running'), timeout=0.15)
    assert calls
    assert all(r.tool == 'operations_get' and r.arguments == {'operation_id': identity}
               for r in calls)
    assert len({r.operation_id for r in calls}) == len(calls)


async def test_probe_recovery_rejects_another_operations_result():
    from anywhere_computer.models import Reply
    helper = runpy.run_path(str(Path(__file__).parents[1] / 'scripts/verify_internet.py'))
    async def execute(request):
        return Reply(operation_id=request.operation_id, state='completed', data={
            'operation_id': 'b' * 32, 'state': 'completed', 'data': {}, 'error': None,
        })
    with pytest.raises(RuntimeError, match='identity mismatch'):
        await helper['complete_probe_operation'](SimpleNamespace(execute=execute),
            Reply(operation_id='a' * 32, state='running'))
