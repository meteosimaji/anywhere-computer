import asyncio

import httpx
import pytest
from test_client_tokens import MemoryVault
from test_http_service import RESOURCE, authenticate, initialize, setup

from anywhere_computer import engine_migration as migration
from anywhere_computer.connection import exchange, serve
from anywhere_computer.engine import Engine
from anywhere_computer.engine_selection import engine_directory
from anywhere_computer.http_service import http_service, load_http_config
from anywhere_computer.models import Request
from anywhere_computer.owner_credentials import OwnerCredentials


@pytest.fixture
async def prepared(tmp_path, unused_tcp_port):
    local, http = tmp_path / 'local', tmp_path / 'http'
    engine = Engine(local)
    try:
        await engine.execute(Request(operation_id='a' * 32, tool='computer_status'))
    finally:
        await engine.close()
    config = await setup(http, unused_tcp_port)
    owner = OwnerCredentials(http, resource=RESOURCE, owner='owner', vault=MemoryVault())
    owner.initialize('synthetic owner password')
    async with http_service(http, credentials=owner):
        async with httpx.AsyncClient(base_url=f'http://127.0.0.1:{config.port}',
                                     trust_env=False) as client:
            token = await authenticate(client)
            headers = await initialize(client, token)
            request = {'jsonrpc': '2.0', 'id': 'write', 'method': 'tools/call', 'params': {
                'name': 'files_write', 'arguments': {
                    'request_id': 'b' * 32, 'path': str(tmp_path / 'before.txt'), 'text': 'before',
                },
            }}
            result = (await client.post('/mcp', headers=headers, json=request)).json()
            assert result['result']['structuredContent']['state'] == 'completed'
    return local, http, config, owner, token, request


async def verify_connections(prepared, monkeypatch):
    local, http, config, owner, token, request = prepared
    stop = asyncio.Event()
    monkeypatch.setattr('anywhere_computer.connection.local_credential',
                        lambda *a, **kw: 'migration-fixture')
    agent = asyncio.create_task(serve(local, credential='migration-fixture', shutdown=stop))
    try:
        try:
            async with asyncio.timeout(5):
                while not (local / 'agent.json').exists():
                    if agent.done():
                        await agent
                        pytest.fail('Agent exited before publishing startup information')
                    await asyncio.sleep(0.01)
        except TimeoutError:
            # Record code locations only, never frame locals or credentials.
            locations = [f'{frame.f_code.co_name}:{frame.f_lineno}'
                         for frame in agent.get_stack(limit=8)]
            pytest.fail(f'Agent startup exceeded 5 seconds; done={agent.done()}; '
                        f'cancelled={agent.cancelled()}; stack={locations}', pytrace=False)
        old = await exchange(local, 'operations_get', {'operation_id': 'a' * 32})
        assert old.data['state'] == 'completed'
        async with http_service(http, credentials=owner):
            async with httpx.AsyncClient(base_url=f'http://127.0.0.1:{config.port}',
                                         trust_env=False) as client:
                headers = await initialize(client, token)
                # The old expected create would now fail if the operation were repeated.
                result = (await client.post('/mcp', headers=headers, json=request)).json()
                assert result['result']['structuredContent']['state'] == 'completed'
    finally:
        stop.set()
        await asyncio.wait_for(agent, 10)


async def test_migration_selects_both_entrances_and_keeps_existing_login(prepared, monkeypatch):
    local, http, config, *_ = prepared
    selected = migration.migrate_engine(local, http)
    assert selected['changed'] is True
    assert engine_directory(local) != local
    assert load_http_config(http).shared_agent_directory == str(local)
    assert load_http_config(http).device == config.device
    assert migration.migrate_engine(local, http)['changed'] is False
    await verify_connections(prepared, monkeypatch)


@pytest.mark.parametrize('fail_at', [1, 2, 3, 4])
async def test_interrupted_selection_resumes_without_new_login_or_duplicate_write(
    prepared, monkeypatch, fail_at,
):
    local, http, *_ = prepared
    original = migration._write
    calls = 0

    def interrupted(*args, **kwargs):
        nonlocal calls
        calls += 1
        result = original(*args, **kwargs)
        if calls == fail_at:
            raise OSError('injected interruption')
        return result

    monkeypatch.setattr(migration, '_write', interrupted)
    with pytest.raises(OSError, match='injected'):
        migration.migrate_engine(local, http)
    with pytest.raises(RuntimeError, match='incomplete'):
        engine_directory(local)
    monkeypatch.setattr(migration, '_write', original)
    result = migration.migrate_engine(local, http)
    assert result['changed'] is True
    assert not (local / migration.PENDING).exists()
    assert not (http / migration.PENDING).exists()
    await verify_connections(prepared, monkeypatch)


async def test_cli_unifies_existing_state(prepared, monkeypatch, capsys):
    import json

    from anywhere_computer import cli

    local, http, *_ = prepared
    monkeypatch.setattr('sys.argv', ['anywhere', 'engine-unify', '--state-dir', str(local),
                                    '--http-state-dir', str(http)])
    cli.main()
    result = json.loads(capsys.readouterr().out)
    assert result['state'] == 'selected' and result['credentials_preserved'] is True
    await verify_connections(prepared, monkeypatch)


@pytest.mark.parametrize('fail_at', [5, 6])
async def test_lost_completion_acknowledgment_never_rolls_back_data(prepared, monkeypatch, fail_at):
    local, http, *_ = prepared
    original = migration._sync
    calls = 0

    def interrupted(directory):
        nonlocal calls
        calls += 1
        original(directory)
        if calls == fail_at:
            raise OSError('lost completion acknowledgment')

    monkeypatch.setattr(migration, '_sync', interrupted)
    with pytest.raises(OSError, match='lost completion'):
        migration.migrate_engine(local, http)
    monkeypatch.setattr(migration, '_sync', original)
    assert migration.migrate_engine(local, http)['state'] == 'selected'
    await verify_connections(prepared, monkeypatch)


async def test_transfer_administration_follows_selected_store(prepared):
    from anywhere_computer.downloads import Downloads
    from anywhere_computer.models import BeginDownload, TransferId
    from anywhere_computer.transfer_admin import list_transfers, release_transfer

    local, http, *_ = prepared
    identity = 'e' * 32
    source = local.parent / 'before.txt'
    legacy = Downloads(local)
    legacy.begin(BeginDownload(transfer_id=identity, path=str(source)))
    migration.migrate_engine(local, http)
    for directory, area in ((local, 'local'), (http, 'http')):
        result = list_transfers(directory, area=area, kind='download')
        assert result['storage_scope'] == 'shared'
        assert result['transfers'][0]['storage_id'] == identity
    released = release_transfer(http, area='http', kind='download', storage_id=identity)
    assert released['state'] == 'closed'
    assert list_transfers(local, area='local', kind='download')['transfers'][0]['state'] == 'closed'
    assert legacy.status(TransferId(transfer_id=identity))['state'] == 'ready'
