import os

import pytest

from anywhere_computer import renderer_location


def test_bundled_renderers_win_over_path_without_mixing_partial_bundle(tmp_path, monkeypatch):
    if os.name == 'nt':
        pytest.skip('Executable mode bits in macOS bundles are not represented on Windows')
    app = tmp_path / "relocated 日本語" / "Anywhere Computer"
    python = app / "runtime/bin/python3"
    python.parent.mkdir(parents=True)
    python.write_text("fixture")
    monkeypatch.setattr(renderer_location.sys, "executable", str(python))
    monkeypatch.setattr(renderer_location.shutil, "which", lambda name: f"/host/{name}")
    assert renderer_location.find_renderer("soffice") == "/host/soffice"
    bundle = app / "renderers/macos"
    office = bundle / "LibreOffice.app/Contents/MacOS/soffice"
    office.parent.mkdir(parents=True)
    office.write_text("fixture")
    office.chmod(0o755)
    assert renderer_location.find_renderer("soffice") == str(office)
    assert renderer_location.find_renderer("pdfinfo") is None
    assert renderer_location.find_renderer("sandbox-exec") == "/host/sandbox-exec"
    office.chmod(0o644)
    assert renderer_location.find_renderer("soffice") is None
