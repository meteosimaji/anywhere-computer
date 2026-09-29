"""Receive only known bounded media summaries; unrelated metadata is inert."""

import base64
import copy

import pytest
from test_plugin_image_results import image
from test_remote_media_transport import AUDIO, MEDIA

from anywhere_computer.mcp_media import restore_reply_media
from anywhere_computer.mcp_server import _reply_result
from anywhere_computer.models import Reply
from anywhere_computer.plugin_audio import audio_summary
from anywhere_computer.plugin_images import image_summary


def result_for(name='mcp_call'):
    reply = Reply(operation_id='a' * 32, state='completed', data={
        'content': copy.deepcopy(MEDIA), 'is_error': True,
        'structured_content': {'preview': image()},
    })
    if name in {'devices_call', 'recovered_device'}:
        reply.data = {'device_id': 'local', 'tool': 'mcp_call', 'result': reply.data}
    if name in {'operations_get', 'recovered_device'}:
        reply = Reply(operation_id='b' * 32, state='completed',
                      data=reply.model_dump(mode='json'))
    return _reply_result('operations_get' if name == 'recovered_device' else name, reply)


@pytest.mark.parametrize('name', ['mcp_call', 'devices_call', 'operations_get', 'recovered_device'])
def test_known_envelopes_restore_exact_content_without_mutating_packet(name):
    result = result_for(name)
    snapshot = copy.deepcopy(result)
    reply = restore_reply_media('operations_get' if name == 'recovered_device' else name, result)
    data = reply.data
    if name in {'operations_get', 'recovered_device'}:
        data = data['data']
    if name in {'devices_call', 'recovered_device'}:
        data = data['result']
    assert data['content'] == MEDIA
    assert data['is_error'] is True
    assert data['structured_content']['preview'] == image_summary(image())
    assert result == snapshot


@pytest.mark.parametrize('state', ['completed', 'failed', 'running', 'unknown'])
def test_arbitrary_tool_metadata_is_never_interpreted_as_media(state):
    structured = Reply(operation_id='c' * 32, state=state, data={
        'content': [image_summary(image())],
        'metadata': {'data': {'content': [audio_summary(AUDIO)]}},
    }).model_dump(mode='json')
    result = {'structuredContent': structured, 'content': [
        {'type': 'image', 'data': 'private-invalid-base64', 'mimeType': 'image/png'},
    ]}
    assert restore_reply_media('files_read', result).model_dump(mode='json') == structured


def test_summary_shaped_metadata_does_not_satisfy_missing_content_media():
    result = result_for()
    result['content'] = result['content'][:1]
    result['structuredContent']['data']['unrelated'] = image()
    with pytest.raises(ValueError, match='media'):
        restore_reply_media('mcp_call', result)


@pytest.mark.parametrize('change', [
    lambda summary: summary.update(bytes=summary['bytes'] + 1),
    lambda summary: summary.update(bytes=float(summary['bytes'])),
    lambda summary: summary.update(sha256='a' * 64),
    lambda summary: summary.update(mimeType='image/jpeg'),
    lambda summary: summary.update(extra='untrusted'),
    lambda summary: summary.pop('sha256'),
    lambda summary: summary.update(data=image()['data']),
])
def test_summary_requires_exact_fields_and_values(change):
    result = result_for()
    change(result['structuredContent']['data']['content'][0])
    with pytest.raises(ValueError, match='media'):
        restore_reply_media('mcp_call', result)


def test_repeated_media_consumes_one_wire_block_per_summary():
    reply = Reply(operation_id='d' * 32, state='completed', data={'content': [image(), image()]})
    result = _reply_result('mcp_call', reply)
    assert restore_reply_media('mcp_call', result).data['content'] == [image(), image()]
    result['content'].pop()
    with pytest.raises(ValueError, match='media'):
        restore_reply_media('mcp_call', result)


def test_extra_wire_media_is_rejected_instead_of_attached_to_metadata():
    result = result_for()
    result['content'].append(image())
    with pytest.raises(ValueError, match='media'):
        restore_reply_media('mcp_call', result)


@pytest.mark.parametrize('media_type,count', [('image', 5), ('audio', 2)])
def test_count_limits_are_enforced_before_restore(media_type, count):
    items = [image() if media_type == 'image' else AUDIO] * count
    reply = Reply(operation_id='e' * 32, state='completed', data={'content': items})
    result = _reply_result('mcp_call', reply)
    with pytest.raises(ValueError, match='media'):
        restore_reply_media('mcp_call', result)


@pytest.mark.parametrize('media_type', ['image', 'audio'])
def test_aggregate_byte_limits_use_existing_bounded_validators(monkeypatch, media_type):
    from anywhere_computer import mcp_media

    item = image() if media_type == 'image' else AUDIO
    size = len(base64.b64decode(item['data']))
    budget_name = 'IMAGE_LIMIT' if media_type == 'image' else 'AUDIO_LIMIT'
    monkeypatch.setattr(mcp_media, budget_name, size - 1)
    reply = Reply(operation_id='e' * 32, state='completed', data={'content': [item]})
    with pytest.raises(ValueError, match='media'):
        restore_reply_media('mcp_call', _reply_result('mcp_call', reply))


def test_wire_metadata_is_stripped_by_existing_media_validators():
    result = result_for()
    for item in result['content'][1:]:
        item['_meta'] = {'private': 'excluded'}
        item['annotations'] = {'priority': 1}
    assert restore_reply_media('mcp_call', result).data['content'] == MEDIA


def test_wire_media_order_does_not_change_structured_content_order():
    result = result_for()
    result['content'][1:] = reversed(result['content'][1:])
    assert restore_reply_media('mcp_call', result).data['content'] == MEDIA


def test_media_without_a_known_content_summary_is_rejected():
    result = result_for()
    del result['structuredContent']['data']['content']
    with pytest.raises(ValueError, match='media'):
        restore_reply_media('mcp_call', result)
