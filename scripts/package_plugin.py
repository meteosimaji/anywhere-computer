"""Build a portable plugin containing our wheel and pinned runtime dependencies."""

import ast
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import zipfile
from email.parser import Parser
from pathlib import Path


def plugin_version(python_version: str) -> str:
    match = re.fullmatch(r"(\d+\.\d+\.\d+)(?:(a|b|rc)(\d+))?", python_version)
    if match is None:
        raise ValueError("Release version must be major.minor.patch with optional a/b/rc number")
    if match[2] is None:
        return match[1]
    label = {"a": "alpha", "b": "beta", "rc": "rc"}[match[2]]
    return f"{match[1]}-{label}.{match[3]}"


def package_plugin(root: Path) -> Path:
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    if re.fullmatch(r"[a-f0-9]{40}", commit) is None:
        raise ValueError("Build needs an identifiable Git commit")
    dirty = bool(subprocess.check_output(
        ["git", "status", "--porcelain", "--untracked-files=normal"], cwd=root, text=True,
    ).strip())
    plugin = root / "plugins/anywhere-computer"
    bundled = plugin / "bundled"
    bundled.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="anywhere-wheel-") as temporary:
        subprocess.run(["uv", "build", "--wheel", "--out-dir", temporary], cwd=root, check=True)
        wheels = list(Path(temporary).glob("anywhere_computer-*.whl"))
        if len(wheels) != 1:
            raise ValueError("Build must produce exactly one Anywhere Computer wheel")
        wheel = bundled / wheels[0].name
        shutil.copyfile(wheels[0], wheel)
    with zipfile.ZipFile(wheel) as archive:
        metadata_paths = [name for name in archive.namelist()
                          if name.endswith(".dist-info/METADATA")]
        if len(metadata_paths) != 1:
            raise ValueError("Wheel must contain one package metadata file")
        metadata = Parser().parsestr(archive.read(metadata_paths[0]).decode("utf-8"))
        if metadata["Name"] != "anywhere-computer":
            raise ValueError("Wheel package name does not match this plugin")
        version = plugin_version(metadata["Version"])
    manifest_path = plugin / ".codex-plugin/plugin.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["version"] = version
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    config_path = plugin / ".mcp.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    arguments = config["mcpServers"]["anywhere-computer"]["args"]
    arguments[arguments.index("--from") + 1] = "./bundled/" + wheel.name
    config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    subprocess.run(
        [
            "uv",
            "export",
            "--locked",
            "--no-dev",
            "--extra",
            "mcp",
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
    # Read declarations from the wheel being distributed, never an unrelated installed copy.
    def constant(archive: zipfile.ZipFile, module: str, name: str) -> int:
        tree = ast.parse(archive.read(f"anywhere_computer/{module}.py").decode("utf-8"))
        values = [ast.literal_eval(node.value) for node in tree.body
                  if isinstance(node, ast.Assign)
                  and any(isinstance(target, ast.Name) and target.id == name
                          for target in node.targets)]
        if len(values) != 1 or type(values[0]) is not int:
            raise ValueError(f"Wheel is missing its {name} compatibility declaration")
        return values[0]

    with zipfile.ZipFile(wheel) as archive:
        ledger_min = constant(archive, "state", "LEDGER_MIN_SUPPORTED_SCHEMA")
        ledger_max = constant(archive, "state", "LEDGER_SCHEMA_VERSION")
        api = constant(archive, "runtime_identity", "ENGINE_API_VERSION")
    release = {
        "format_version": 1, "version": version, "python_version": metadata["Version"],
        "source_commit": commit, "source_dirty": dirty,
        "validation": "unverified", "engine_api": {"minimum": api, "maximum": api},
        "ledger_schema": {"minimum": ledger_min, "maximum": ledger_max, "writes": ledger_max},
        "artifacts_sha256": hashes,
    }
    (bundled / "release.json").write_text(json.dumps(release, indent=2) + "\n", encoding="utf-8")
    output = root / "dist/anywhere-computer-plugin.zip"
    output.parent.mkdir(parents=True, exist_ok=True)
    members = [
        plugin / ".codex-plugin/plugin.json",
        plugin / ".mcp.json",
        plugin / "LICENSE",
        plugin / "skills/computer-work/SKILL.md",
        bundled / wheel.name,
        bundled / "dependencies.txt",
        bundled / "checksums.json",
        bundled / "release.json",
    ]
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in members:
            if path.is_symlink():
                raise ValueError("Plugin artifacts must not be symlinks")
            archive.write(path, path.relative_to(plugin))
    return output


if __name__ == "__main__":
    print(package_plugin(Path(__file__).resolve().parents[1]))
