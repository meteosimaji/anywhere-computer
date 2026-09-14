import hashlib
import os
import uuid

import pytest

from anywhere_computer import common_skills
from anywhere_computer.common_skills import SkillResource, SkillsPage, list_skills, read_skill
from anywhere_computer.engine import Engine
from anywhere_computer.models import Reply, Request
from anywhere_computer.remote_bridge import RemoteAgent


def make_skill(root, name="example"):
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        "---\nname: example\ndescription: Test\n---\nRead references/日本語.md.\n",
        encoding="utf-8",
    )
    (directory / "references").mkdir()
    (directory / "references/日本語.md").write_text("値: 42 ✅\n", encoding="utf-8")
    return directory


def selected(root):
    return list_skills(SkillsPage(roots=[str(root)]))["skills"][0]


def resource(root, row, relative_path="SKILL.md"):
    return SkillResource(roots=[str(root)], skill_id=row["skill_id"],
                         expected_skill_sha256=row["skill_sha256"], relative_path=relative_path)


def test_common_locations_and_explicit_roots_do_not_scan_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(common_skills.Path, "home", lambda: home)
    shared = make_skill(home / ".agents/skills", "shared")
    workspace = tmp_path / "workspace"
    make_skill(workspace / ".agents/skills", "project")
    make_skill(home / "unrelated-private-tree", "not-discovered")
    listed = list_skills(SkillsPage(cwd=str(workspace)))
    assert {row["name"] for row in listed["skills"]} == {"shared", "project"}
    assert listed["catalog_errors"] == 0
    explicit = list_skills(SkillsPage(roots=[str(shared.parent), str(shared.parent)]))
    assert len(explicit["skills"]) == 1
    assert list_skills(SkillsPage(roots=[str(workspace)]))["skills"] == []
    with pytest.raises(ValueError, match="absolute"):
        list_skills(SkillsPage(roots=["relative"]))
    with pytest.raises(ValueError, match="workspace"):
        list_skills(SkillsPage(cwd="relative"))


def test_body_resources_hashes_and_version_change(tmp_path):
    directory = make_skill(tmp_path)
    row = selected(tmp_path)
    body = read_skill(resource(tmp_path, row))
    assert body["text"] == (directory / "SKILL.md").read_text(encoding="utf-8")
    reference = read_skill(resource(tmp_path, row, "references/日本語.md"))
    assert reference["text"] == "値: 42 ✅\n"
    assert reference["sha256"] == hashlib.sha256("値: 42 ✅\n".encode()).hexdigest()
    (directory / "SKILL.md").write_text("Changed version", encoding="utf-8")
    with pytest.raises(ValueError, match="content changed"):
        read_skill(resource(tmp_path, row, "references/日本語.md"))
    fresh = selected(tmp_path)
    assert fresh["skill_id"] == row["skill_id"]
    assert read_skill(resource(tmp_path, fresh))["text"] == "Changed version"
    directory.rename(tmp_path / "moved")
    with pytest.raises(ValueError, match="current catalog"):
        read_skill(resource(tmp_path, fresh))


@pytest.mark.parametrize("path", ["../private.txt", "/etc/passwd", "a/../../x", ".",
                                     "C:/Windows/a", "C:a", "\\root", "refs/a:stream"])
def test_resource_traversal_is_rejected(tmp_path, path):
    make_skill(tmp_path)
    with pytest.raises(ValueError, match="relative path"):
        read_skill(resource(tmp_path, selected(tmp_path), path))


def test_symlinks_stay_inside_selected_skill(tmp_path):
    root = tmp_path / "skills"
    directory = make_skill(root)
    outside = tmp_path / "private.txt"
    outside.write_text("not returned", encoding="utf-8")
    try:
        (directory / "outside").symlink_to(outside)
    except OSError:
        pytest.skip("Creating symlinks requires platform permission")
    row = selected(root)
    with pytest.raises(ValueError, match="outside"):
        read_skill(resource(root, row, "outside"))
    (directory / "inside").symlink_to(directory / "references/日本語.md")
    assert read_skill(resource(root, row, "inside"))["text"] == "値: 42 ✅\n"
    external_skill = make_skill(tmp_path / "other")
    (root / "escape").symlink_to(external_skill, target_is_directory=True)
    assert list_skills(SkillsPage(roots=[str(root)]))["catalog_errors"] == 1


def test_bounds_pagination_nontext_and_nonregular_resources(tmp_path, monkeypatch):
    directory = make_skill(tmp_path, "one")
    make_skill(tmp_path, "two")
    page = list_skills(SkillsPage(roots=[str(tmp_path)], limit=1))
    next_page = list_skills(SkillsPage(roots=[str(tmp_path)], limit=1, after=page["next_cursor"]))
    assert next_page["next_cursor"] is None
    assert page["skills"][0]["skill_id"] != next_page["skills"][0]["skill_id"]
    row = next(r for r in list_skills(SkillsPage(roots=[str(tmp_path)]))["skills"]
               if r["name"] == "one")
    (directory / "large").write_bytes(b"x" * 65537)
    with pytest.raises(ValueError, match="64 KiB"):
        read_skill(resource(tmp_path, row, "large"))
    (directory / "binary").write_bytes(b"\xff\x00")
    with pytest.raises(UnicodeDecodeError):
        read_skill(resource(tmp_path, row, "binary"))
    with pytest.raises(ValueError, match="regular file"):
        read_skill(resource(tmp_path, row, "references"))
    if hasattr(os, "mkfifo"):
        os.mkfifo(directory / "fifo")
        with pytest.raises(ValueError, match="regular file"):
            read_skill(resource(tmp_path, row, "fifo"))
    monkeypatch.setattr(common_skills, "MAX_ENTRIES", 1)
    with pytest.raises(ValueError, match="narrower roots"):
        list_skills(SkillsPage(roots=[str(tmp_path)]))


async def test_remote_grants_ledger_recovery_and_no_codex(tmp_path, monkeypatch):
    monkeypatch.setenv("ANYWHERE_CODEX_EXECUTABLE", str(tmp_path / "missing-codex"))
    root = tmp_path / "skills"
    make_skill(root)
    engine = Engine(tmp_path / "state")
    grants = {"allowed": frozenset({"skills_list", "skills_read", "operations_get"}),
              "other": frozenset({"skills_list", "skills_read", "operations_get"}),
              "old": frozenset({"computer_status"})}
    bridge = RemoteAgent(engine, grants)

    async def call(peer, tool, arguments):
        request = Request(operation_id=uuid.uuid4().hex, tool=tool, arguments=arguments)
        return Reply.model_validate_json(await bridge.dispatch(
            peer, request.model_dump_json().encode(),
        ))

    try:
        assert (await call("old", "skills_list", {"roots": [str(root)]})).state == "failed"
        listed = await call("allowed", "skills_list", {"roots": [str(root)]})
        assert listed.state == "completed"
        row = listed.data["skills"][0]
        args = resource(root, row, "references/日本語.md").model_dump()
        result = await call("allowed", "skills_read", args)
        assert result.state == "completed" and result.data["text"] == "値: 42 ✅\n"
        await engine.close()

        engine = Engine(tmp_path / "state")
        bridge = RemoteAgent(engine, grants)
        stored = await call("allowed", "operations_get", {"operation_id": result.operation_id})
        assert stored.state == "completed" and stored.data["data"]["text"] == "値: 42 ✅\n"
        assert (await call("other", "operations_get", {
            "operation_id": result.operation_id,
        })).state == "failed"
        bridge.revoke("allowed")
        assert (await call("allowed", "skills_read", args)).state == "failed"
    finally:
        await engine.close()


def test_resource_replaced_between_stat_and_open_is_not_returned(tmp_path, monkeypatch):
    directory = make_skill(tmp_path / "skills")
    path = directory / "references/日本語.md"
    replacement = tmp_path / "replacement"
    replacement.write_text("replacement must not be returned", encoding="utf-8")
    original_open = os.open

    def replace_then_open(target, flags):
        if target == path:
            os.replace(replacement, path)
        return original_open(target, flags)

    monkeypatch.setattr(common_skills.os, "open", replace_then_open)
    with pytest.raises(ValueError, match="changed while opening"):
        common_skills._read(directory, path)
