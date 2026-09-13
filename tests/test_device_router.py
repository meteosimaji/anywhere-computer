import asyncio
import uuid

import pytest
from test_http_client import http_remote as http_remote

from anywhere_computer.device_router import DeviceRouter
from anywhere_computer.engine import Engine
from anywhere_computer.models import Request


def request(outer_tool, operation_id=None, **arguments):
    return Request(operation_id=operation_id or uuid.uuid4().hex,
                   tool=outer_tool, arguments=arguments)


class EngineBackend:
    def __init__(self, engine, tracker):
        self.engine = engine
        self.tracker = tracker

    async def catalog(self):
        return self.engine.catalog()

    async def execute(self, request):
        self.tracker.append(request)
        return await self.engine.execute(request)

    async def close(self):
        pass


@pytest.fixture
async def routed(tmp_path):
    local = Engine(tmp_path / 'local')
    remote = Engine(tmp_path / 'remote')
    sent = []

    async def catalog():
        return local.catalog()

    router = DeviceRouter(tmp_path / 'registry', catalog, local.execute,
                          backend_factory=lambda _: EngineBackend(remote, sent))
    first = router.store.add('One', 'one')['device_id']
    second = router.store.add('Two', 'two')['device_id']
    try:
        yield router, local, remote, sent, first, second
    finally:
        router.close()
        await local.close()
        await remote.close()


async def test_catalog_is_connector_only_and_routes_explicitly(routed):
    router, local, remote, sent, first, _ = routed
    tools = await router.catalog()
    assert len(tools) == len(local.catalog()) + 3
    assert len({tool['name'] for tool in tools}) == len(tools)
    assert not any(tool['name'].startswith('devices_') for tool in remote.catalog())
    listed = await router.execute(request('devices_list'))
    assert [item['device_id'] for item in listed.data['devices']][0] == 'local'
    target = await router.execute(request('devices_tools', device_id=first))
    assert target.data['device_id'] == first and target.data['tools'] == remote.catalog()
    result = await router.execute(request('devices_call', device_id=first,
                                         tool='computer_status', arguments={}))
    assert result.state == 'completed' and result.data['device_id'] == first
    assert len(sent) == 1
    assert (await router.execute(request('devices_call', tool='computer_status'))).state == 'failed'
    assert (await router.execute(request('devices_call', device_id='missing',
                                        tool='computer_status'))).state == 'failed'


async def test_replayed_route_is_not_forwarded_and_cross_device_reuse_fails(routed, tmp_path):
    router, _, remote, sent, first, second = routed
    path = tmp_path / 'created-once.txt'
    original = request('devices_call', device_id=first, tool='files_write',
                       arguments={'path': str(path), 'text': 'once'})
    replies = await asyncio.gather(router.execute(original), router.execute(original))
    assert sorted(reply.state for reply in replies) == ['completed', 'unknown']
    repeated = await router.execute(original)
    assert repeated.state == 'unknown' and repeated.data['previously_forwarded']
    assert path.read_text() == 'once'
    # A fresh execution would fail because create refuses an existing file.
    assert remote.ledger.get(original.operation_id).state == 'completed'
    assert len(sent) == 1
    moved = original.model_copy(update={'arguments': {**original.arguments, 'device_id': second}})
    assert (await router.execute(moved)).state == 'failed'
    assert len(sent) == 1
    # Binding survives connector restart; only digests are stored, not file contents/arguments.
    router.store.db.commit()
    row = router.store.db.execute('SELECT digest FROM routed_operations WHERE id=?',
                                  (original.operation_id,)).fetchone()
    assert len(row[0]) == 64
    async def catalog():
        return remote.catalog()
    restored = DeviceRouter(router.store.directory, catalog, remote.execute,
                            backend_factory=router.backend_factory)
    try:
        assert (await restored.execute(moved)).state == 'failed'
    finally:
        restored.close()


async def test_endpoint_change_prefixes_removed_and_read_only_id_binding(routed):
    router, _, _, sent, first, second = routed
    original = request('devices_tools', device_id=first)
    assert (await router.execute(original)).state == 'completed'
    changed = original.model_copy(update={'arguments': {'device_id': second}})
    assert (await router.execute(changed)).state == 'failed'
    with router.store.db:
        router.store.db.execute('UPDATE devices SET ssh_host=? WHERE id=?', ('changed', first))
    assert (await router.execute(original)).state == 'failed'
    for tool in ('devices_call', 'devices_other', '__catalog'):
        assert (await router.execute(request('devices_call', device_id=second,
                                            tool=tool))).state == 'failed'
    assert not sent
    assert router._remote_tools([{'name': 'files_read'}, {'name': 'devices_other'},
                                 {'name': '__catalog'}]) == [{'name': 'files_read'}]
    with pytest.raises(ValueError, match='duplicate'):
        router._remote_tools([{'name': 'files_read'}, {'name': 'files_read'}])


async def test_unknown_response_preserves_target_and_cleanup_cannot_override_success(routed):
    router, _, remote, sent, first, _ = routed
    closed = []

    class LostBackend(EngineBackend):
        async def execute(self, operation):
            await super().execute(operation)
            raise ConnectionError('private backend failure')

        async def close(self):
            closed.append(True)
            raise ConnectionError('private cleanup failure')

    router.backend_factory = lambda _: LostBackend(remote, sent)
    operation = request('devices_call', device_id=first, tool='computer_status')
    response = await router.execute(operation)
    assert response.state == 'unknown' and response.operation_id == operation.operation_id
    assert 'private' not in response.model_dump_json()
    assert len(sent) == 1 and closed == [True]
    assert remote.ledger.get(operation.operation_id).state == 'completed'

    class BadCleanup(EngineBackend):
        async def close(self):
            raise RuntimeError('private cleanup failure')
    router.backend_factory = lambda _: BadCleanup(remote, sent)
    lookup = await router.execute(request('devices_call', device_id=first, tool='operations_get',
                                         arguments={'operation_id': operation.operation_id}))
    assert lookup.state == 'completed'


async def test_permission_removal_blocks_replay_before_execution(routed):
    router, _, remote, sent, first, _ = routed
    operation = request('devices_call', device_id=first, tool='computer_status')
    assert (await router.execute(operation)).state == 'completed'

    class RestrictedBackend(EngineBackend):
        async def catalog(self):
            return []
    router.backend_factory = lambda _: RestrictedBackend(remote, sent)
    assert (await router.execute(operation)).state == 'failed'
    assert len(sent) == 1


async def test_http_authorization_and_session_cleanup(http_remote, tmp_path):
    from anywhere_computer.http_client import HTTPBackend

    existing, adapter, _, _, calls, _ = http_remote
    local = Engine(tmp_path / 'router-local')
    async def catalog():
        return local.catalog()
    router = DeviceRouter(tmp_path / 'router-registry', catalog, local.execute,
                          backend_factory=lambda _: HTTPBackend(existing.tokens,
                                                                 wire=existing.wire))
    try:
        identity = router.store.add_http('HTTP', existing.tokens.resource,
                                         existing.tokens.client, 'device')['device_id']
        result = await router.execute(request('devices_call', device_id=identity,
                                             tool='computer_status'))
        assert result.state == 'completed' and result.data['device_id'] == identity
        assert not adapter.sessions
        invoked = sum(packet is not None and packet['method'] == 'tools/call' for packet in calls)
        forbidden = await router.execute(request('devices_call', device_id=identity,
                                                 tool='terminal_start', arguments={}))
        assert forbidden.state == 'failed'
        assert sum(packet is not None and packet['method'] == 'tools/call'
                   for packet in calls) == invoked
        assert not adapter.sessions
    finally:
        router.close()
        await local.close()


def test_routed_catalog_removes_only_transport_request_id():
    from anywhere_computer.mcp_server import with_request_id

    raw = [{'name': 'computer_status', 'inputSchema': {
        'type': 'object', 'properties': {}, 'additionalProperties': False,
    }}]
    advertised = with_request_id(raw)
    assert DeviceRouter._remote_tools(advertised) == raw
    assert 'request_id' in advertised[0]['inputSchema']['properties']


async def test_nested_request_id_is_rejected_before_remote_dispatch(routed):
    router, _, _, sent, first, _ = routed
    reply = await router.execute(request(
        'devices_call', device_id=first, tool='computer_status',
        arguments={'request_id': uuid.uuid4().hex},
    ))
    assert reply.state == 'failed'
    assert 'outer devices_call' in reply.error
    assert not sent
