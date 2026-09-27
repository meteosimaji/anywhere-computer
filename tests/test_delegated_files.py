"""A delegated path must remain confined after its policy precheck."""

import os

import pytest

from anywhere_computer.delegated_files import read, write
from anywhere_computer.models import ReadFile, WriteFile


@pytest.mark.skipif(os.name == "nt", reason="POSIX dir_fd confinement")
def test_confined_read_write_and_swapped_parent(tmp_path):
    root = tmp_path / "root"
    child = root / "child"
    outside = tmp_path / "outside"
    child.mkdir(parents=True)
    outside.mkdir()
    (child / "visible.txt").write_text("allowed", encoding="utf-8")
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    roots = (str(root),)
    assert read(ReadFile(path=str(child / "visible.txt")), roots)["text"] == "allowed"
    created = write(WriteFile(path=str(child / "new.txt"), mode="create", text="new"),
                    roots, tmp_path / "backups")
    assert (child / "new.txt").read_text() == "new"
    write(WriteFile(path=str(child / "new.txt"), mode="replace", text="updated",
                    expected_sha256=created["sha256"]), roots, tmp_path / "backups")
    assert (child / "new.txt").read_text() == "updated"

    # The policy may have checked root/child before this rename. The executor
    # must reject the symlink substituted between that check and open.
    child.rename(root / "original-child")
    child.symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        read(ReadFile(path=str(child / "secret.txt")), roots)
    with pytest.raises(OSError):
        write(WriteFile(path=str(child / "injected.txt"), mode="create", text="bad"),
              roots, tmp_path / "backups")
    assert not (outside / "injected.txt").exists()

    child.unlink()
    root.rename(tmp_path / "old-root")
    root.symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        read(ReadFile(path=str(root / "secret.txt")), roots)
