import asyncio
import json

import pytest

from anywhere_computer.connection import exchange, serve
from anywhere_computer.engine import Engine
from anywhere_computer.engine_selection import EngineSelection, engine_directory
from anywhere_computer.models import Request
from anywhere_computer.private_directory import create_private_directory


async def test_selected_data_keeps_endpoint_and_existing_operation(tmp_path):
    control, data = tmp_path / 'control', tmp_path / 'data'
    create_private_directory(control)
    engine = Engine(data)
    request = Request(operation_id='a' * 32, tool='files_write', arguments={
        'path': str(tmp_path / 'result.txt'), 'text': 'before selection',
    })
    try:
        written = await engine.execute(request)
        assert written.state == 'completed'
    finally:
        await engine.close()
    (control / 'engine-selection.json').write_text(
        EngineSelection(directory=str(data)).model_dump_json(),
    )
    stop = asyncio.Event()
    service = asyncio.create_task(serve(control, credential='selection-fixture', shutdown=stop))
    try:
        async with asyncio.timeout(5):
            while not (control / 'agent.json').exists():
                if service.done():
                    await service
                await asyncio.sleep(0.01)
        recovered = await exchange(control, 'operations_get',
            {'operation_id': request.operation_id}, credential='selection-fixture')
        assert recovered.data['data']['sha256'] == written.data['sha256']
        assert not (control / 'operations.sqlite3').exists()
        assert not (data / 'agent.json').exists()
    finally:
        stop.set()
        await asyncio.wait_for(service, 10)


@pytest.mark.parametrize('damage', ['pending', 'relative', 'missing', 'oversized', 'invalid'])
def test_invalid_selection_never_falls_back_to_legacy_state(tmp_path, damage):
    if damage == 'pending':
        (tmp_path / 'engine-migration.pending.json').write_text('{}')
    else:
        raw = {'directory': str(tmp_path / 'missing')}
        if damage == 'relative':
            raw['directory'] = 'relative'
        text = json.dumps(raw)
        if damage == 'oversized':
            text = ' ' * 8193
        elif damage == 'invalid':
            text = 'invalid json'
        (tmp_path / 'engine-selection.json').write_text(text)
    with pytest.raises((ValueError, RuntimeError)):
        engine_directory(tmp_path)
    assert not (tmp_path / 'operations.sqlite3').exists()
