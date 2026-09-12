import hashlib

import pytest

from anywhere_computer import skills_context


@pytest.fixture
async def skill_catalog(tmp_path, monkeypatch):
    first = tmp_path / 'one' / 'SKILL.md'
    second = tmp_path / 'two' / 'SKILL.md'
    first.parent.mkdir()
    second.parent.mkdir()
    first.write_text('---\nname: example\n---\nUse the selected workspace.\n')
    second.write_text('Never read this disabled skill.')
    rows = [
        {'path': str(first), 'name': 'example', 'description': 'Test skill',
         'enabled': True, 'pluginId': 'example@test', 'dependencies': {'secret': 'not-returned'}},
        {'path': str(second), 'name': 'disabled', 'description': 'Hidden', 'enabled': False},
    ]
    async def request(method, params):
        assert method == 'skills/list'
        assert params['forceReload'] is False
        return {'data': [{'cwd': str(tmp_path), 'skills': rows,
                          'errors': [{'message': 'private error detail'}]}]}
    monkeypatch.setattr(skills_context, 'codex_request', request)
    return first, second, rows


async def test_enabled_catalog_and_selected_read_only(skill_catalog, tmp_path):
    first, _, _ = skill_catalog
    listed = await skills_context.list_codex_skills(cwd=str(tmp_path))
    assert len(listed['skills']) == 1
    assert listed['catalog_errors'] == 1
    assert 'path' not in listed['skills'][0]
    assert 'private error detail' not in str(listed)
    assert 'not-returned' not in str(listed)
    selected = await skills_context.read_codex_skill(
        listed['skills'][0]['skill_id'], cwd=str(tmp_path),
    )
    assert selected['text'] == first.read_bytes().decode("utf-8")
    assert selected['sha256'] == hashlib.sha256(first.read_bytes()).hexdigest()
    assert selected['skill_path'] == str(first.resolve())
    assert selected['skill_directory'] == str(first.parent.resolve())
    assert 'reference' in selected['instructions']


async def test_disabled_unknown_and_replaced_path_rejected(skill_catalog, tmp_path):
    first, second, rows = skill_catalog
    with pytest.raises(ValueError, match='current enabled catalog'):
        await skills_context.read_codex_skill(
            hashlib.sha256(str(second).encode()).hexdigest(), cwd=str(tmp_path),
        )
    first_id = hashlib.sha256(str(first).encode()).hexdigest()
    rows[0]['enabled'] = False
    with pytest.raises(ValueError):
        await skills_context.read_codex_skill(first_id, cwd=str(tmp_path))
    rows[0]['enabled'] = True
    secret = tmp_path / 'auth.json'
    secret.write_text('not-a-skill')
    first.unlink()
    first.symlink_to(secret)
    with pytest.raises(ValueError, match='SKILL.md'):
        await skills_context.read_codex_skill(first_id, cwd=str(tmp_path))


async def test_skill_bounds_and_pagination(skill_catalog, tmp_path):
    first, _, rows = skill_catalog
    rows[1]['enabled'] = True
    page = await skills_context.list_codex_skills(cwd=str(tmp_path), limit=1)
    assert page['next_cursor']
    next_page = await skills_context.list_codex_skills(
        cwd=str(tmp_path), limit=1, after=page['next_cursor'],
    )
    assert next_page['next_cursor'] is None
    assert next_page['skills'][0]['skill_id'] != page['skills'][0]['skill_id']
    with pytest.raises(ValueError):
        await skills_context.list_codex_skills(cwd='relative')
    with pytest.raises(ValueError):
        await skills_context.list_codex_skills(cwd=str(tmp_path), limit=0)
    first.write_bytes(b'x' * 65537)
    with pytest.raises(ValueError, match='64 KiB'):
        await skills_context.read_codex_skill(
            hashlib.sha256(str(first).encode()).hexdigest(), cwd=str(tmp_path),
        )
