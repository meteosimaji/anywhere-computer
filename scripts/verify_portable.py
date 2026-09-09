"""Verify a trusted, extracted portable build, using only its bundled interpreter."""

import argparse
import asyncio
import hashlib
import json
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


def verify_manifest(app: Path) -> int:
    if (app / "manifest.json").is_symlink():
        raise ValueError("Invalid portable manifest path")
    manifest = json.loads((app / "manifest.json").read_text())
    files = manifest.get("files") if isinstance(manifest, dict) else None
    if not isinstance(files, dict) or not files:
        raise ValueError("Invalid portable file manifest")
    for name, digest in files.items():
        if (not isinstance(name, str) or Path(name).is_absolute()
                or name != Path(name).as_posix() or ".." in Path(name).parts
                or name == "manifest.json"):
            raise ValueError("Invalid portable member path")
        path = app / name
        if not path.resolve().is_relative_to(app.resolve()) or path.is_symlink():
            raise ValueError("Invalid portable member path")
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError("Portable checksum mismatch")
    observed = set()
    for path in app.rglob("*"):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise ValueError("Invalid portable member type")
        if path.is_file():
            observed.add(path.relative_to(app).as_posix())
    if observed != set(files) | {"manifest.json"}:
        raise ValueError("Unexpected portable files; verify a freshly extracted build")
    return len(files)


async def exercise(directory: Path) -> dict[str, bool]:
    # Imports must come from the relocated runtime, not this repository.
    from anywhere_computer.connection import exchange
    from anywhere_computer.regex_worker import regex_line_numbers

    async def rpc(tool: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        reply = await exchange(directory, tool, arguments)
        assert reply.state == "completed", f"Fixture call failed: {tool}"
        return reply.data

    for _ in range(150):
        try:
            before = await rpc("__status")
            break
        except (OSError, ConnectionError):
            await asyncio.sleep(0.1)
    else:
        raise AssertionError("Portable agent did not become ready")
    path = directory / "日本語.txt"
    await rpc("files_write", {"path": str(path), "text": "portable-file"})
    assert (await rpc("files_read", {"path": str(path)}))["text"] == "portable-file"
    assert await regex_line_numbers("first\nneedle", "needle", ignore_case=False,
                                    whole_word=False, limit=1, timeout=5) == [2]
    argv = [sys.executable, "-I", "-u", "-c",
            "import sys; print('READY', flush=True); "
            "line=input(); print('RECONNECTED:'+line, flush=True)"]
    command = subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)
    session = await rpc("terminal_start", {"command": command, "cwd": str(directory)})
    identity = session["session_id"]
    try:
        # Every exchange closes its socket. The session must outlive the first client.
        output = await rpc("terminal_output", {"session_id": identity, "wait_ms": 3000})
        assert "READY" in output["text"]
        after = await rpc("__status")
        assert before["instance_id"] == after["instance_id"]
        denied = await exchange(directory, "__stop")
        assert denied.state == "failed"
        response = await rpc("terminal_input", {
            "session_id": identity, "text": "portable\n", "wait_ms": 3000,
            "wait_for_prompt": "RECONNECTED:portable",
        })
        assert "RECONNECTED:portable" in response["text"]
    finally:
        await exchange(directory, "terminal_stop", {"session_id": identity})
    await rpc("__stop")
    return {"agent_started": True, "files_roundtrip": True, "regex_child": True,
            "terminal_reconnected": True, "busy_stop_refused": True}


def runtime_worker() -> None:
    from anywhere_computer.files import Files
    from anywhere_computer.models import ReadFile, WriteFile
    from anywhere_computer.regex_worker import regex_line_numbers
    from anywhere_computer.state import prepare_directory

    with tempfile.TemporaryDirectory(prefix="portable-core-") as raw:
        directory = Path(raw)
        prepare_directory(directory / "state")
        files = Files(directory / "state")
        path = directory / "日本語.txt"
        files.write(WriteFile(path=str(path), text="runtime-only"))
        assert files.read(ReadFile(path=str(path)))["text"] == "runtime-only"
        assert asyncio.run(regex_line_numbers(
            "first\nneedle", "needle", ignore_case=False,
            whole_word=False, limit=1, timeout=5,
        )) == [2]
    print(json.dumps({"files_roundtrip": True, "regex_child": True,
                      "native_agent_tested": False}))


def worker() -> None:
    from anywhere_computer.credentials import SERVICE, local_credential, secure_backend

    with tempfile.TemporaryDirectory(prefix="portable-agent-") as raw:
        directory = Path(raw).resolve()
        account = "local-agent-" + hashlib.sha256(str(directory).encode()).hexdigest()[:24]
        backend = secure_backend()
        assert backend.get_password(SERVICE, account) is None
        process = None
        try:
            local_credential(directory, create=True)
            process = subprocess.Popen(
                [sys.executable, "-I", "-m", "anywhere_computer", "serve",
                 "--state-dir", str(directory)], stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            result = asyncio.run(exercise(directory))
            assert process.wait(timeout=10) == 0
        finally:
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            if backend.get_password(SERVICE, account) is not None:
                backend.delete_password(SERVICE, account)
            assert backend.get_password(SERVICE, account) is None
        result.update(agent_exited=True, fixture_credential_removed=True)
        print(json.dumps(result))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("app", nargs="?", type=Path)
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--runtime-only", action="store_true",
                        help="Check files and child interpreter without accessing an OS vault")
    args = parser.parse_args()
    if args.worker:
        if args.runtime_only:
            runtime_worker()
        else:
            worker()
        return
    if args.app is None:
        parser.error("app directory is required")
    app = args.app.resolve()
    count = verify_manifest(app)
    interpreter = app / "runtime" / ("python.exe" if os.name == "nt" else "bin/python3")
    env = os.environ.copy()
    env.update(PYTHONHOME="/nonexistent", PYTHONPATH="/nonexistent")
    env["PATH"] = (str(Path(os.environ["SystemRoot"]) / "System32")
                   if os.name == "nt" else "/usr/bin:/bin")
    with tempfile.TemporaryDirectory(prefix="unrelated-cwd-") as cwd:
        run = subprocess.run([str(interpreter), "-I", str(Path(__file__).resolve()), "--worker",
                              *(["--runtime-only"] if args.runtime_only else [])],
                             cwd=cwd, env=env, check=True, text=True, capture_output=True,
                             timeout=60)
    result = json.loads(run.stdout)
    result["manifest_files"] = count
    print(json.dumps(result))


if __name__ == "__main__":
    main()
