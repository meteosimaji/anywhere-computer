import hashlib
import json
import re
import zipfile
from email.parser import Parser
from pathlib import Path

from anywhere_computer import __version__
from anywhere_computer.runtime_identity import ENGINE_API_VERSION
from anywhere_computer.state import LEDGER_MIN_SUPPORTED_SCHEMA, LEDGER_SCHEMA_VERSION

ROOT = Path(__file__).resolve().parents[1]


def test_packaged_runtime_matches_current_source_and_checksums():
    plugin = ROOT / "plugins/anywhere-computer"
    manifest = json.loads((plugin / ".codex-plugin/plugin.json").read_text(encoding="utf-8"))
    assert manifest["name"] == plugin.name
    config = json.loads((plugin / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"][
        "anywhere-computer"
    ]
    assert config["command"] == "uv" and config["cwd"] == "."
    checksums = json.loads((plugin / "bundled/checksums.json").read_text(encoding="utf-8"))
    release = json.loads((plugin / "bundled/release.json").read_text(encoding="utf-8"))
    assert release["artifacts_sha256"] == checksums
    assert release["python_version"] == __version__
    assert release["version"] == manifest["version"]
    assert re.fullmatch(r"[a-f0-9]{40}", release["source_commit"])
    assert type(release["source_dirty"]) is bool
    assert release["validation"] == "unverified"
    assert release["engine_api"] == {"minimum": ENGINE_API_VERSION, "maximum": ENGINE_API_VERSION}
    assert release["ledger_schema"] == {
        "minimum": LEDGER_MIN_SUPPORTED_SCHEMA, "maximum": LEDGER_SCHEMA_VERSION,
        "writes": LEDGER_SCHEMA_VERSION,
    }
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
        for asset in (ROOT / "src/anywhere_computer/web").glob("*"):
            assert archive.read("anywhere_computer/web/" + asset.name) == asset.read_bytes()
        metadata_files = [name for name in archive.namelist()
                          if name.endswith(".dist-info/METADATA")]
        assert len(metadata_files) == 1
        metadata = archive.read(metadata_files[0]).decode()
        assert Parser().parsestr(metadata)["Version"] == __version__
        expected_plugin_version = re.sub(r"a(\d+)$", r"-alpha.\1", __version__)
        expected_plugin_version = re.sub(r"b(\d+)$", r"-beta.\1", expected_plugin_version)
        expected_plugin_version = re.sub(r"rc(\d+)$", r"-rc.\1", expected_plugin_version)
        assert manifest["version"] == expected_plugin_version
        # Direct MCP is optional; the base plugin must still start without that SDK.
        mcp_requirements = [line for line in metadata.splitlines()
                            if line.startswith("Requires-Dist: mcp")]
        assert mcp_requirements == ["Requires-Dist: mcp==1.30.0; extra == 'mcp'"]
        assert "Requires-Dist: filelock" not in metadata
        assert "Requires-Dist: platformdirs" not in metadata
