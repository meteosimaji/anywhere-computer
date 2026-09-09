"""Bundle a trusted python-build-standalone runtime; no installer on the target PC."""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path


def validate_runtime(source: Path) -> None:
    if not source.is_absolute() or source.is_symlink() or not source.is_dir():
        raise ValueError("Runtime must be an absolute, real directory")
    if (source / "pyvenv.cfg").exists() or not (source / "BUILD").is_file():
        raise ValueError("Use a trusted python-build-standalone base, not a virtualenv")
    for path in source.rglob("*"):
        if path.is_symlink() and not path.resolve(strict=True).is_relative_to(source.resolve()):
            raise ValueError("Runtime symlink escapes its directory")
        if not path.is_file() and not path.is_dir():
            raise ValueError("Runtime contains a special file")


def write_setup_launcher(app: Path, *, windows: bool) -> None:
    (app / "setup_chatgpt.py").write_text(
        "import sys\n"
        "from anywhere_computer.cli import main\n"
        "from anywhere_computer.state import state_directory\n"
        "sys.argv = ['anywhere', 'chatgpt-setup', '--state-dir',\n"
        "            str(state_directory() / 'chatgpt'), *sys.argv[1:]]\n"
        "try:\n"
        "    main()\n"
        "finally:\n"
        "    if sys.stdin.isatty() and '--help' not in sys.argv:\n"
        "        try: input('\\nPress Return to close this window...')\n"
        "        except (EOFError, KeyboardInterrupt): pass\n",
        encoding="utf-8",
    )
    if windows:
        (app / "Setup ChatGPT.cmd").write_text(
            '@echo off\nsetlocal DisableDelayedExpansion\n'
            '"%~dp0runtime\\python.exe" -I "%~dp0setup_chatgpt.py" %*\n',
            encoding="utf-8",
        )
    else:
        launcher = app / "Setup ChatGPT.command"
        launcher.write_text(
            '#!/bin/sh\n'
            'base=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 1\n'
            'exec "$base/runtime/bin/python3" -I "$base/setup_chatgpt.py" "$@"\n',
            encoding="utf-8",
        )
        launcher.chmod(0o755)


def build_portable(root: Path, runtime: Path, output: Path) -> Path:
    validate_runtime(runtime)
    if output.exists():
        raise ValueError("Output already exists; choose a new archive path")
    uv = shutil.which("uv")
    if uv is None:
        raise ValueError("The build machine needs uv")
    bundled = root / "plugins/anywhere-computer/bundled"
    checksums = json.loads((bundled / "checksums.json").read_text())
    for name in ("dependencies.txt", "anywhere_computer-0.1.0a1-py3-none-any.whl"):
        if hashlib.sha256((bundled / name).read_bytes()).hexdigest() != checksums[name]:
            raise ValueError("Bundled artifact checksum mismatch")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="portable-build-", dir=output.parent) as temporary:
        stage = Path(temporary)
        app = stage / "Anywhere Computer"
        copied = app / "runtime"
        shutil.copytree(runtime, copied, symlinks=False,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        interpreter = copied / ("python.exe" if os.name == "nt" else "bin/python3")
        site = Path(subprocess.check_output(
            [str(interpreter), "-I", "-c", "import sysconfig;print(sysconfig.get_path('purelib'))"],
            text=True,
        ).strip())
        if not site.resolve().is_relative_to(copied.resolve()):
            raise ValueError("Copied interpreter is not relocatable")
        wheel = bundled / "anywhere_computer-0.1.0a1-py3-none-any.whl"
        requirements = stage / "requirements.txt"
        requirements.write_text((bundled / "dependencies.txt").read_text() +
                                f"\nanywhere-computer @ {wheel.as_uri()} " +
                                f"--hash=sha256:{checksums[wheel.name]}\n")
        subprocess.run([uv, "pip", "install", "--offline", "--require-hashes",
                        "--python", str(interpreter), "--target", str(site),
                        "-r", str(requirements)], check=True)
        if os.name == "nt":
            (app / "anywhere.cmd").write_text(
                '@echo off\r\n"%~dp0runtime\\python.exe" -I -m anywhere_computer %*\r\n')
        else:
            launcher = app / "anywhere"
            launcher.write_text('#!/bin/sh\n'
                                'base=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 1\n'
                                'exec "$base/runtime/bin/python3" -I -m anywhere_computer "$@"\n')
            launcher.chmod(0o755)
        write_setup_launcher(app, windows=os.name == "nt")
        shutil.copyfile(root / "LICENSE", app / "LICENSE")
        (app / "README.txt").write_text(
            "Anywhere Computer portable alpha\n"
            "Run ./anywhere --help (Windows: anywhere.cmd --help).\n"
            "Double-click Setup ChatGPT.command on macOS, or Setup ChatGPT.cmd on Windows.\n"
            "Run chatgpt-setup with an explicit --state-dir to configure ChatGPT.\n"
            "Public HTTPS and ChatGPT authorization are still required.\n"
            "Python and dependencies retain their own licenses under runtime/.\n"
            "Build provenance is recorded in manifest.json. No credentials are bundled.\n")
        runtime_info = json.loads(subprocess.check_output(
            [str(interpreter), "-I", "-c", "import json,sys,sysconfig;"
             "print(json.dumps({'platform':sysconfig.get_platform(),"
             "'python':sys.version.split()[0]}))"], text=True,
        ))
        manifest = {**runtime_info,
                    "runtime_build": (runtime / "BUILD").read_text().strip(),
                    "bundled_inputs": checksums, "files": {}}
        manifest["files"] = {
            str(path.relative_to(app)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(app.rglob("*")) if path.is_file()
        }
        (app / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        archive_path = stage / "portable.zip"
        with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(app.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(stage))
        # Exclusive creation prevents a concurrent build from replacing an existing archive.
        with output.open("xb") as destination, archive_path.open("rb") as source:
            shutil.copyfileobj(source, destination)
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, default=Path(sys.base_prefix))
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    print(build_portable(Path(__file__).resolve().parents[1], arguments.runtime,
                         arguments.output.absolute()))
