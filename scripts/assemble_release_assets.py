"""Name the five verified CI archives for an immutable GitHub beta release."""

import argparse
import ast
import hashlib
import json
import re
import shutil
import zipfile
from pathlib import Path

from package_plugin import plugin_version

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = (
    ("portable-ubuntu-latest", "portable-ubuntu-latest.zip", False),
    ("portable-windows-latest", "portable-windows-latest.zip", False),
    ("portable-macos-latest", "portable-macos-latest.zip", False),
    ("managed-portable-windows-latest", "managed-portable.zip", True),
    ("managed-portable-macos-latest", "managed-portable.zip", True),
)


def release_version(root: Path) -> tuple[str, dict[str, object]]:
    tree = ast.parse((root / "src/anywhere_computer/__init__.py").read_text(encoding="utf-8"))
    versions = [ast.literal_eval(node.value) for node in tree.body
                if isinstance(node, ast.Assign)
                and any(isinstance(target, ast.Name) and target.id == "__version__"
                        for target in node.targets)]
    if len(versions) != 1 or not isinstance(versions[0], str):
        raise ValueError("Expected one literal source version")
    version = versions[0]
    release = json.loads((root / "plugins/anywhere-computer/bundled/release.json")
                         .read_text(encoding="utf-8"))
    if (release.get("python_version") != version
            or release.get("version") != plugin_version(version)
            or release.get("source_dirty") is not False
            or not isinstance(release.get("source_commit"), str)
            or not re.fullmatch(r"[a-f0-9]{40}", release["source_commit"])):
        raise ValueError("Bundled Plugin is not a clean build of the source version")
    return version, release


def release_platform(platform: str) -> str:
    if re.fullmatch(r"macosx-\d+(?:\.\d+)*-arm64", platform):
        return "macos-arm64"
    if re.fullmatch(r"macosx-\d+(?:\.\d+)*-x86_64", platform):
        return "macos-x86_64"
    platforms = {"linux-x86_64": "linux-x86_64", "win-amd64": "windows-x86_64"}
    if platform not in platforms:
        raise ValueError(f"Unsupported release platform: {platform}")
    return platforms[platform]


def assemble(root: Path, incoming: Path, output: Path) -> str:
    version, release = release_version(root)
    if output.exists() and any(output.iterdir()):
        raise ValueError("Release output directory must be empty")
    selected: list[tuple[Path, str]] = []
    names: set[str] = set()
    for artifact, source_name, managed in ARTIFACTS:
        source = incoming / artifact / source_name
        with zipfile.ZipFile(source) as archive:
            member = archive.getinfo("Anywhere Computer/manifest.json")
            if member.file_size > 4 * 1024 * 1024:
                raise ValueError(f"Oversized portable manifest: {artifact}")
            manifest = json.loads(archive.read(member))
        if manifest.get("release") != release or not isinstance(manifest.get("files"), dict):
            raise ValueError(f"Portable release metadata differs from the Plugin: {artifact}")
        platform = release_platform(manifest["platform"])
        os_name = artifact.split("-")[-2]
        if (os_name == "ubuntu" and platform != "linux-x86_64"
                or os_name == "windows" and platform != "windows-x86_64"
                or os_name == "macos" and not platform.startswith("macos-")):
            raise ValueError(f"Portable platform differs from its CI runner: {artifact}")
        manager = ("Anywhere Computer Manager.exe" if os_name == "windows" else
                   "Anywhere Computer Manager.app/Contents/MacOS/anywhere-computer-manager")
        if managed and manager not in manifest["files"]:
            raise ValueError(f"Managed portable lacks its native manager: {artifact}")
        edition = "managed-" if managed else ""
        name = f"anywhere-computer-{version}-{edition}{platform}.zip"
        if name in names:
            raise ValueError(f"Duplicate release platform: {platform}")
        names.add(name)
        selected.append((source, name))
    output.mkdir(parents=True, exist_ok=True)
    checksums = []
    for source, name in selected:
        target = output / name
        shutil.copyfile(source, target)
        with target.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        checksums.append(f"{digest}  {name}")
    (output / "SHA256SUMS").write_text("\n".join(checksums) + "\n", encoding="utf-8")
    return "v" + version


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="store_true", help="Print the clean source tag only")
    parser.add_argument("--incoming", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.version:
        print("v" + release_version(ROOT)[0])
    elif args.incoming is not None and args.output is not None:
        print(assemble(ROOT, args.incoming, args.output))
    else:
        parser.error("Provide --version or both --incoming and --output")


if __name__ == "__main__":
    main()
