"""Regression checks for the isolated probe's executable preflight."""

import os
import runpy
import sys
from pathlib import Path

import pytest


@pytest.fixture
def probe(monkeypatch):
    # The standalone script prepends src; keep that change local to this test.
    monkeypatch.setattr(sys, "path", list(sys.path))
    script = Path(__file__).resolve().parents[1] / "scripts" / "verify_codex_plugins.py"
    return runpy.run_path(str(script), run_name="probe_regression")


class PreflightPassed(Exception):
    """Stop before creating a fixture directory or launching any process."""


async def test_probe_honors_explicit_codex_with_empty_path(probe, tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    program = bindir / ("codex.exe" if os.name == "nt" else "codex")
    program.write_bytes(b"fixture-not-executed\n")
    program.chmod(0o700)
    monkeypatch.setenv("PATH", "")
    monkeypatch.setenv("ANYWHERE_CODEX_EXECUTABLE", str(program))

    def stop_after_preflight(*args, **kwargs):
        raise PreflightPassed

    monkeypatch.setattr(probe["tempfile"], "TemporaryDirectory", stop_after_preflight)
    with pytest.raises(PreflightPassed):
        await probe["main"]()


@pytest.mark.parametrize("kind", ["missing", "relative"])
async def test_probe_rejects_invalid_explicit_codex(probe, tmp_path, monkeypatch, kind):
    selected = str(tmp_path / "missing-codex") if kind == "missing" else "relative/codex"
    monkeypatch.setenv("ANYWHERE_CODEX_EXECUTABLE", selected)

    def forbidden_fixture(*args, **kwargs):
        pytest.fail("invalid executable must fail before creating a fixture")

    monkeypatch.setattr(probe["tempfile"], "TemporaryDirectory", forbidden_fixture)
    with pytest.raises(ValueError, match="Codex executable"):
        await probe["main"]()
