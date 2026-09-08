"""Build a source-only guest test bundle from an explicit allowlist (stdlib only)."""

import hashlib
import json
import subprocess
import zipfile
from pathlib import Path


def build_guest_bundle(root: Path, output: Path) -> Path:
    files = [root / name for name in ("pyproject.toml", "uv.lock", "LICENSE", "README.md")]
    files += sorted((root / "src/anywhere_computer").glob("*.py"))
    files += sorted((root / "tests").glob("test_*.py"))
    files += [root / "docs/ARCHITECTURE.md"]
    payloads = {}
    for source in files:
        if source.is_symlink() or not source.is_file():
            raise ValueError(f"Invalid bundle source: {source.name}")
        payloads[source.relative_to(root).as_posix()] = source.read_bytes()
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    manifest = {
        "source_revision": revision,
        "files_sha256": {name: hashlib.sha256(data).hexdigest() for name, data in payloads.items()},
        "scope": "Source test bundle; hashes identify actual files, including uncommitted edits",
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in payloads.items():
            archive.writestr(name, data)
        archive.writestr("bundle-manifest.json", json.dumps(manifest, indent=2))
    return output


if __name__ == "__main__":
    repository = Path(__file__).resolve().parents[1]
    artifact = build_guest_bundle(repository, repository / "dist/anywhere-guest-tests.zip")
    print(
        json.dumps(
            {"artifact": str(artifact), "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest()}
        )
    )
