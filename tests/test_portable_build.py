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
