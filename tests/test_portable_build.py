import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "portable_builder", Path(__file__).resolve().parents[1] / "scripts/build_portable.py",
)
assert spec is not None and spec.loader is not None
portable_builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(portable_builder)


def test_runtime_rejects_virtualenv_and_external_links(tmp_path):
    (tmp_path / "BUILD").write_text("fixture")
    (tmp_path / "pyvenv.cfg").write_text("home = /elsewhere")
    with pytest.raises(ValueError, match="virtualenv"):
        portable_builder.validate_runtime(tmp_path)
    (tmp_path / "pyvenv.cfg").unlink()
    (tmp_path / "outside").symlink_to(tmp_path.parent)
    with pytest.raises(ValueError, match="escapes"):
        portable_builder.validate_runtime(tmp_path)


def test_runtime_accepts_internal_links(tmp_path):
    (tmp_path / "BUILD").write_text("fixture")
    (tmp_path / "python").write_text("fixture")
    (tmp_path / "python3").symlink_to("python")
    portable_builder.validate_runtime(tmp_path)


def test_output_is_never_replaced(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "BUILD").write_text("fixture")
    archive = tmp_path / "existing.zip"
    archive.write_bytes(b"keep")
    with pytest.raises(ValueError, match="already exists"):
        portable_builder.build_portable(tmp_path, runtime, archive)
    assert archive.read_bytes() == b"keep"


@pytest.mark.parametrize("windows", [False, True])
def test_setup_launcher_uses_shared_setup_and_preserves_arguments(tmp_path, monkeypatch, windows):
    import io
    import runpy
    import sys

    from anywhere_computer import cli, state

    portable_builder.write_setup_launcher(tmp_path, windows=windows)
    captured = []
    monkeypatch.setattr(cli, "main", lambda: captured.append(list(sys.argv)))
    monkeypatch.setattr(state, "state_directory", lambda: tmp_path / "user state")
    monkeypatch.setattr(sys, "argv", ["setup_chatgpt.py", "--help"])
    monkeypatch.setattr(sys, "stdin", io.StringIO())
    runpy.run_path(str(tmp_path / "setup_chatgpt.py"), run_name="__main__")
    assert captured == [["anywhere", "chatgpt-setup", "--state-dir",
                         str(tmp_path / "user state/chatgpt"), "--help"]]
    suffix = "cmd" if windows else "command"
    assert (tmp_path / f"Setup ChatGPT.{suffix}").is_file()


def test_archive_publication_preserves_existing_file_and_survives_staging_cleanup(tmp_path):
    staged = tmp_path / "staged.zip"
    published = tmp_path / "published.zip"
    staged.write_bytes(b"complete archive")
    portable_builder.publish_archive(staged, published)
    staged.unlink()
    assert published.read_bytes() == b"complete archive"
    staged.write_bytes(b"replacement")
    with pytest.raises(FileExistsError):
        portable_builder.publish_archive(staged, published)
    assert published.read_bytes() == b"complete archive"


def test_generated_bytecode_is_removed_before_manifest(tmp_path):
    package = tmp_path / "runtime" / "site-packages" / "package"
    cache = package / "__pycache__"
    cache.mkdir(parents=True)
    source = package / "module.py"
    source.write_text("VALUE = 1\n")
    compiled = cache / "module.cpython-312.pyc"
    compiled.write_bytes(b"generated")
    portable_builder.discard_bytecode(tmp_path)
    assert source.read_text() == "VALUE = 1\n"
    assert not compiled.exists()


def test_failed_archive_publication_leaves_no_partial_output(tmp_path, monkeypatch):
    staged = tmp_path / "staged.zip"
    published = tmp_path / "published.zip"
    staged.write_bytes(b"complete archive")

    def fail_link(*args):
        raise OSError("fixture filesystem failure")

    monkeypatch.setattr(portable_builder.os, "link", fail_link)
    with pytest.raises(OSError, match="filesystem failure"):
        portable_builder.publish_archive(staged, published)
    assert not published.exists()
    assert staged.read_bytes() == b"complete archive"


def test_runtime_rejects_directory_link_cycle(tmp_path):
    (tmp_path / "BUILD").write_text("fixture")
    (tmp_path / "cycle").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match="directory symlinks"):
        portable_builder.validate_runtime(tmp_path)


@pytest.mark.parametrize("platform", ["darwin", "win32"])
def test_manager_packaging_keeps_native_executable_and_relative_runtime_layout(
    tmp_path, platform, monkeypatch,
):
    import os
    import plistlib

    chmod_calls = []
    real_chmod = Path.chmod

    def record_chmod(path, mode):
        chmod_calls.append((path, mode))
        real_chmod(path, mode)

    monkeypatch.setattr(Path, "chmod", record_chmod)
    native = tmp_path / "native"
    native.write_bytes(b"native fixture")
    app = tmp_path / "relocated 日本語"
    app.mkdir()
    portable_builder.include_manager(app, native, platform=platform,
                                     release_version="0.2.0-alpha.63")
    if platform == "darwin":
        contents = app / "Anywhere Computer Manager.app" / "Contents"
        metadata = plistlib.loads((contents / "Info.plist").read_bytes())
        executable = contents / "MacOS" / metadata["CFBundleExecutable"]
        assert (executable, 0o755) in chmod_calls
        if os.name != "nt":
            assert executable.stat().st_mode & 0o111
        assert metadata["CFBundlePackageType"] == "APPL"
        assert metadata["CFBundleIdentifier"] == "org.anywherecomputer.manager.preview"
        assert metadata["CFBundleShortVersionString"] == "0.2.0"
        assert metadata["CFBundleVersion"] == "0.2.63"
        assert metadata["AnywhereComputerReleaseVersion"] == "0.2.0-alpha.63"
    else:
        executable = app / "Anywhere Computer Manager.exe"
    assert executable.read_bytes() == native.read_bytes()
    assert not (app / "runtime").exists()  # Existing runtime packaging owns that directory.


def test_manager_packaging_requires_explicit_existing_file(tmp_path):
    with pytest.raises(ValueError, match="native executable"):
        portable_builder.include_manager(tmp_path, tmp_path / "missing", platform="win32",
                                         release_version="0.2.0-alpha.63")
    native = tmp_path / "native"
    native.write_bytes(b"native fixture")
    with pytest.raises(ValueError, match="supports"):
        portable_builder.include_manager(tmp_path, native, platform="linux",
                                         release_version="0.2.0-alpha.63")


def test_manager_bundle_versions_follow_release_order_and_reject_invalid_versions():
    releases = ["0.2.0-alpha.63", "0.2.0-alpha.64", "0.2.0-beta.1",
                "0.2.0-rc.1", "0.2.0", "0.2.1-alpha.1"]
    versions = [portable_builder.manager_bundle_versions(version) for version in releases]
    assert [short for short, _ in versions] == ["0.2.0"] * 5 + ["0.2.1"]
    assert [build for _, build in versions] == [
        "0.2.63", "0.2.64", "0.2.100001", "0.2.200001", "0.2.300000", "0.2.1000001",
    ]
    assert [tuple(map(int, build.split("."))) for _, build in versions] == sorted(
        tuple(map(int, build.split("."))) for _, build in versions
    )
    for invalid in ("0.2.0-alpha.100000", "0.2.0-preview.1", "0.2.0a63", "0.2"):
        with pytest.raises(ValueError, match="release|Release"):
            portable_builder.manager_bundle_versions(invalid)


@pytest.mark.parametrize("platform", ["win32", "linux"])
@pytest.mark.parametrize("kind", ["audio_helper", "gui_helper"])
def test_native_helper_rejects_unsupported_platform_before_build(
    tmp_path, monkeypatch, platform, kind,
):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "BUILD").write_text("fixture")
    monkeypatch.setattr(portable_builder.sys, "platform", platform)
    with pytest.raises(ValueError, match="macOS only"):
        portable_builder.build_portable(
            tmp_path, runtime, tmp_path / "output.zip", **{kind: tmp_path / "helper"},
        )
    assert not (tmp_path / "output.zip").exists()


@pytest.mark.parametrize("helper", [Path("relative"), Path("/missing-audio-helper")])
@pytest.mark.parametrize("kind", ["audio_helper", "gui_helper"])
def test_native_helper_requires_explicit_executable(tmp_path, monkeypatch, helper, kind):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "BUILD").write_text("fixture")
    monkeypatch.setattr(portable_builder.sys, "platform", "darwin")
    with pytest.raises(ValueError, match="absolute executable"):
        portable_builder.build_portable(
            tmp_path, runtime, tmp_path / "output.zip", **{kind: helper},
        )
    assert not (tmp_path / "output.zip").exists()


def renderer_fixture(root):
    for name in ("LibreOffice.app/Contents/MacOS/soffice",
                 "bin/pdfinfo", "bin/pdftoppm"):
        executable = root / name
        executable.parent.mkdir(parents=True, exist_ok=True)
        executable.write_text("#!/bin/sh\n")
        executable.chmod(0o755)
    licenses = root / "LICENSES"
    licenses.mkdir()
    (licenses / "LibreOffice.txt").write_text("LibreOffice license fixture")
    (licenses / "Poppler.txt").write_text("Poppler license fixture")
    (root / "SOURCES.json").write_text(
        '{"libreoffice":{"version":"1","binary_source":"https://example.test/lo",'
        '"source_code":"https://example.test/lo-source"},'
        '"poppler":{"version":"1","binary_source":"https://example.test/poppler",'
        '"source_code":"https://example.test/poppler-source"}}')
    return root


def test_renderer_bundle_requires_executables_licenses_and_sources(tmp_path):
    renderer = renderer_fixture(tmp_path / "renderer")
    portable_builder.validate_renderer_bundle(renderer)
    (renderer / "LICENSES/Poppler.txt").unlink()
    with pytest.raises(ValueError, match="LICENSES/Poppler.txt"):
        portable_builder.validate_renderer_bundle(renderer)


def test_renderer_bundle_rejects_escaping_links_and_unsupported_host(tmp_path, monkeypatch):
    renderer = renderer_fixture(tmp_path / "renderer")
    (renderer / "escape").symlink_to(tmp_path)
    with pytest.raises(ValueError, match="escapes"):
        portable_builder.validate_renderer_bundle(renderer)
    (renderer / "escape").unlink()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "BUILD").write_text("fixture")
    monkeypatch.setattr(portable_builder.sys, "platform", "win32")
    with pytest.raises(ValueError, match="macOS only"):
        portable_builder.build_portable(
            tmp_path, runtime, tmp_path / "output.zip", renderer_bundle=renderer,
        )
    assert not (tmp_path / "output.zip").exists()
