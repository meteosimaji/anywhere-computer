import importlib.util
import json
import subprocess
import sys
import zipfile
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "guest_builder", Path(__file__).resolve().parents[1] / "scripts/build_guest_test_bundle.py",
)
assert spec is not None and spec.loader is not None
guest_builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guest_builder)


def test_guest_bundle_includes_runtime_assets_but_not_local_state(tmp_path, monkeypatch):
    root = tmp_path / "source"
    names = ["pyproject.toml", "uv.lock", "LICENSE", "README.md", "docs/ARCHITECTURE.md",
             "src/anywhere_computer/example.py", "src/anywhere_computer/web/workspace.html",
             "tests/test_example.py", "tests/conftest.py", "tests/example.cjs",
             "tests/fixtures/native_gui.swift", "tests/fixtures/audio/probe_harness.swift",
             "tests/fixtures/example.json", "native/macos/AXHelper.swift",
             "desktop/ui/app.js", "desktop/ui/style.css",
             "scripts/verify_example.py", "scripts/receipt.js",
             "scripts/probe_audio_capture.swift", "scripts/windows_local_startup.ps1",
             "scripts/ci/windows_test_costs.json", ".github/workflows/quality.yml",
             ".claude-plugin/marketplace.json", ".agents/plugins/marketplace.json",
             "README.ja.md", "docs/SUBCHAT-PROBE.md",
             "src/anywhere_computer/subchat_browser/backend.py",
             "src/anywhere_computer/subchat_browser/input.js"]
    names += ["plugins/anywhere-computer/" + name for name in (
        ".codex-plugin/plugin.json", ".mcp.json", ".claude-plugin/plugin.json",
        ".claude-mcp.json", "LICENSE", "skills/computer-work/SKILL.md",
        "skills/subchat/SKILL.md",
        "bundled/checksums.json", "bundled/dependencies.txt", "bundled/release.json",
        "bundled/anywhere_computer-0.9.7-py3-none-any.whl",
    )]
    for name in [*names, ".env", "local-state/private.json", "output/private.txt",
                  "tests/fixtures/private.sqlite3", "tests/fixtures/local.env"]:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(name)
    (root / "plugins/anywhere-computer/bundled/checksums.json").write_text(json.dumps({
        "anywhere_computer-0.9.7-py3-none-any.whl": "fixture-digest",
    }))
    (root / "tests/conftest.py").write_text(
        "import pytest\n@pytest.fixture\ndef shared_fixture(): return 'packaged fixture'\n")
    (root / "tests/test_example.py").write_text(
        "def test_fixture(shared_fixture): assert shared_fixture == 'packaged fixture'\n")
    (root / "pyproject.toml").write_text('[tool.pytest.ini_options]\n')
    monkeypatch.setattr(guest_builder.subprocess, "check_output", lambda *a, **k: "fixture\n")
    output = guest_builder.build_guest_bundle(root, tmp_path / "guest.zip")
    with zipfile.ZipFile(output) as archive:
        assert set(archive.namelist()) == set(names) | {"bundle-manifest.json"}
        manifest = json.loads(archive.read("bundle-manifest.json"))
        assert set(manifest["files_sha256"]) == set(names)
        assert manifest["source_revision"] == "fixture"

        extracted = tmp_path / 'extracted'
        archive.extractall(extracted)
    result = subprocess.run(
        [sys.executable, '-I', '-m', 'pytest', 'tests/test_example.py', '-q'],
        cwd=extracted, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert '1 passed' in result.stdout
