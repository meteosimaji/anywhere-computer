import base64
import hashlib

import pytest

from anywhere_computer.engine import Engine
from anywhere_computer.locking import ProcessLock
from anywhere_computer.models import Request
from anywhere_computer.state_migration import stage_shared_engine


async def execute(engine, identity, tool, **arguments):
    result = await engine.execute(Request(
        operation_id=identity * 32, tool=tool, arguments=arguments,
    ))
    assert result.state == 'completed', result.error
    return result


async def test_shared_state_keeps_operations_backups_and_transfer_chunks(tmp_path):
    local = tmp_path / 'local'
    http = tmp_path / 'http'
    remote = http / 'http-server/engine'
    destination = tmp_path / 'combined'
    records = []
    for index, source in enumerate((local, remote)):
        engine = Engine(source)
        target = tmp_path / f'file-{index}.txt'
        original = f'original {index}🙂'
        target.write_text(original, encoding='utf-8')
        digest = hashlib.sha256(original.encode()).hexdigest()
        try:
            record = await execute(engine, str(index + 1), 'files_write',
                                   path=str(target), text='changed', mode='replace',
                                   expected_sha256=digest)
            download = ('a' if index == 0 else 'b') * 32
            await execute(engine, str(index + 3), 'download_begin', transfer_id=download,
                          path=str(target))
            upload = ('c' if index == 0 else 'd') * 32
            payload = f'upload {index}'.encode()
            output = tmp_path / f'upload-{index}.txt'
            await execute(engine, str(index + 5), 'upload_begin', transfer_id=upload,
                          path=str(output), total_bytes=len(payload),
                          sha256=hashlib.sha256(payload).hexdigest())
            await execute(engine, str(index + 7), 'upload_chunk', transfer_id=upload,
                          offset=0, data_base64=base64.b64encode(payload).decode())
            records.append((record, target, original, download, upload, output, payload))
        finally:
            await engine.close()
    before = {path: path.read_bytes() for source in (local, remote)
              for path in source.rglob('*') if path.is_file() and not path.name.endswith('.lock')}
    stage_shared_engine(local, http, destination)
    assert all(path.read_bytes() == body for path, body in before.items())
    merged = Engine(destination)
    try:
        for record, target, original, download, upload, output, payload in records:
            assert merged.ledger.get(record.operation_id) == record
            request = Request(operation_id=record.operation_id, tool='files_write', arguments={
                'path': str(target), 'text': 'changed', 'mode': 'replace',
                'expected_sha256': hashlib.sha256(original.encode()).hexdigest(),
            })
            assert await merged.execute(request) == record
            restored = await merged.tools['files_restore'].handler(
                merged.tools['files_restore'].schema.model_validate({
                    'path': str(target), 'backup_id': record.data['backup_id'],
                    'expected_sha256': record.data['sha256'],
                }))
            assert restored['restored_backup_id'] == record.data['backup_id']
            assert target.read_text(encoding='utf-8') == original
            # Download still serves its prepared copy, although the source was restored.
            data = await merged.tools['download_read'].handler(
                merged.tools['download_read'].schema.model_validate({
                    'transfer_id': download, 'offset': 0, 'limit': 100,
                }))
            assert base64.b64decode(data['data_base64']) == b'changed'
            result = await merged.tools['upload_commit'].handler(
                merged.tools['upload_commit'].schema.model_validate({'transfer_id': upload}))
            assert result['state'] == 'complete'
            assert output.read_bytes() == payload
    finally:
        await merged.close()


@pytest.mark.parametrize('conflict', ['operation', 'settings'])
async def test_conflict_leaves_sources_and_destination_untouched(tmp_path, conflict):
    local, http = tmp_path / 'local', tmp_path / 'http'
    sources = (local, http / 'http-server/engine')
    for index, source in enumerate(sources):
        engine = Engine(source)
        try:
            if conflict == 'operation':
                await execute(engine, '1', 'computer_status')
            else:
                await execute(engine, str(index + 1), 'settings_update',
                              key='file_read_line_limit', value=100 + index)
        finally:
            await engine.close()
    snapshots = [(source / 'operations.sqlite3').read_bytes() for source in sources]
    with pytest.raises(ValueError, match='Conflicting'):
        stage_shared_engine(local, http, tmp_path / 'combined')
    assert not (tmp_path / 'combined').exists()
    assert snapshots == [(source / 'operations.sqlite3').read_bytes() for source in sources]


async def test_running_service_is_not_migrated(tmp_path):
    local, http = tmp_path / 'local', tmp_path / 'http'
    local.mkdir()
    http.mkdir()
    with ProcessLock(local / 'agent.lock'):
        with pytest.raises(TimeoutError):
            stage_shared_engine(local, http, tmp_path / 'combined')
    assert not (tmp_path / 'combined').exists()


async def test_legacy_ledger_is_migrated_only_in_snapshot(tmp_path):
    import json
    import sqlite3

    local, http = tmp_path / 'local', tmp_path / 'http'
    local.mkdir()
    reply = {'operation_id': 'a' * 32, 'state': 'completed', 'data': {'saved': 'legacy'}}
    with sqlite3.connect(local / 'operations.sqlite3') as db:
        db.execute('CREATE TABLE operations(id TEXT PRIMARY KEY,tool TEXT NOT NULL,'
                   'digest TEXT NOT NULL,started REAL NOT NULL,reply TEXT NOT NULL)')
        db.execute('INSERT INTO operations VALUES(?,?,?,?,?)',
                   ('a' * 32, 'files_read', 'b' * 64, 1.0, json.dumps(reply)))
    remote = Engine(http / 'http-server/engine')
    await remote.close()
    original = (local / 'operations.sqlite3').read_bytes()
    stage_shared_engine(local, http, tmp_path / 'combined')
    assert (local / 'operations.sqlite3').read_bytes() == original
    merged = Engine(tmp_path / 'combined')
    try:
        assert merged.ledger.get('a' * 32).data == {'saved': 'legacy'}
    finally:
        await merged.close()


@pytest.mark.parametrize('damage', ['unknown-table', 'result-hash', 'backup-hash'])
async def test_invalid_source_never_publishes_shared_state(tmp_path, damage):
    import sqlite3

    local, http = tmp_path / 'local', tmp_path / 'http'
    for source in (local, http / 'http-server/engine'):
        engine = Engine(source)
        await execute(engine, '1' if source == local else '2', 'computer_status')
        await engine.close()
    if damage == 'backup-hash':
        (local / 'backups' / ('a' * 64)).write_bytes(b'invalid')
    else:
        with sqlite3.connect(local / 'operations.sqlite3') as db:
            if damage == 'unknown-table':
                db.execute('CREATE TABLE future_data(id TEXT PRIMARY KEY)')
            else:
                db.execute("UPDATE operation_results SET body='{}'")
    before = (local / 'operations.sqlite3').read_bytes()
    with pytest.raises(ValueError):
        stage_shared_engine(local, http, tmp_path / 'combined')
    assert not (tmp_path / 'combined').exists()
    assert (local / 'operations.sqlite3').read_bytes() == before


async def test_manual_archives_stay_in_source_while_owned_backups_migrate(tmp_path):
    from anywhere_computer.engine_migration import _digest

    local, http = tmp_path / 'local', tmp_path / 'http'
    for source in (local, http / 'http-server/engine'):
        engine = Engine(source)
        await engine.close()
    archive = local / 'backups/pre-upgrade'
    archive.mkdir()
    preserved = archive / 'manual.txt'
    preserved.write_bytes(b'manual archive')
    content = b'owned backup'
    name = hashlib.sha256(content).hexdigest()
    (local / 'backups' / name).write_bytes(content)
    before = _digest(local)
    preserved.write_bytes(b'manual archive retained')
    assert _digest(local) == before
    destination = tmp_path / 'combined'
    stage_shared_engine(local, http, destination)
    assert (destination / 'backups' / name).read_bytes() == content
    assert not (destination / 'backups/pre-upgrade').exists()
    assert preserved.read_bytes() == b'manual archive retained'
