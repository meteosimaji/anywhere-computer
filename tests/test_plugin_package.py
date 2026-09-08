import hashlib
import json
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_packaged_runtime_matches_current_source_and_checksums():
    plugin = ROOT / "plugins/anywhere-computer"
    manifest = json.loads((plugin / ".codex-plugin/plugin.json").read_text())
    assert manifest["name"] == plugin.name
    config = json.loads((plugin / ".mcp.json").read_text())["mcpServers"]["anywhere-computer"]
    assert config["command"] == "uv" and config["cwd"] == "."
    checksums = json.loads((plugin / "bundled/checksums.json").read_text())
    for name, digest in checksums.items():
        assert hashlib.sha256((plugin / "bundled" / name).read_bytes()).hexdigest() == digest
    wheel = plugin / config["args"][config["args"].index("--from") + 1]
    with zipfile.ZipFile(wheel) as archive:
        sources = sorted((ROOT / "src/anywhere_computer").glob("*.py"))
        assert {name for name in archive.namelist() if name.endswith(".py")} == {
            "anywhere_computer/" + source.name for source in sources
        }
        for source in sources:
            assert archive.read("anywhere_computer/" + source.name) == source.read_bytes(), (
                "Rebuild plugin after source changes: uv run python scripts/package_plugin.py"
            )
        metadata = archive.read("anywhere_computer-0.1.0a1.dist-info/METADATA").decode()
        assert "Requires-Dist: mcp" not in metadata
        assert "Requires-Dist: filelock" not in metadata
        assert "Requires-Dist: platformdirs" not in metadata
