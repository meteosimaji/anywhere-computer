import os
from pathlib import Path

import pytest

from anywhere_computer.codex_context import (
    codex_request,
    list_codex_threads,
    read_codex_thread,
)


@pytest.fixture
def fake_codex(tmp_path):
    if os.name == "nt":
        pytest.skip("Native executable fixture uses a POSIX shebang")
    script = tmp_path / "fake-codex"
    script.write_text(
        """#!/usr/bin/env python3
import json, sys
for line in sys.stdin:
    packet = json.loads(line)
    method = packet.get('method')
    if 'id' not in packet:
        continue
    if method == 'initialize':
        if sys.argv[1:] != ['app-server', '--listen', 'stdio://']:
            print(json.dumps({'jsonrpc': '2.0', 'id': packet['id'],
                              'error': {'message': 'bad argv'}}), flush=True)
            continue
        if packet['params'].get('capabilities', {}).get('experimentalApi') is not True:
            print(json.dumps({'jsonrpc': '2.0', 'id': packet['id'],
                              'error': {'message': 'bad capabilities'}}), flush=True)
            continue
        result = {'ok': True}
    elif method == 'thread/list':
        result = {'data': [
            {'id': 't1', 'title': 'A title', 'preview': 'secret preview',
             'first_user_message': 'secret message', 'cwd': '/private',
             'path': '/private/rollout'},
            {'id': 't2', 'name': '', 'tool_output': 'secret'},
        ], 'nextCursor': 'next'}
    elif method == 'thread/turns/list':
        if packet['params'].get('threadId') == 'wrong':
            result = {'threadId': 'other', 'data': []}
        elif packet['params'].get('threadId') == 'bytes':
            result = {'threadId': 'bytes', 'data': [{'items': [
                {'id': 'u-bytes', 'type': 'userMessage', 'text': 'é' * 65535},
            ]}]}
        else:
            result = {'threadId': packet['params']['threadId'], 'data': [{
                'items': [
                    {'id': 'u1', 'type': 'userMessage', 'content': [
                        {'type': 'input_text', 'text': 'hello'}]},
                    {'id': 'a1', 'type': 'agentMessage', 'text': 'world'},
                    {'id': 'a-analysis', 'type': 'agentMessage', 'phase': 'analysis',
                     'text': 'secret analysis'},
                    {'id': 'x', 'type': 'reasoning', 'text': 'secret reasoning'},
                    {'id': 'y', 'type': 'toolCall', 'arguments': 'secret args'},
                ]}], 'nextCursor': 'turn-next'}
    elif method == 'skills/list':
        result = {'skills': [{'name': 'computer-work'}]}
    else:
        print(json.dumps({'jsonrpc': '2.0', 'id': packet['id'],
                          'error': {'message': 'forbidden'}}), flush=True)
        continue
    print(json.dumps({'jsonrpc': '2.0', 'id': packet['id'], 'result': result}), flush=True)
""",
        encoding="utf-8",
    )
    script.chmod(0o700)
    return script


async def test_transport_allowlist_and_initialize(fake_codex):
    result = await codex_request("skills/list", {}, executable=fake_codex)
    assert result["skills"] == [{"name": "computer-work"}]
    with pytest.raises(ValueError):
        await codex_request("thread/read", {}, executable=fake_codex)  # type: ignore[arg-type]


async def test_thread_list_bounds_and_metadata_sanitization(fake_codex):
    result = await list_codex_threads(2, executable=fake_codex)
    assert result["next_cursor"] == "next"
    assert result["threads"] == [
        {"id": "t1", "title": "A title"},
        {"id": "t2", "title": "Untitled"},
    ]
    with pytest.raises(ValueError):
        await list_codex_threads(31, executable=fake_codex)
    with pytest.raises(ValueError, match="more threads"):
        await list_codex_threads(1, executable=fake_codex)


async def test_thread_read_filters_and_checks_exact_id(fake_codex):
    result = await read_codex_thread("t1", executable=fake_codex)
    assert result["thread_id"] == "t1"
    assert result["messages"] == [
        {"id": "u1", "role": "user", "text": "hello"},
        {"id": "a1", "role": "assistant", "text": "world"},
    ]
    assert result["truncated"] is False
    assert result["next_cursor"] == "turn-next"
    with pytest.raises(ValueError, match="different thread"):
        await read_codex_thread("wrong", executable=fake_codex)
    with pytest.raises(ValueError):
        await read_codex_thread("t1", 11, executable=fake_codex)
    bounded = await read_codex_thread("bytes", executable=fake_codex)
    text = bounded["messages"][0]["text"]
    assert len(text.encode("utf-8")) <= 64 * 1024
    assert bounded["truncated"] is True


def test_explicit_executable_must_not_be_relative_or_cwd(tmp_path):
    with pytest.raises(ValueError):
        # The helper is intentionally tested without starting a subprocess.
        from anywhere_computer.codex_context import _executable
        _executable(Path("codex"))
    old = Path.cwd()
    try:
        os.chdir(tmp_path)
        local = tmp_path / "codex"
        local.write_text("#!/bin/sh\n")
        local.chmod(0o700)
        from anywhere_computer.codex_context import _executable
        with pytest.raises(ValueError):
            _executable(local)
    finally:
        os.chdir(old)


async def test_multiple_unicode_messages_have_aggregate_byte_bound(monkeypatch):
    from anywhere_computer import codex_context

    async def reply(*args, **kwargs):
        return {"data": [{"items": [
            {"type": "userMessage", "text": "あ" * 20000},
            {"type": "agentMessage", "text": "い" * 20000},
            {"type": "agentMessage", "text": "must not appear"},
        ]}]}

    monkeypatch.setattr(codex_context, "codex_request", reply)
    result = await read_codex_thread("selected", limit=1)
    assert result["truncated"] is True
    assert len(result["messages"]) == 2
    assert sum(len(item["text"].encode()) for item in result["messages"]) <= 65536


def test_pinned_codex_survives_minimal_path_and_fails_closed(tmp_path, monkeypatch):
    from anywhere_computer.codex_context import _executable

    executable = tmp_path / "app" / "codex"
    executable.parent.mkdir()
    executable.write_bytes(b"fixture")
    monkeypatch.setenv("PATH", "/nonexistent")
    monkeypatch.setenv("ANYWHERE_CODEX_EXECUTABLE", str(executable))
    assert _executable(None) == executable.resolve()
    executable.unlink()
    with pytest.raises(ValueError, match="regular file"):
        _executable(None)
    monkeypatch.setenv("ANYWHERE_CODEX_EXECUTABLE", "relative-codex")
    with pytest.raises(ValueError, match="absolute path"):
        _executable(None)
