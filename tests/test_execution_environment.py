import asyncio
import json
import os
import sys

import pytest

from anywhere_computer import execution_environment


def test_only_path_is_augmented_and_existing_order_is_preserved(tmp_path, monkeypatch):
    installed = tmp_path / "installed"
    installed.mkdir()
    monkeypatch.setattr(execution_environment, "_standard_bins", lambda: (installed,))
    original = {"PATH": os.pathsep.join(("/existing", str(installed))), "HOME": "/home/test"}
    monkeypatch.setenv("PRIVATE_TOKEN", "must-not-be-inherited")
    result = execution_environment.with_tool_path(original)
    assert result == original
    assert result is not original
    assert execution_environment.with_tool_path({}) == {"PATH": str(installed)}


async def test_terminal_finds_later_installed_tool_with_restricted_path(tmp_path, monkeypatch):
    from anywhere_computer.models import StartSession
    from anywhere_computer.sessions import Sessions

    installed = tmp_path / "installed"
    monkeypatch.setattr(execution_environment, "_standard_bins", lambda: (installed,))
    windows = sys.platform == "win32"
    restricted = str(tmp_path / "missing") if windows else "/usr/bin:/bin"
    monkeypatch.setenv("PATH", restricted)
    sessions = Sessions()
    installed.mkdir()
    tool = installed / ("anywhere-fixture.cmd" if windows else "anywhere-fixture")
    tool.write_text("@echo off\n<nul set /p=fixture-42\n" if windows else
                    "#!/bin/sh\nprintf 'fixture-42'\n", encoding="utf-8")
    tool.chmod(0o700)
    started = await sessions.start(StartSession(
        command="anywhere-fixture", cwd=str(tmp_path),
        shell=os.environ["COMSPEC"] if windows else "/bin/sh",
    ))
    session = sessions.get(started["session_id"])
    try:
        await asyncio.wait_for(session.reader, 10)
        assert session.process.returncode == 0
        assert bytes(session.output) == b"fixture-42"
        assert os.environ["PATH"] == restricted
    finally:
        await sessions.close()


def test_windows_path_case_and_separator(tmp_path, monkeypatch):
    installed = tmp_path / "installed"
    installed.mkdir()
    monkeypatch.setattr(execution_environment.sys, "platform", "win32")
    monkeypatch.setattr(execution_environment, "_standard_bins", lambda: (installed,))
    original = {"Path": "C:\\Existing;" + str(installed).upper()}
    result = execution_environment.with_tool_path(original)
    assert result == {"PATH": original["Path"]}
    assert "Path" in original


def test_missing_relative_and_cwd_fallbacks_are_not_added(tmp_path, monkeypatch):
    from pathlib import Path

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(execution_environment, "_standard_bins", lambda: (
        tmp_path, tmp_path / "missing", Path("relative"),
    ))
    assert execution_environment.with_tool_path({"PATH": "/configured"}) == {
        "PATH": "/configured",
    }


async def test_mcp_child_gets_tool_path_without_unrelated_secrets(tmp_path, monkeypatch):
    from anywhere_computer.direct_mcp import DirectMCPContext

    installed = tmp_path / "installed"
    installed.mkdir()
    monkeypatch.setattr(execution_environment, "_standard_bins", lambda: (installed,))
    monkeypatch.setenv("PATH", str(tmp_path / "missing"))
    monkeypatch.setenv("ANYWHERE_SYNTHETIC_SECRET", "must-not-be-inherited")
    server = tmp_path / "environment.py"
    server.write_text('''import os
from mcp.server.fastmcp import FastMCP
m = FastMCP('path-test')
@m.tool()
def inspect() -> dict:
    return {'path': os.environ.get('PATH', ''),
            'private_present': 'ANYWHERE_SYNTHETIC_SECRET' in os.environ}
m.run(transport='stdio')
''', encoding="utf-8")
    context = DirectMCPContext([sys.executable, "-I", str(server)], tmp_path)
    await context.open()
    try:
        result = await context.call("inspect", {})
        value = json.loads(result["content"][0]["text"])
        assert str(installed) in value["path"].split(os.pathsep)
        assert value["private_present"] is False
    finally:
        await context.close()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX executable fixture")
def test_codex_and_diagnosis_resolve_the_same_fallback(tmp_path, monkeypatch):
    from anywhere_computer.codex_context import _executable
    from anywhere_computer.diagnostics import runtime_environment

    installed = tmp_path / "installed"
    installed.mkdir()
    program = installed / "codex"
    program.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    program.chmod(0o700)
    monkeypatch.delenv("ANYWHERE_CODEX_EXECUTABLE", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path / "missing"))
    monkeypatch.setattr(execution_environment, "_standard_bins", lambda: (installed,))
    assert _executable(None) == program.resolve()
    report = runtime_environment()
    assert report["executables_on_path"]["codex"] is None
    assert report["executables_for_new_children"]["codex"] == str(program)
