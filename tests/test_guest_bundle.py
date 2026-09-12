import importlib.util
import json
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
             "tests/test_example.py", "scripts/verify_example.py"]
    names += ["plugins/anywhere-computer/" + name for name in (
        ".codex-plugin/plugin.json", ".mcp.json", "LICENSE", "skills/computer-work/SKILL.md",
        "bundled/checksums.json", "bundled/dependencies.txt", "bundled/release.json",
        "bundled/anywhere_computer-0.9.7-py3-none-any.whl",
    )]
    for name in [*names, ".env", "local-state/private.json", "output/private.txt"]:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(name)
    (root / "plugins/anywhere-computer/bundled/checksums.json").write_text(json.dumps({
        "anywhere_computer-0.9.7-py3-none-any.whl": "fixture-digest",
    }))
    monkeypatch.setattr(guest_builder.subprocess, "check_output", lambda *a, **k: "fixture\n")
    output = guest_builder.build_guest_bundle(root, tmp_path / "guest.zip")
    with zipfile.ZipFile(output) as archive:
        assert set(archive.namelist()) == set(names) | {"bundle-manifest.json"}
        manifest = json.loads(archive.read("bundle-manifest.json"))
        assert set(manifest["files_sha256"]) == set(names)
        assert manifest["source_revision"] == "fixture"
