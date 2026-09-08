import os
from concurrent.futures import ThreadPoolExecutor

import pytest

from anywhere_computer.move_native import move_exclusive


def test_directory_move_retains_contents_and_refuses_existing_destination(tmp_path):
    source, destination = tmp_path / "original", tmp_path / "destination"
    source.mkdir()
    (source / "nested").mkdir()
    (source / "nested" / "data").write_bytes(b"unchanged")
    destination.mkdir()
    with pytest.raises(OSError):
        move_exclusive(source, destination)
    assert (source / "nested" / "data").read_bytes() == b"unchanged"
    destination.rmdir()
    move_exclusive(source, destination)
    assert not source.exists()
    assert (destination / "nested" / "data").read_bytes() == b"unchanged"


def test_concurrent_moves_have_one_winner_and_preserve_loser(tmp_path):
    sources = [tmp_path / "a", tmp_path / "b"]
    for path in sources:
        path.mkdir()
        (path / "identity").write_text(path.name)
    target = tmp_path / "target"

    def attempt(path):
        try:
            move_exclusive(path, target)
            return True
        except FileExistsError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(attempt, sources))
    assert sorted(outcomes) == [False, True]
    winner = outcomes.index(True)
    assert (target / "identity").read_text() == sources[winner].name
    assert (sources[1 - winner] / "identity").read_text() == sources[1 - winner].name


def test_move_link_moves_entry_without_modifying_target(tmp_path):
    target, link, moved = tmp_path / "target", tmp_path / "link", tmp_path / "moved"
    target.write_text("preserved")
    try:
        link.symlink_to("target")
    except OSError as error:
        if os.name == "nt" and getattr(error, "winerror", None) == 1314:
            pytest.skip("Windows symlink privilege unavailable")
        raise
    move_exclusive(link, moved)
    assert moved.is_symlink() and os.readlink(moved) == "target"
    assert not link.is_symlink() and target.read_text() == "preserved"
