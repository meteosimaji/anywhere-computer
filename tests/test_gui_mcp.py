import asyncio
import time

import pytest

from anywhere_computer.gui_mcp import GUIMCP, GUIClick, GUIKey, GUIObserve, GUIType


class Peer:
    def __init__(self):
        self.calls = []
        self.entries = {'a' * 32: object()}
        self.metadata = None
        self.app = None

    def status(self, session_id, *, owner):
        if owner != 'owner':
            raise ValueError('Not owned')

    async def call(self, session_id, name, arguments, *, owner):
        self.status(session_id, owner=owner)
        self.calls.append((name, arguments))
        if name == 'app':
            self.app = arguments['name']
        text = (f'Snapshot ID: snapshot-1\nApplication: {self.app}\n  elem_1 - button'
                if name == 'see' else 'done')
        return {'content': [{'type': 'text', 'text': text}], 'isError': False,
                '_meta': self.metadata}


async def test_observation_owner_staleness_and_consumption():
    peer = Peer()
    gui = GUIMCP(peer)
    sid = 'a' * 32
    seen = await gui.observe(GUIObserve(session_id=sid, app='Calculator'), owner='owner')
    with pytest.raises(ValueError, match='Not owned'):
        await gui.observe(GUIObserve(session_id=sid, app='Other'), owner='other')
    assert gui.observations[sid].identity == seen['observation_id']
    action = GUIClick(session_id=sid, observation_id=seen['observation_id'], element_id='elem_1')
    with pytest.raises(ValueError, match='another connection'):
        await gui.act(action, owner='other')
    assert len(peer.calls) == 2
    gui.observations[sid].created = time.monotonic() - 61
    with pytest.raises(ValueError, match='stale'):
        await gui.act(action, owner='owner')
    assert len(peer.calls) == 2
    seen = await gui.observe(GUIObserve(session_id=sid, app='Calculator'), owner='owner')
    action.observation_id = seen['observation_id']
    await gui.act(action, owner='owner')
    assert peer.calls[-1] == ('click', {'on': 'elem_1', 'snapshot': 'snapshot-1'})
    with pytest.raises(ValueError, match='missing'):
        await gui.act(action, owner='owner')
    assert len(peer.calls) == 5


async def test_focus_and_keys_are_explicit_and_only_valid_elements_dispatch():
    peer = Peer()
    gui = GUIMCP(peer)
    sid = 'a' * 32
    seen = await gui.observe(GUIObserve(session_id=sid, app='Calculator'), owner='owner')
    common = {'session_id': sid, 'observation_id': seen['observation_id']}
    with pytest.raises(ValueError, match='Element'):
        await gui.act(GUIClick(**common, element_id='invented'), owner='owner')
    with pytest.raises(ValueError, match='key'):
        await gui.act(GUIKey(**common, keys=['unknown']), owner='owner')
    assert len(peer.calls) == 2
    await gui.act(GUIType(**common, text='12+30', press_return=True), owner='owner')
    assert peer.calls[-2] == ('app', {'action': 'focus', 'name': 'Calculator'})
    assert peer.calls[-1] == ('type', {'text': '12+30', 'press_return': True,
                                     'snapshot': 'snapshot-1'})


async def test_coordinate_metadata_is_validated_and_unrelated_metadata_omitted():
    peer = Peer()
    peer.metadata = {'private': 'omit-me', 'coordinate_context': {
        'version': 1, 'logical_space': 'global_display_points', 'origin': 'top_left',
        'logical_bounds': {'x': 20, 'y': 30, 'width': 100, 'height': 50},
        'delivered_image_size': {'width': 200, 'height': 100},
        'reference_id': 'snapshot-1', 'private': 'also-omit',
    }}
    gui = GUIMCP(peer)
    args = GUIObserve(session_id='a' * 32, app='Calculator')
    result = await gui.observe(args, owner='owner')
    assert result['coordinate_status'] == 'validated'
    assert result['coordinate_context']['logical_bounds']['width'] == 100
    assert 'omit' not in str(result)
    peer.metadata['coordinate_context']['reference_id'] = 'different'
    result = await gui.observe(args, owner='owner')
    assert result['coordinate_status'] == 'unsupported_or_invalid'
    assert result['coordinate_context'] is None


@pytest.mark.parametrize(('keys', 'expected'), [
    (['ESCAPE'], 'escape'), (['ESC'], 'escape'), (['CMD', 'ENTER'], 'cmd,return'),
])
async def test_gui_key_normalizes_documented_case_and_aliases(keys, expected):
    peer = Peer()
    gui = GUIMCP(peer)
    sid = 'a' * 32
    seen = await gui.observe(GUIObserve(session_id=sid, app='Calculator'), owner='owner')
    await gui.act(GUIKey(session_id=sid, observation_id=seen['observation_id'], keys=keys),
                  owner='owner')
    assert peer.calls[-1] == ('hotkey', {'keys': expected})


async def test_focus_and_input_cannot_be_interleaved_by_another_typed_session():
    peer = Peer()
    peer.entries['b' * 32] = object()
    gui = GUIMCP(peer)
    first = await gui.observe(GUIObserve(session_id='a' * 32, app='Editor'), owner='owner')
    second = await gui.observe(GUIObserve(session_id='b' * 32, app='Browser'), owner='owner')
    focused, release = asyncio.Event(), asyncio.Event()
    original = peer.call

    async def delayed(session_id, name, arguments, *, owner):
        result = await original(session_id, name, arguments, owner=owner)
        if name == 'app':
            focused.set()
            await release.wait()
        return result

    peer.call = delayed
    action = asyncio.create_task(gui.act(GUIType(
        session_id='a' * 32, observation_id=first['observation_id'], text='hello'), owner='owner'))
    try:
        await asyncio.wait_for(focused.wait(), 1)
        count = len(peer.calls)
        with pytest.raises(ValueError, match='busy'):
            await gui.observe(GUIObserve(session_id='b' * 32, app='Browser'), owner='owner')
        assert len(peer.calls) == count
    finally:
        release.set()
        await action
    with pytest.raises(ValueError, match='missing'):
        await gui.act(GUIKey(session_id='b' * 32, observation_id=second['observation_id'],
                            keys=['return']), owner='owner')
    assert peer.calls[-1][0] == 'type'
    await gui.observe(GUIObserve(session_id='b' * 32, app='Browser'), owner='owner')


async def test_cancelled_observation_releases_typed_gui_slot():
    peer = Peer()
    gui = GUIMCP(peer)
    started = asyncio.Event()
    original = peer.call

    async def waiting(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    peer.call = waiting
    observation = asyncio.create_task(gui.observe(
        GUIObserve(session_id='a' * 32, app='Editor'), owner='owner'))
    await asyncio.wait_for(started.wait(), 1)
    observation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await observation
    peer.call = original
    assert (await gui.observe(GUIObserve(session_id='a' * 32, app='Editor'),
                              owner='owner'))['action_ready']


async def test_engine_records_busy_without_replaying_input(tmp_path):
    from anywhere_computer.engine import Engine
    from anywhere_computer.models import Request

    engine = Engine(tmp_path)
    peer = Peer()
    engine.gui_mcp = GUIMCP(peer)
    focused, release = asyncio.Event(), asyncio.Event()
    original = peer.call

    async def delayed(session_id, name, arguments, *, owner):
        result = await original(session_id, name, arguments, owner=owner)
        if name == 'app':
            focused.set()
            await release.wait()
        return result

    running = None
    try:
        observed = await engine.execute(Request(operation_id='1' * 32, tool='gui_observe',
            arguments={'session_id': 'a' * 32, 'app': 'Editor'}), peer='owner')
        peer.call = delayed
        arguments = {'session_id': 'a' * 32,
                     'observation_id': observed.data['observation_id'], 'text': 'hello'}
        running = asyncio.create_task(engine.execute(Request(
            operation_id='2' * 32, tool='gui_type', arguments=arguments), peer='owner'))
        await asyncio.wait_for(focused.wait(), 1)
        rejected = Request(operation_id='3' * 32, tool='gui_type', arguments=arguments)
        busy = await engine.execute(rejected, peer='owner')
        assert busy.state == 'failed' and 'busy' in busy.error
        release.set()
        assert (await running).state == 'completed'
        count = len(peer.calls)
        assert (await engine.execute(rejected, peer='owner')) == busy
        assert len(peer.calls) == count
        recovered = await engine.execute(Request(operation_id='4' * 32, tool='operations_get',
            arguments={'operation_id': rejected.operation_id}), peer='owner')
        assert recovered.data['state'] == 'failed'
        assert [name for name, _ in peer.calls].count('type') == 1
    finally:
        release.set()
        if running is not None:
            await running
        await engine.close()


async def test_observe_focuses_requested_app_and_captures_frontmost():
    peer = Peer()
    gui = GUIMCP(peer)
    result = await gui.observe(GUIObserve(session_id='a' * 32, app='Editor'), owner='owner')
    assert peer.calls == [('app', {'action': 'focus', 'name': 'Editor'}),
                          ('see', {'app_target': 'frontmost'})]
    assert result['action_ready']


@pytest.mark.parametrize('application', ['Other', '', 'Editor\nApplication: Other'])
async def test_observation_of_different_frontmost_app_cannot_authorize_input(application):
    peer = Peer()
    original = peer.call

    async def switched(session_id, name, arguments, *, owner):
        result = await original(session_id, name, arguments, owner=owner)
        if name == 'see':
            result['content'][0]['text'] = (
                f'Snapshot ID: snapshot-1\nApplication: {application}\n  elem_1 - button')
        return result

    peer.call = switched
    gui = GUIMCP(peer)
    result = await gui.observe(GUIObserve(session_id='a' * 32, app='Editor'), owner='owner')
    assert result['action_ready'] is False
    assert result['reason'] == 'observed_application_mismatch'
    assert not gui.observations
