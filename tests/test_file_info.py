import os
import stat

import pytest

from anywhere_computer.engine import Engine
from anywhere_computer.files import inspect_file
from anywhere_computer.models import Request


def test_metadata_reports_mode_and_distinguishes_creation_from_change(tmp_path):
    path = tmp_path / "unicode-日本語.txt"
    path.write_text("hello")
    metadata = path.stat()
    result = inspect_file(str(path))
    assert result["size"] == 5
    assert result["kind"] == "file"
    assert result["mode_octal"] == oct(stat.S_IMODE(metadata.st_mode))
    assert result["permissions"] == stat.filemode(metadata.st_mode)
    assert result["created"] == getattr(metadata, "st_birthtime", None)
    assert result["status_changed"] == (metadata.st_ctime if os.name != "nt" else None)
    assert result["permissions_scope"] == "mode_bits_not_effective_access_or_acl"
    assert inspect_file(str(tmp_path))["kind"] == "directory"


def test_symlink_entry_and_target_are_distinct_even_after_target_removed(tmp_path):
    target = tmp_path / "target"
    target.write_text("content")
    link = tmp_path / "link"
    try:
        link.symlink_to("target")
    except OSError as error:
        if os.name == "nt" and getattr(error, "winerror", None) == 1314:
            pytest.skip("Windows account lacks symlink privilege")
        raise
    result = inspect_file(str(link))
    assert result["entry"]["kind"] == "symlink"
    assert result["kind"] == "file"
    assert result["size"] == 7
    assert result["link_target"] == "target"
    assert result["target_state"] == "resolved"
    target.unlink()
    missing = inspect_file(str(link))
    assert missing["target_state"] == "missing"
    assert missing["size"] is None
    assert missing["entry"]["kind"] == "symlink"


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX FIFO required")
def test_info_does_not_open_fifo(tmp_path):
    pipe = tmp_path / "pipe"
    os.mkfifo(pipe)
    assert inspect_file(str(pipe))["kind"] == "fifo"


def test_missing_entry_and_relative_path_are_not_success(tmp_path):
    with pytest.raises(FileNotFoundError):
        inspect_file(str(tmp_path / "absent"))
    with pytest.raises(ValueError, match="absolute"):
        inspect_file("relative")


async def test_file_info_is_exposed_through_engine(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    engine = Engine(state)
    target = tmp_path / "file.txt"
    target.write_text("example")
    try:
        reply = await engine.execute(Request(
            operation_id="b" * 32, tool="files_info", arguments={"path": str(target)},
        ))
        assert reply.state == "completed"
        assert reply.data["kind"] == "file"
        assert reply.data["metadata_subject"] == "entry"
        assert reply.data["size"] == 7
    finally:
        await engine.close()
