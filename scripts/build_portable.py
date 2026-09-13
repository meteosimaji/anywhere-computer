"""Bundle a trusted python-build-standalone runtime; no installer on the target PC."""

import argparse
import hashlib
import json
import os
import plistlib
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
        if path.is_symlink():
            if not path.resolve(strict=True).is_relative_to(source.resolve()):
                raise ValueError("Runtime symlink escapes its directory")
            if path.is_dir():
                raise ValueError("Runtime directory symlinks are unsupported")
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
            '"%~dp0runtime\\python.exe" -I -X utf8 "%~dp0setup_chatgpt.py" %*\n',
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


def publish_archive(staged: Path, output: Path) -> None:
    # Staging is on the destination filesystem. Linking publishes the complete
    # file atomically and refuses an existing destination, including a symlink.
    # A copy into the final pathname would leave a partial release on interruption.
    os.link(staged, output)


def include_manager(app: Path, manager: Path, *, platform: str) -> None:
    """Include an explicitly selected native build in the existing file manifest."""
    if not manager.is_absolute() or manager.is_symlink() or not manager.is_file():
        raise ValueError("Manager must be an absolute native executable file")
    if platform == "win32":
        shutil.copyfile(manager, app / "Anywhere Computer Manager.exe")
    elif platform == "darwin":
        contents = app / "Anywhere Computer Manager.app" / "Contents"
        binary = contents / "MacOS" / "anywhere-computer-manager"
        binary.parent.mkdir(parents=True)
        shutil.copyfile(manager, binary)
        binary.chmod(0o755)
        (contents / "Info.plist").write_bytes(plistlib.dumps({
            "CFBundleExecutable": binary.name,
            "CFBundleIdentifier": "org.anywherecomputer.manager.preview",
            "CFBundleName": "Anywhere Computer",
            "CFBundlePackageType": "APPL",
            "CFBundleShortVersionString": "0.1.0",
            "CFBundleVersion": "1",
            "NSHighResolutionCapable": True,
        }))
    else:
        raise ValueError("Native manager packaging supports macOS and Windows")


def build_portable(
    root: Path, runtime: Path, output: Path, *, allow_downloads: bool = False,
    manager: Path | None = None,
) -> Path:
    validate_runtime(runtime)
    if output.exists():
        raise ValueError("Output already exists; choose a new archive path")
    uv = shutil.which("uv")
    if uv is None:
        raise ValueError("The build machine needs uv")
    bundled = root / "plugins/anywhere-computer/bundled"
    checksums = json.loads((bundled / "checksums.json").read_text())
    release = json.loads((bundled / "release.json").read_text(encoding="utf-8"))
    if release.get("artifacts_sha256") != checksums:
        raise ValueError("Release metadata does not match bundled artifact hashes")
    wheels = [name for name in checksums if name.startswith("anywhere_computer-")
              and name.endswith(".whl") and Path(name).name == name]
    if len(wheels) != 1:
        raise ValueError("Plugin checksums must identify exactly one runtime wheel")
    for name in ("dependencies.txt", wheels[0]):
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
        wheel = bundled / wheels[0]
        requirements = stage / "requirements.txt"
        requirements.write_text((bundled / "dependencies.txt").read_text() +
                                f"\nanywhere-computer @ {wheel.as_uri()} " +
                                f"--hash=sha256:{checksums[wheel.name]}\n")
        subprocess.run([uv, "pip", "install", *([] if allow_downloads else ["--offline"]),
                        "--require-hashes",
                        "--python", str(interpreter), "--target", str(site),
                        "-r", str(requirements)], check=True)
        if os.name == "nt":
            (app / "anywhere.cmd").write_text(
                '@echo off\r\n"%~dp0runtime\\python.exe" -I -X utf8 -m anywhere_computer %*\r\n')
        else:
            launcher = app / "anywhere"
            launcher.write_text('#!/bin/sh\n'
                                'base=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 1\n'
                                'exec "$base/runtime/bin/python3" -I -m anywhere_computer "$@"\n')
            launcher.chmod(0o755)
        write_setup_launcher(app, windows=os.name == "nt")
        if manager is not None:
            include_manager(app, manager, platform=sys.platform)
        shutil.copyfile(root / "LICENSE", app / "LICENSE")
        (app / "README.txt").write_text(
            "Anywhere Computer portable alpha\n"
            + ("Open Anywhere Computer Manager to view status and configure local startup.\n"
               "Keep the manager and runtime together when moving this directory.\n"
               if manager is not None else "") +
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
                    "release": release,
                    "runtime_build": (runtime / "BUILD").read_text().strip(),
                    "bundled_inputs": checksums, "files": {}}
        manifest["files"] = {
            path.relative_to(app).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(app.rglob("*")) if path.is_file()
        }
        (app / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        archive_path = stage / "portable.zip"
        with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(app.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(stage))
        publish_archive(archive_path, output)
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, default=Path(sys.base_prefix))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--allow-downloads", action="store_true",
                        help="Allow hash-pinned dependency downloads on the build machine")
    parser.add_argument("--manager", type=Path,
                        help="Include a native management executable built for this platform")
    arguments = parser.parse_args()
    print(build_portable(Path(__file__).resolve().parents[1], arguments.runtime,
                         arguments.output.absolute(), allow_downloads=arguments.allow_downloads,
                         manager=arguments.manager))
