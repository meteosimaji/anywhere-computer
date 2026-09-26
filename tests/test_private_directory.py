import os
import subprocess

import pytest

from anywhere_computer import private_directory
from anywhere_computer.state import Ledger


def test_private_directory_descriptor_names_only_creator_and_privileged_system_sids():
    sid = "S-1-5-21-123-456-789-1001"
    assert private_directory._directory_sddl(sid) == (
        f"O:{sid}D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;FA;;;{sid})"
    )


def test_existing_acl_must_match_protected_inheritable_user_policy():
    user = "S-1-5-21-123-456-789-1001"
    entries = [(0, 3, 0x1F01FF, sid) for sid in
               ("S-1-5-18", "S-1-5-32-544", user)]
    validate = private_directory._validate_private_acl
    assert validate(user, True, entries, user)
    assert not validate("S-1-5-32-544", True, entries, user)
    assert not validate(user, False, entries, user)
    assert not validate(user, True, entries + [(0, 3, 0x1F01FF, "S-1-1-0")], user)
    assert not validate(user, True, entries[:2] + [(0, 0, 0x1F01FF, user)], user)
    assert not validate(user, True, entries[:2] + [(0, 3, 0x120089, user)], user)
    legacy = entries[:2] + [(0, 3, 0x1F01FF, "S-1-3-4")]
    assert not validate(user, True, legacy, user)
    assert not validate("S-1-5-32-544", True, legacy, user)
    assert not validate(user, False, legacy, user)
    assert not validate(user, True, legacy + [(0, 3, 0x1F01FF, "S-1-1-0")], user)
    assert not validate(user, True, entries[:2] + [(0, 0x08, 0x1F01FF, "S-1-3-4")], user)


def test_existing_directory_is_not_recreated_on_posix(tmp_path):
    directory = tmp_path / "ledger"
    directory.mkdir()
    marker = directory / "keep"
    marker.write_text("preserved")
    private_directory.create_private_directory(directory)
    assert marker.read_text() == "preserved"


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL integration")
def test_windows_pytest_tmp_path_is_private_before_state_uses_it(tmp_path):
    user_sid = private_directory._windows_current_user_sid()
    private_directory._validate_existing_windows_directory(tmp_path, user_sid)
    ledger = Ledger(tmp_path)
    ledger.close()


@pytest.mark.skipif(os.name == "nt", reason="Creating symlinks requires a Windows privilege")
def test_acl_migration_preflight_rejects_links_before_changes(tmp_path):
    root = tmp_path / "state"
    root.mkdir()
    (root / "keep.txt").write_text("keep")
    assert set(private_directory._migration_entries(root)) == {root, root / "keep.txt"}
    (root / "linked").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match="reparse point"):
        private_directory._migration_entries(root)
    assert (root / "keep.txt").read_text() == "keep"


def test_acl_migration_preflight_rejects_hardlinks(tmp_path):
    root = tmp_path / "state"
    root.mkdir()
    original = root / "shared.txt"
    original.write_text("keep")
    os.link(original, tmp_path / "outside.txt")
    with pytest.raises(ValueError, match="hard-linked file"):
        private_directory._migration_entries(root)


@pytest.mark.skipif(os.name == "nt", reason="Creating symlinks requires a Windows privilege")
def test_installed_migration_preflights_both_roots_before_any_apply(tmp_path, monkeypatch):
    engine = tmp_path / "engine"
    subchat = tmp_path / "subchat"
    engine.mkdir()
    subchat.mkdir()
    (engine / "keep.txt").write_text("keep")
    (subchat / "redirect").symlink_to(engine, target_is_directory=True)
    monkeypatch.setattr(private_directory, "_windows_migration_targets",
                        lambda: [engine, subchat])
    applied = []

    def inspect(*, root, apply=False):
        if apply:
            applied.append(root)
        return len(private_directory._migration_entries(root))

    monkeypatch.setattr(private_directory, "migrate_default_windows_state", inspect)
    with pytest.raises(ValueError, match="reparse point"):
        private_directory.migrate_installed_windows_state(apply=True)
    assert applied == []
    assert (engine / "keep.txt").read_text() == "keep"


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL integration")
def test_windows_private_directory_reopens_ledger_without_changing_existing_acl(tmp_path):
    directory = tmp_path / "new-parent" / "ledger"
    private_directory.create_private_directory(directory)
    private_directory.create_private_directory(directory.parent)
    first = Ledger(directory)
    try:
        first.connection.execute("CREATE TABLE private_acl_probe (value TEXT)")
        first.connection.execute("INSERT INTO private_acl_probe VALUES ('preserved')")
        first.connection.commit()
    finally:
        first.close()
    before = subprocess.check_output(["icacls", str(directory)])
    second = Ledger(directory)
    try:
        assert second.connection.execute("SELECT value FROM private_acl_probe").fetchone() == (
            "preserved",
        )
    finally:
        second.close()
    assert subprocess.check_output(["icacls", str(directory)]) == before


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL integration")
def test_windows_existing_permissive_directory_is_rejected_without_rewriting_acl(tmp_path):
    directory = tmp_path / "broad"
    directory.mkdir()
    subprocess.run(["icacls", str(directory), "/grant", "*S-1-1-0:(OI)(CI)F"], check=True,
                   capture_output=True)
    before = subprocess.check_output(["icacls", str(directory)])
    with pytest.raises(PermissionError, match="explicit ACL repair"):
        private_directory.create_private_directory(directory)
    assert subprocess.check_output(["icacls", str(directory)]) == before


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL integration")
def test_windows_legacy_owner_rights_directory_requires_explicit_migration(tmp_path):
    directory = tmp_path / "legacy"
    directory.mkdir(mode=0o700)
    marker = directory / "keep.txt"
    marker.write_text("keep", encoding="utf-8")
    before = subprocess.check_output(["icacls", str(directory)])
    with pytest.raises(PermissionError, match="explicit ACL repair"):
        private_directory.create_private_directory(directory)
    assert subprocess.check_output(["icacls", str(directory)]) == before
    assert marker.read_text(encoding="utf-8") == "keep"


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL integration")
def test_windows_default_state_migration_dry_run_keeps_existing_acl(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    root = tmp_path / "Anywhere Computer" / "Anywhere Computer"
    root.mkdir(parents=True)
    marker = root / "keep.txt"
    marker.write_text("keep", encoding="utf-8")
    before = subprocess.check_output(["icacls", str(root)])
    assert private_directory.migrate_default_windows_state() == 2
    assert subprocess.check_output(["icacls", str(root)]) == before
    assert marker.read_text(encoding="utf-8") == "keep"


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL integration")
def test_windows_state_migration_preserves_data_and_is_idempotent(tmp_path):
    root = tmp_path / "test-state"
    legacy = root / "subchat" / "chrome-login"
    legacy.mkdir(parents=True)
    marker = legacy / "keep.txt"
    marker.write_text("keep", encoding="utf-8")
    count = private_directory.migrate_default_windows_state(apply=True, root=root)
    assert count == 4
    private_directory.create_private_directory(root)
    private_directory.create_private_directory(legacy)
    before = subprocess.check_output(["icacls", str(legacy)])
    assert private_directory.migrate_default_windows_state(apply=True, root=root) == count
    assert subprocess.check_output(["icacls", str(legacy)]) == before
    assert marker.read_text(encoding="utf-8") == "keep"
