"""Platform-independent framing/timing contracts; native I/O is covered on Windows."""

from types import SimpleNamespace

import pytest

from anywhere_computer import owner_json_pipe as pipe


@pytest.mark.parametrize('value', [b'NaN', b'Infinity', b'-Infinity'])
@pytest.mark.parametrize('direction', ['encode', 'decode'])
def test_frames_reject_non_json_constants(monkeypatch, value, direction):
    payload = b'{"nested":{"value":' + value + b'}}'
    with pytest.raises(pipe.OwnerPipeProtocolError, match='strict UTF-8 JSON'):
        _validate_frame(monkeypatch, payload, direction)


@pytest.mark.parametrize('direction', ['encode', 'decode'])
def test_over_nested_json_has_a_protocol_error(monkeypatch, direction):
    payload = b'{"nested":' + b'[' * 20000 + b'0' + b']' * 20000 + b'}'
    with pytest.raises(pipe.OwnerPipeProtocolError, match='strict UTF-8 JSON'):
        _validate_frame(monkeypatch, payload, direction)


@pytest.mark.parametrize('direction', ['encode', 'decode'])
def test_frames_preserve_valid_unicode_and_finite_numbers(monkeypatch, direction):
    payload = '{"text":"日本語 NaN Infinity","value":-1.25e3,"optional":null}'.encode()
    assert _validate_frame(monkeypatch, payload, direction) == payload


def _validate_frame(monkeypatch, payload, direction):
    if direction == 'encode':
        return pipe._encode_frame(payload, pipe.DEFAULT_MAX_PAYLOAD_BYTES)[pipe._HEADER.size:]
    chunks = iter([pipe._HEADER.pack(len(payload)), payload])

    def read(handle, size, deadline, stopped):
        chunk = next(chunks)
        assert len(chunk) == size
        return chunk

    monkeypatch.setattr(pipe, '_WINDOWS', SimpleNamespace(read_exact=read), raising=False)
    return pipe._read_frame(10, pipe.DEFAULT_MAX_PAYLOAD_BYTES, 5)


def test_client_uses_one_deadline_for_connect_write_and_reply(tmp_path, monkeypatch):
    clock = [100.0]
    observed = []
    closed = []
    endpoint = pipe.OwnerPipeEndpoint(
        pipe_name=r'\\.\pipe\anywhere-owner-json-' + 'a' * 48,
        server_id='b' * 64, server_pid=123, server_creation_time=1.0,
        owner_sid='S-1-5-21-123', server_executable=str(tmp_path / 'python'),
    )

    def connect(name, timeout):
        observed.append(timeout)
        clock[0] += 3
        return 10

    def read(handle, maximum, timeout):
        observed.append(timeout)
        clock[0] += 2
        return b'{}'

    def write(handle, frame, timeout, thread):
        observed.append(timeout)
        clock[0] += 1

    monkeypatch.setattr(pipe, '_require_windows', lambda: None)
    monkeypatch.setattr(pipe, '_verify_server_identity', lambda *args: None)
    monkeypatch.setattr(pipe, '_verify_hello', lambda *args: None)
    monkeypatch.setattr(pipe, '_read_frame', read)
    monkeypatch.setattr(pipe.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(pipe, '_WINDOWS', SimpleNamespace(
        open_client=connect, write_all=write, close=closed.append,
        kernel=SimpleNamespace(OpenThread=lambda *args: 20),
    ), raising=False)
    assert pipe.request_owner_json_pipe(endpoint, b'{}', timeout=10) == b'{}'
    assert observed == [10, 7, 5, 4]
    assert closed == [10, 20]
