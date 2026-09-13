"""Platform-independent timing contract; native I/O is covered on Windows."""

from types import SimpleNamespace

from anywhere_computer import owner_json_pipe as pipe


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
