import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "portable_verifier", Path(__file__).resolve().parents[1] / "scripts/verify_portable.py",
)
assert spec is not None and spec.loader is not None
portable_verifier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(portable_verifier)


def test_manifest_rejects_changed_payload(tmp_path):
    payload = tmp_path / "payload"
    payload.write_bytes(b"original")
    (tmp_path / "manifest.json").write_text(json.dumps({"files": {
        "payload": hashlib.sha256(b"original").hexdigest(),
    }}))
    assert portable_verifier.verify_manifest(tmp_path) == 1
    payload.write_bytes(b"modified")
    with pytest.raises(ValueError, match="checksum"):
        portable_verifier.verify_manifest(tmp_path)


def test_repeated_verification_does_not_write_bytecode(tmp_path, monkeypatch, capsys):
    runtime = tmp_path / "runtime" / ("" if os.name == "nt" else "bin")
    runtime.mkdir(parents=True)
    interpreter = runtime / ("python.exe" if os.name == "nt" else "python3")
    interpreter.write_bytes(b"fixture")
    package = tmp_path / "package"
    package.mkdir()
    module = package / "worker_module.py"
    module.write_text("VALUE = 42\n")
    (tmp_path / "manifest.json").write_text(json.dumps({"files": {
        interpreter.relative_to(tmp_path).as_posix():
            hashlib.sha256(interpreter.read_bytes()).hexdigest(),
        "package/worker_module.py": hashlib.sha256(module.read_bytes()).hexdigest(),
    }}))
    commands = []
    real_run = subprocess.run

    def run(command, **options):
        commands.append(command)
        assert command[:3] == [str(interpreter), "-B", "-I"]
        assert options["check"] is True
        program = "import sys; sys.path.insert(0, sys.argv[1]); import package.worker_module"
        real_run([sys.executable, *command[1:3], "-c", program, str(tmp_path)],
                 cwd=options["cwd"], env=options["env"], check=True)
        return subprocess.CompletedProcess(command, 0, '{"files_roundtrip": true}')

    monkeypatch.setattr(portable_verifier.subprocess, "run", run)
    monkeypatch.setattr(sys, "argv", ["verify_portable.py", str(tmp_path), "--runtime-only"])
    portable_verifier.main()
    portable_verifier.main()
    assert len(commands) == 2
    assert all("--runtime-only" in command for command in commands)
    output = capsys.readouterr().out.splitlines()
    assert [json.loads(line)["manifest_files"] for line in output] == [2, 2]
    assert not list(tmp_path.rglob("*.pyc"))


def test_manifest_rejects_path_escape(tmp_path):
    (tmp_path / "manifest.json").write_text(json.dumps({"files": {"../outside": "a" * 64}}))
    with pytest.raises(ValueError, match="member path"):
        portable_verifier.verify_manifest(tmp_path)


@pytest.mark.parametrize("extra", ["sitecustomize.py", "package/injected.pth",
                                   "package/__pycache__/cached.pyc"])
def test_manifest_rejects_unlisted_code(tmp_path, extra):
    (tmp_path / "payload").write_bytes(b"original")
    (tmp_path / "manifest.json").write_text(json.dumps({"files": {
        "payload": hashlib.sha256(b"original").hexdigest(),
    }}))
    additional = tmp_path / extra
    additional.parent.mkdir(parents=True, exist_ok=True)
    additional.write_bytes(b"unlisted")
    with pytest.raises(ValueError, match="Unexpected portable files"):
        portable_verifier.verify_manifest(tmp_path)
