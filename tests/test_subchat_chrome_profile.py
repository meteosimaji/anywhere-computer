"""Selected Chrome profile snapshots stay private and leave the source untouched."""

import sqlite3
import sys
from pathlib import Path

import pytest

from anywhere_computer.subchat import SubchatAccessError
from anywhere_computer.subchat_chrome_profile import (
    _snapshot_profile,
    temporary_chrome_profile,
)


def profile_fixture(root: Path, name: str = "Profile 2") -> Path:
    profile = root / name
    profile.mkdir(parents=True)
    (root / "Local State").write_text('{"profile":{}}')
    (profile / "Preferences").write_text('{"session":{}}')
    (profile / "Secure Preferences").write_text('{}')
    with sqlite3.connect(profile / "Cookies") as database:
        database.execute("CREATE TABLE cookies (host_key TEXT, name TEXT, value TEXT)")
        database.executemany("INSERT INTO cookies VALUES (?, ?, ?)", [
            (".chatgpt.com", "session", "private-chat"),
            ("chatgpt.com", "other", "private-chat-two"),
            ("other.example", "unrelated", "must-not-copy"),
        ])
    return profile


def test_snapshot_selects_exact_profile_and_filters_other_hosts(tmp_path):
    source = profile_fixture(tmp_path / "source")
    clone = tmp_path / "clone"
    clone.mkdir()
    _snapshot_profile(source, clone)
    assert (clone / "Profile 2/Cookies").exists()
    assert not (clone / "Default").exists()
    with sqlite3.connect(clone / "Profile 2/Cookies") as database:
        assert database.execute("SELECT host_key FROM cookies ORDER BY host_key").fetchall() == [
            (".chatgpt.com",), ("chatgpt.com",),
        ]
    with sqlite3.connect(source / "Cookies") as database:
        assert database.execute("SELECT COUNT(*) FROM cookies").fetchone()[0] == 3
    assert "must-not-copy" not in (clone / "Profile 2/Cookies").read_bytes().decode(
        "latin1")


def test_snapshot_requires_selected_login_and_valid_database(tmp_path):
    source = profile_fixture(tmp_path / "source", "Default")
    with sqlite3.connect(source / "Cookies") as database:
        database.execute("DELETE FROM cookies WHERE host_key != 'other.example'")
    clone = tmp_path / "clone"
    clone.mkdir()
    with pytest.raises(SubchatAccessError) as error:
        _snapshot_profile(source, clone)
    assert error.value.status == 401
    (source / "Cookies").unlink()
    with pytest.raises(ValueError, match="Cookie database"):
        _snapshot_profile(source, tmp_path / "another")


def test_snapshot_includes_committed_wal_cookie_without_touching_live_database(tmp_path):
    source = profile_fixture(tmp_path / "source", "Default")
    with sqlite3.connect(source / "Cookies") as live:
        live.execute("PRAGMA journal_mode=WAL")
        live.execute("INSERT INTO cookies VALUES (?, ?, ?)",
                     (".chatgpt.com", "recent", "wal-only"))
        live.commit()
        clone = tmp_path / "clone"
        clone.mkdir()
        _snapshot_profile(source, clone)
        with sqlite3.connect(tmp_path / "clone/Default/Cookies") as snapshot:
            assert snapshot.execute(
                "SELECT COUNT(*) FROM cookies WHERE name = 'recent'").fetchone()[0] == 1
        assert live.execute(
            "SELECT COUNT(*) FROM cookies WHERE name = 'recent'").fetchone()[0] == 1


@pytest.mark.skipif(sys.platform != "darwin", reason="Live profile snapshot is macOS only")
async def test_temporary_snapshot_is_removed_on_success_and_failure(tmp_path, monkeypatch):
    import anywhere_computer.subchat_chrome_profile as module

    source = profile_fixture(tmp_path / "source", "Default")
    monkeypatch.setattr(module.tempfile, "tempdir", str(tmp_path))
    async with temporary_chrome_profile(source) as clone:
        assert clone.exists()
        assert clone.stat().st_mode & 0o777 == 0o700
        assert (clone / "Default/Cookies").stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob("anywhere-subchat-chrome-*"))
    with pytest.raises(RuntimeError, match="simulated launch failure"):
        async with temporary_chrome_profile(source):
            raise RuntimeError("simulated launch failure")
    assert not list(tmp_path.glob("anywhere-subchat-chrome-*"))
