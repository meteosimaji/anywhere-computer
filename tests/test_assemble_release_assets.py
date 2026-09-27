"""A release must contain only the five archives from the same clean build."""

import hashlib
import importlib.util
import json
import zipfile
from pathlib import Path

import pytest


@pytest.fixture
def assembly(monkeypatch):
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location(
        "assemble_release_assets", scripts / "assemble_release_assets.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def archives(tmp_path, assembly):
    root = tmp_path / "source"
    source = root / "src/anywhere_computer/__init__.py"
    source.parent.mkdir(parents=True)
    source.write_text('__version__ = "1.2.3b4"\n')
    bundled = root / "plugins/anywhere-computer/bundled"
    bundled.mkdir(parents=True)
    release = {
        "version": "1.2.3-beta.4", "python_version": "1.2.3b4",
        "source_commit": "a" * 40, "source_dirty": False,
    }
    (bundled / "release.json").write_text(json.dumps(release))
    incoming = tmp_path / "incoming"
    for artifact, source_name, managed in assembly.ARTIFACTS:
        platform = {"ubuntu": "linux-x86_64", "windows": "win-amd64",
                    "macos": "macosx-11.0-arm64"}[artifact.split("-")[-2]]
        manager = ("Anywhere Computer Manager.exe" if "windows" in artifact else
                   "Anywhere Computer Manager.app/Contents/MacOS/anywhere-computer-manager")
        manifest = {"release": release, "platform": platform,
                    "files": {manager: "a" * 64} if managed else {}}
        path = incoming / artifact / source_name
        path.parent.mkdir(parents=True)
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("Anywhere Computer/manifest.json", json.dumps(manifest))
    return root, incoming


def test_assemble_names_and_hashes_five_matching_archives(assembly, archives, tmp_path):
    root, incoming = archives
    output = tmp_path / "release"
    assert assembly.assemble(root, incoming, output) == "v1.2.3b4"
    checksums = (output / "SHA256SUMS").read_text().splitlines()
    assert len(checksums) == 5
    assert len(list(output.iterdir())) == 6
    for line in checksums:
        digest, name = line.split("  ")
        assert digest == hashlib.sha256((output / name).read_bytes()).hexdigest()
    assert {path.name for path in output.iterdir()} == {
        "SHA256SUMS", "anywhere-computer-1.2.3b4-linux-x86_64.zip",
        "anywhere-computer-1.2.3b4-windows-x86_64.zip",
        "anywhere-computer-1.2.3b4-macos-arm64.zip",
        "anywhere-computer-1.2.3b4-managed-windows-x86_64.zip",
        "anywhere-computer-1.2.3b4-managed-macos-arm64.zip",
    }


@pytest.mark.parametrize("change, message", [
    ("dirty", "clean build"), ("mismatched release", "metadata differs"),
    ("missing manager", "native manager"), ("wrong platform", "CI runner"),
])
def test_assemble_rejects_unverified_or_mismatched_inputs(
    assembly, archives, tmp_path, change, message,
):
    root, incoming = archives
    if change == "dirty":
        path = root / "plugins/anywhere-computer/bundled/release.json"
        release = json.loads(path.read_text())
        release["source_dirty"] = True
        path.write_text(json.dumps(release))
    else:
        path = incoming / "managed-portable-windows-latest/managed-portable.zip"
        with zipfile.ZipFile(path) as archive:
            manifest = json.loads(archive.read("Anywhere Computer/manifest.json"))
        if change == "mismatched release":
            manifest["release"]["python_version"] = "0.0.0"
        elif change == "missing manager":
            manifest["files"] = {}
        else:
            manifest["platform"] = "linux-x86_64"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("Anywhere Computer/manifest.json", json.dumps(manifest))
    with pytest.raises(ValueError, match=message):
        assembly.assemble(root, incoming, tmp_path / "release")
    assert not (tmp_path / "release").exists()
