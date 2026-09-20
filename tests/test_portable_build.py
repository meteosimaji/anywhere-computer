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
    portable_builder.include_manager(app, native, platform=platform)
    if platform == "darwin":
        contents = app / "Anywhere Computer Manager.app" / "Contents"
        metadata = plistlib.loads((contents / "Info.plist").read_bytes())
        executable = contents / "MacOS" / metadata["CFBundleExecutable"]
        assert (executable, 0o755) in chmod_calls
        if os.name != "nt":
            assert executable.stat().st_mode & 0o111
        assert metadata["CFBundlePackageType"] == "APPL"
    else:
        executable = app / "Anywhere Computer Manager.exe"
    assert executable.read_bytes() == native.read_bytes()
    assert not (app / "runtime").exists()  # Existing runtime packaging owns that directory.


def test_manager_packaging_requires_explicit_existing_file(tmp_path):
    with pytest.raises(ValueError, match="native executable"):
        portable_builder.include_manager(tmp_path, tmp_path / "missing", platform="win32")
    native = tmp_path / "native"
    native.write_bytes(b"native fixture")
    with pytest.raises(ValueError, match="supports"):
        portable_builder.include_manager(tmp_path, native, platform="linux")


@pytest.mark.parametrize("platform", ["win32", "linux"])
def test_audio_helper_rejects_unsupported_platform_before_build(tmp_path, monkeypatch, platform):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "BUILD").write_text("fixture")
    monkeypatch.setattr(portable_builder.sys, "platform", platform)
    with pytest.raises(ValueError, match="macOS only"):
        portable_builder.build_portable(
            tmp_path, runtime, tmp_path / "output.zip", audio_helper=tmp_path / "helper",
        )
    assert not (tmp_path / "output.zip").exists()


@pytest.mark.parametrize("helper", [Path("relative"), Path("/missing-audio-helper")])
def test_audio_helper_requires_explicit_executable(tmp_path, monkeypatch, helper):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "BUILD").write_text("fixture")
    monkeypatch.setattr(portable_builder.sys, "platform", "darwin")
    with pytest.raises(ValueError, match="absolute executable"):
        portable_builder.build_portable(
            tmp_path, runtime, tmp_path / "output.zip", audio_helper=helper,
        )
    assert not (tmp_path / "output.zip").exists()
