import hashlib
import json
import stat
import zipfile

import pytest

from anywhere_computer.portable_archive import inspect_portable


def archive_fixture(tmp_path, *, name='runtime/python', content=b'fixture', digest=None,
                    extra=None, dirty=False, symlink=False):
    manifest = {'files': {name: digest or hashlib.sha256(content).hexdigest()},
                'platform': 'fixture-platform', 'release': {
                    'python_version': '1.2.3', 'source_commit': 'a' * 40, 'source_dirty': dirty,
                    'engine_api': {'minimum': 1, 'maximum': 1},
                    'ledger_schema': {'minimum': 0, 'maximum': 1, 'writes': 1}}}
    path = tmp_path / 'fixture.zip'
    with zipfile.ZipFile(path, 'w') as archive:
        entry = zipfile.ZipInfo('Anywhere Computer/' + name)
        entry.external_attr = ((stat.S_IFLNK if symlink else stat.S_IFREG) | 0o700) << 16
        archive.writestr(entry, content)
        archive.writestr('Anywhere Computer/manifest.json', json.dumps(manifest))
        if extra:
            archive.writestr('Anywhere Computer/' + extra, b'undeclared')
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def test_archive_integrity_without_extraction_or_execution(tmp_path):
    path, digest = archive_fixture(tmp_path)
    result = inspect_portable(path, expected_sha256=digest)
    assert result.file_count == 1 and result.source_commit == 'a' * 40
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize('options', [
    {'name': '../escape'}, {'name': 'runtime/../escape'}, {'name': 'C:/escape'},
    {'name': 'runtime\\escape'}, {'name': 'runtime/NUL.txt'}, {'name': 'runtime/file.'},
    {'extra': 'unexpected'}, {'extra': 'RUNTIME/PYTHON'}, {'symlink': True},
    {'digest': 'b' * 64}, {'dirty': True},
])
def test_invalid_archive_rejected_without_writes(tmp_path, options):
    path, digest = archive_fixture(tmp_path, **options)
    with pytest.raises(ValueError):
        inspect_portable(path, expected_sha256=digest)
    assert list(tmp_path.iterdir()) == [path]


def test_download_checksum_and_expansion_limits(tmp_path, monkeypatch):
    path, digest = archive_fixture(tmp_path)
    with pytest.raises(ValueError, match='checksum'):
        inspect_portable(path, expected_sha256='c' * 64)
    monkeypatch.setattr('anywhere_computer.portable_archive.MAX_EXPANDED_BYTES', 1)
    with pytest.raises(ValueError, match='expanded size'):
        inspect_portable(path, expected_sha256=digest)


def test_staging_preserves_existing_installation_and_uses_unique_directory(tmp_path):
    from anywhere_computer.portable_archive import stage_portable

    path, digest = archive_fixture(tmp_path)
    parent = tmp_path / 'installed'
    parent.mkdir()
    existing = parent / 'previous'
    existing.write_bytes(b'preserved')
    app = stage_portable(path, parent, expected_sha256=digest)
    assert (app / 'runtime/python').read_bytes() == b'fixture'
    assert existing.read_bytes() == b'preserved'
    assert not (app.parent / 'archive.zip').exists()
    second = stage_portable(path, parent, expected_sha256=digest)
    assert second != app


def test_invalid_staging_cleans_only_its_own_directory(tmp_path):
    from anywhere_computer.portable_archive import stage_portable

    path, digest = archive_fixture(tmp_path, extra='undeclared')
    parent = tmp_path / 'installed'
    parent.mkdir()
    existing = parent / 'previous'
    existing.write_bytes(b'preserved')
    with pytest.raises(ValueError):
        stage_portable(path, parent, expected_sha256=digest)
    assert list(parent.iterdir()) == [existing]


def test_staging_extracts_validated_snapshot_even_if_original_changes(tmp_path, monkeypatch):
    import anywhere_computer.portable_archive as portable

    path, digest = archive_fixture(tmp_path)
    inspect = portable.inspect_portable
    def replacing_inspect(snapshot, **kwargs):
        result = inspect(snapshot, **kwargs)
        path.write_bytes(b'replaced original after verification')
        return result
    monkeypatch.setattr(portable, 'inspect_portable', replacing_inspect)
    parent = tmp_path / 'installed'
    parent.mkdir()
    app = portable.stage_portable(path, parent, expected_sha256=digest)
    assert (app / 'runtime/python').read_bytes() == b'fixture'


@pytest.mark.parametrize('kind', ['valid', 'api', 'ledger', 'downgrade'])
def test_candidate_compatibility_reads_selected_ledger_without_changing_it(tmp_path, kind):
    from dataclasses import replace

    from anywhere_computer.engine_selection import EngineSelection
    from anywhere_computer.portable_archive import (
        CompatibilityRange,
        LedgerCompatibility,
        require_portable_compatibility,
    )
    from anywhere_computer.state import Ledger

    control = tmp_path / 'control'
    control.mkdir()
    data = tmp_path / 'selected'
    ledger = Ledger(data)
    ledger.close()
    (control / 'engine-selection.json').write_text(
        EngineSelection(directory=str(data)).model_dump_json(), encoding='utf-8',
    )
    before = (data / 'operations.sqlite3').read_bytes()
    path, digest = archive_fixture(tmp_path)
    inspection = inspect_portable(path, expected_sha256=digest)
    if kind == 'api':
        inspection = replace(inspection, engine_api=CompatibilityRange(minimum=2, maximum=2))
    elif kind == 'ledger':
        inspection = replace(inspection, ledger_schema=LedgerCompatibility(
            minimum=0, maximum=0, writes=0))
    elif kind == 'downgrade':
        inspection = replace(inspection, ledger_schema=LedgerCompatibility(
            minimum=0, maximum=1, writes=0))
    if kind == 'valid':
        assert require_portable_compatibility(inspection, control) == 1
    else:
        with pytest.raises(ValueError):
            require_portable_compatibility(inspection, control)
    assert (data / 'operations.sqlite3').read_bytes() == before
