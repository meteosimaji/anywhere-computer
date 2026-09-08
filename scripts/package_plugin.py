"""Build a portable plugin containing our wheel and pinned runtime dependencies."""

import hashlib
import json
import shutil
import subprocess
import zipfile
from pathlib import Path


def package_plugin(root: Path) -> Path:
    plugin = root / "plugins/anywhere-computer"
    bundled = plugin / "bundled"
    bundled.mkdir(exist_ok=True)
    subprocess.run(["uv", "build", "--wheel"], cwd=root, check=True)
    wheel = root / "dist/anywhere_computer-0.1.0a1-py3-none-any.whl"
    shutil.copyfile(wheel, bundled / wheel.name)
    subprocess.run(
        [
            "uv",
            "export",
            "--locked",
            "--no-dev",
            "--no-emit-project",
            "--no-header",
            "--output-file",
            str(bundled / "dependencies.txt"),
        ],
        cwd=root,
        check=True,
        stdout=subprocess.DEVNULL,
    )
    hashes = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (bundled / wheel.name, bundled / "dependencies.txt")
    }
    (bundled / "checksums.json").write_text(json.dumps(hashes, indent=2) + "\n")
    output = root / "dist/anywhere-computer-plugin.zip"
    members = [
        plugin / ".codex-plugin/plugin.json",
        plugin / ".mcp.json",
        plugin / "LICENSE",
        plugin / "skills/computer-work/SKILL.md",
        bundled / wheel.name,
        bundled / "dependencies.txt",
        bundled / "checksums.json",
    ]
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in members:
            if path.is_symlink():
                raise ValueError("Plugin artifacts must not be symlinks")
            archive.write(path, path.relative_to(plugin))
    return output


if __name__ == "__main__":
    print(package_plugin(Path(__file__).resolve().parents[1]))
