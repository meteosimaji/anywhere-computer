import hashlib
import json
import re
import zipfile
from email.parser import Parser
from pathlib import Path

import pytest

from anywhere_computer import __version__
from anywhere_computer.runtime_identity import ENGINE_API_VERSION
from anywhere_computer.state import LEDGER_MIN_SUPPORTED_SCHEMA, LEDGER_SCHEMA_VERSION

ROOT = Path(__file__).resolve().parents[1]


def test_current_plugin_bundle_is_coherent_even_for_development_builds():
    """A dirty candidate still needs the exact code and every configured server."""
    plugin = ROOT / "plugins/anywhere-computer"
    release = json.loads((plugin / "bundled/release.json").read_text(encoding="utf-8"))
    checksums = json.loads((plugin / "bundled/checksums.json").read_text(encoding="utf-8"))
    assert release["artifacts_sha256"] == checksums
    for name, digest in checksums.items():
        assert hashlib.sha256((plugin / "bundled" / name).read_bytes()).hexdigest() == digest
    wheel_name = f"anywhere_computer-{__version__}-py3-none-any.whl"
    assert wheel_name in checksums
    with zipfile.ZipFile(plugin / "bundled" / wheel_name) as archive:
        sources = sorted((ROOT / "src/anywhere_computer").rglob("*.py"))
        assert {name for name in archive.namelist() if name.endswith(".py")} == {
            source.relative_to(ROOT / "src").as_posix() for source in sources}
        for source in sources:
            assert archive.read(source.relative_to(ROOT / "src").as_posix()) == source.read_bytes()
        entrypoint = next(name for name in archive.namelist()
                          if name.endswith(".dist-info/entry_points.txt"))
        installed_commands = archive.read(entrypoint).decode("utf-8")
    expected = {
        "anywhere-computer": "anywhere mcp",
        "anywhere-subchat": "anywhere-subchat-plugin",
        "anywhere-subchat-library": "anywhere-subchat-library-mcp",
    }
    for config_name in (".mcp.json", ".claude-mcp.json"):
        servers = json.loads((plugin / config_name).read_text(encoding="utf-8"))["mcpServers"]
        assert set(servers) == set(expected)
        for server_name, command in expected.items():
            args = servers[server_name]["args"]
            assert args[args.index("--from") + 1].endswith("/bundled/" + wheel_name)
            assert args[-len(command.split()):] == command.split()
            assert command.split()[0] in installed_commands


def test_packaged_runtime_matches_current_source_and_checksums():
    plugin = ROOT / "plugins/anywhere-computer"
    manifest = json.loads((plugin / ".codex-plugin/plugin.json").read_text(encoding="utf-8"))
    assert manifest["name"] == plugin.name
    servers = json.loads((plugin / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]
    assert set(servers) == {"anywhere-computer", "anywhere-subchat",
                            "anywhere-subchat-library"}
    config = servers["anywhere-computer"]
    assert config["command"] == "uv" and config["cwd"] == "."
    subchat = servers["anywhere-subchat"]
    assert subchat["command"] == "uv" and subchat["cwd"] == "."
    assert subchat["args"][:-1] == config["args"][:-2]
    assert subchat["args"][-1] == "anywhere-subchat-plugin"
    library = servers["anywhere-subchat-library"]
    assert library["command"] == "uv" and library["cwd"] == "."
    assert library["args"][:-1] == config["args"][:-2]
    assert library["args"][-1] == "anywhere-subchat-library-mcp"
    assert not any("/Users/" in arg or "\\\\Users\\\\" in arg for arg in subchat["args"])
    claude_manifest = json.loads(
        (plugin / ".claude-plugin/plugin.json").read_text(encoding="utf-8")
    )
    assert claude_manifest["name"] == manifest["name"]
    assert claude_manifest["version"] == manifest["version"]
    assert claude_manifest["mcpServers"] == "./.claude-mcp.json"
    claude_servers = json.loads(
        (plugin / ".claude-mcp.json").read_text(encoding="utf-8")
    )["mcpServers"]
    assert set(claude_servers) == set(servers)
    for server in claude_servers.values():
        assert server["command"] == "uv"
        assert "cwd" not in server
        claude_wheel = server["args"][server["args"].index("--from") + 1]
        assert claude_wheel == (
            "${CLAUDE_PLUGIN_ROOT}/bundled/anywhere_computer-"
            + __version__ + "-py3-none-any.whl"
        )
        requirements = server["args"][server["args"].index("--with-requirements") + 1]
        assert requirements == "${CLAUDE_PLUGIN_ROOT}/bundled/dependencies.txt"
    marketplace = json.loads(
        (ROOT / ".claude-plugin/marketplace.json").read_text(encoding="utf-8")
    )
    assert marketplace["plugins"][0]["source"] == "./plugins/anywhere-computer"
    checksums = json.loads((plugin / "bundled/checksums.json").read_text(encoding="utf-8"))
    dependencies = (plugin / "bundled/dependencies.txt").read_text(encoding="utf-8")
    assert re.search(r'^mcp==1\.30\.0\s', dependencies, re.MULTILINE)
    assert re.search(r'^playwright==1\.58\.0\s', dependencies, re.MULTILINE)
    assert re.search(r'^httpx==0\.28\.1\s', dependencies, re.MULTILINE)
    release = json.loads((plugin / "bundled/release.json").read_text(encoding="utf-8"))
    assert release["artifacts_sha256"] == checksums
    assert release["python_version"] == __version__
    assert release["version"] == manifest["version"]
    assert re.fullmatch(r"[a-f0-9]{40}", release["source_commit"])
    assert release["source_dirty"] is False
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
        sources = sorted((ROOT / "src/anywhere_computer").rglob("*.py"))
        assert {name for name in archive.namelist() if name.endswith(".py")} == {
            source.relative_to(ROOT / "src").as_posix() for source in sources
        }
        for source in sources:
            member = source.relative_to(ROOT / "src").as_posix()
            assert archive.read(member) == source.read_bytes(), (
                "Rebuild plugin after source changes: uv run python scripts/package_plugin.py"
            )
        for asset in (ROOT / "src/anywhere_computer/web").glob("*"):
            assert archive.read("anywhere_computer/web/" + asset.name) == asset.read_bytes()
        for asset in (ROOT / "src/anywhere_computer/subchat_browser").glob("*.js"):
            assert archive.read(asset.relative_to(ROOT / "src").as_posix()) == asset.read_bytes()
        entrypoints = [name for name in archive.namelist()
                       if name.endswith(".dist-info/entry_points.txt")]
        assert len(entrypoints) == 1
        assert ("anywhere-subchat = anywhere_computer.subchat_cli:main"
                in archive.read(entrypoints[0]).decode())
        assert ("anywhere-subchat-plugin = anywhere_computer.subchat_plugin:main"
                in archive.read(entrypoints[0]).decode())
        assert ("anywhere-subchat-library-mcp = "
                "anywhere_computer.subchat_library_mcp:main"
                in archive.read(entrypoints[0]).decode())
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
        assert "Requires-Dist: playwright==1.58.0; extra == 'mcp'" in metadata
        assert "Requires-Dist: httpx==0.28.1" in metadata
        assert "Requires-Dist: filelock" not in metadata
        assert "Requires-Dist: platformdirs" not in metadata


def test_plugin_packager_refuses_dirty_source_before_writing(tmp_path, monkeypatch):
    import importlib.util

    module_spec = importlib.util.spec_from_file_location(
        "package_plugin", ROOT / "scripts/package_plugin.py"
    )
    assert module_spec is not None and module_spec.loader is not None
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)

    def git_output(command, **_kwargs):
        return "a" * 40 if command[1] == "rev-parse" else " M src/changed.py\n"

    monkeypatch.setattr(module.subprocess, "check_output", git_output)
    with pytest.raises(ValueError, match="dirty source tree"):
        module.package_plugin(tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_plugin_packager_requires_new_version_for_changed_wheel(tmp_path):
    import importlib.util

    module_spec = importlib.util.spec_from_file_location(
        "package_plugin", ROOT / "scripts/package_plugin.py"
    )
    assert module_spec is not None and module_spec.loader is not None
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)

    bundled = tmp_path / "bundled.whl"
    built = tmp_path / "built.whl"
    bundled.write_bytes(b"original")
    built.write_bytes(b"original")
    module.require_immutable_wheel(bundled, built, allow_dirty=False)

    built.write_bytes(b"changed")
    with pytest.raises(ValueError, match="bump the version"):
        module.require_immutable_wheel(bundled, built, allow_dirty=False)
    module.require_immutable_wheel(bundled, built, allow_dirty=True)


def test_dirty_plugin_runtime_avoids_cached_wheel():
    import importlib.util

    module_spec = importlib.util.spec_from_file_location(
        "package_plugin", ROOT / "scripts/package_plugin.py"
    )
    assert module_spec is not None and module_spec.loader is not None
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)

    arguments = ["tool", "run", "--python", "3.12", "--from", "old.whl", "anywhere"]
    module.configure_runtime_args(arguments, "new.whl", dirty=True)
    assert arguments == ["tool", "run", "--python", "3.12", "--no-cache",
                         "--from", "new.whl", "anywhere"]
    module.configure_runtime_args(arguments, "next.whl", dirty=True)
    assert arguments.count("--no-cache") == 1
    module.configure_runtime_args(arguments, "release.whl", dirty=False)
    assert arguments == ["tool", "run", "--python", "3.12", "--from", "release.whl",
                         "anywhere"]


def test_bundled_runtime_cache_policy_matches_release_state():
    plugin = ROOT / "plugins/anywhere-computer"
    release = json.loads((plugin / "bundled/release.json").read_text(encoding="utf-8"))
    for config_name in (".mcp.json", ".claude-mcp.json"):
        servers = json.loads((plugin / config_name).read_text(encoding="utf-8"))["mcpServers"]
        for server in servers.values():
            args = server["args"]
            assert args.count("--no-cache") == int(release["source_dirty"])
