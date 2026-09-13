import time

import pytest

from anywhere_computer.gui_mcp import GUIMCP, GUIClick, GUIKey, GUIObserve, GUIType


class Peer:
    def __init__(self):
        self.calls = []
        self.entries = {'a' * 32: object()}
        self.metadata = None

    def status(self, session_id, *, owner):
        if owner != 'owner':
            raise ValueError('Not owned')

    async def call(self, session_id, name, arguments, *, owner):
        self.status(session_id, owner=owner)
        self.calls.append((name, arguments))
        text = 'Snapshot ID: snapshot-1\n  elem_1 - button' if name == 'see' else 'done'
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
    assert len(peer.calls) == 1
    gui.observations[sid].created = time.monotonic() - 61
    with pytest.raises(ValueError, match='stale'):
        await gui.act(action, owner='owner')
    assert len(peer.calls) == 1
    seen = await gui.observe(GUIObserve(session_id=sid, app='Calculator'), owner='owner')
    action.observation_id = seen['observation_id']
    await gui.act(action, owner='owner')
    assert peer.calls[-1] == ('click', {'on': 'elem_1', 'snapshot': 'snapshot-1'})
    with pytest.raises(ValueError, match='missing'):
        await gui.act(action, owner='owner')
    assert len(peer.calls) == 3


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
    assert len(peer.calls) == 1
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
