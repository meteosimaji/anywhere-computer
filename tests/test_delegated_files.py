"""A delegated path must remain confined after its policy precheck."""

import os

import pytest

from anywhere_computer.delegated_files import read, write
from anywhere_computer.models import ReadFile, WriteFile


@pytest.mark.skipif(os.name == "nt", reason="POSIX root directory")
@pytest.mark.parametrize("mixed_roots", [False, True])
def test_posix_root_grant_reads_and_creates_regular_files(tmp_path, mixed_roots):
    root = tmp_path.resolve()
    roots = ("/", str(root)) if mixed_roots else ("/",)
    source = root / "source.txt"
    source.write_text("root-authorized", encoding="utf-8")
    assert read(ReadFile(path=str(source)), roots)["text"] == "root-authorized"
    target = root / "created.txt"
    result = write(WriteFile(path=str(target), text="created", mode="create"),
                   roots, root / "backups")
    assert result["sha256"] and target.read_text() == "created"
    alias = root / "alias.txt"
    alias.symlink_to(source)
    with pytest.raises(OSError):
        read(ReadFile(path=str(alias)), roots)
    with pytest.raises((OSError, ValueError)):
        read(ReadFile(path="/"), roots)


@pytest.mark.skipif(os.name != "nt", reason="Windows-specific handle confinement")
def test_windows_delegated_read_and_create_only(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    source = root / "source.txt"
    source.write_text("private", encoding="utf-8")
    assert read(ReadFile(path=str(source)), (str(root),))["text"] == "private"
    created = write(WriteFile(path=str(root / "new.txt"), mode="create", text="new"),
                    (str(root),), tmp_path / "backups")
    assert created["sha256"] and (root / "new.txt").read_text() == "new"
    with pytest.raises(ValueError, match="unavailable on Windows"):
        write(WriteFile(path=str(source), mode="replace", text="bad",
                        expected_sha256=created["sha256"]),
              (str(root),), tmp_path / "backups")
    assert source.read_text() == "private"


@pytest.mark.skipif(os.name != "nt", reason="Windows-specific handle confinement")
def test_windows_delegated_reparse_and_outside_paths_have_no_write_effect(tmp_path):
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    with pytest.raises(ValueError, match="outside the permitted roots"):
        write(WriteFile(path=str(outside / "denied.txt"), text="bad"),
              (str(root),), tmp_path / "backups")
    assert not (outside / "denied.txt").exists()
    link = root / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"Windows symlink creation is unavailable: {error}")
    with pytest.raises(ValueError, match="reparse point"):
        read(ReadFile(path=str(link / "secret.txt")), (str(root),))
    with pytest.raises(ValueError, match="reparse point"):
        write(WriteFile(path=str(link / "denied.txt"), text="bad"),
              (str(root),), tmp_path / "backups")
    assert not (outside / "denied.txt").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows-specific handle confinement")
def test_windows_delegated_swapped_parent_is_rejected(tmp_path):
    root = tmp_path / "root"
    child = root / "child"
    outside = tmp_path / "outside"
    child.mkdir(parents=True)
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    try:
        child.rename(root / "old-child")
        child.symlink_to(outside, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"Windows symlink creation is unavailable: {error}")
    with pytest.raises(ValueError, match="reparse point"):
        read(ReadFile(path=str(child / "secret.txt")), (str(root),))
    with pytest.raises(ValueError, match="reparse point"):
        write(WriteFile(path=str(child / "denied.txt"), text="bad"),
              (str(root),), tmp_path / "backups")
    assert not (outside / "denied.txt").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows-specific path aliases")
@pytest.mark.parametrize("suffix", ["..\\outside.txt", "file.txt:stream", "file.txt.",
                                          "CON.txt", "sub\\\\file.txt"])
def test_windows_delegated_rejects_noncanonical_targets(tmp_path, suffix):
    root = tmp_path / "root"
    root.mkdir()
    with pytest.raises(ValueError, match="canonical drive path"):
        write(WriteFile(path=str(root) + "\\" + suffix, text="bad"),
              (str(root),), tmp_path / "backups")
    assert list(root.iterdir()) == []


@pytest.mark.skipif(os.name != "nt", reason="Windows-specific pinned parent")
def test_windows_delegated_parent_cannot_be_renamed_during_create(tmp_path, monkeypatch):
    from anywhere_computer.upload_win32 import PinnedRegular

    root = tmp_path / "root"
    root.mkdir()
    original_link = PinnedRegular.link

    def link_during_rename(source, target):
        with pytest.raises(OSError):
            root.rename(tmp_path / "moved")
        return original_link(source, target)

    monkeypatch.setattr(PinnedRegular, "link", link_during_rename)
    write(WriteFile(path=str(root / "new.txt"), text="new"),
          (str(root),), tmp_path / "backups")
    assert (root / "new.txt").read_text() == "new"


@pytest.mark.skipif(os.name != "nt", reason="Windows-specific handle publication")
def test_windows_hard_link_uses_pinned_file_after_staging_name_is_reused(tmp_path):
    from anywhere_computer.upload_win32 import pin_regular_handle_nofollow

    source = tmp_path / "source.txt"
    target = tmp_path / "target.txt"
    source.write_text("original", encoding="utf-8")
    with pin_regular_handle_nofollow(source) as pinned:
        source.unlink()
        source.write_text("attacker", encoding="utf-8")
        try:
            pinned.link(target)
        except OSError:
            assert not target.exists()
    if target.exists():
        assert target.read_text(encoding="utf-8") == "original"
    assert source.read_text(encoding="utf-8") == "attacker"


@pytest.mark.skipif(os.name != "nt", reason="Windows-specific handle publication")
def test_windows_hard_link_never_replaces_existing_target(tmp_path):
    from anywhere_computer.upload_win32 import pin_regular_handle_nofollow

    source = tmp_path / "source.txt"
    target = tmp_path / "target.txt"
    source.write_text("original", encoding="utf-8")
    target.write_text("preserve", encoding="utf-8")
    with pin_regular_handle_nofollow(source) as pinned:
        with pytest.raises(FileExistsError):
            pinned.link(target)
    assert target.read_text(encoding="utf-8") == "preserve"


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
