"""SSE framing and localhost stream tests; no Chat account or browser is used."""

import asyncio
import json

import httpx
import pytest

from anywhere_computer.subchat_sse import SSEDecoder, StreamLimitError


def decode(raw: bytes, **budgets):
    decoder = SSEDecoder(**budgets)
    events = decoder.feed(raw)
    events.extend(decoder.finish())
    return events, decoder


def test_every_split_of_utf8_bom_crlf_and_multiline_data():
    raw = ('\ufeffid: fixture\r\nevent: delta\r\ndata: 日本語🙂\r\n'
           'data: second line\r\n\r\n').encode()
    expected, _ = decode(raw)
    assert len(expected) == 1
    assert (expected[0].event, expected[0].last_event_id, expected[0].data) == (
        'delta', 'fixture', '日本語🙂\nsecond line')
    for split in range(len(raw) + 1):
        decoder = SSEDecoder()
        assert decoder.feed(raw[:split]) + decoder.feed(raw[split:]) + decoder.finish() == expected
    decoder = SSEDecoder()
    assert [event for byte in raw for event in decoder.feed(bytes([byte]))] == expected


@pytest.mark.parametrize('ending', ['\r', '\n', '\r\n'])
def test_line_endings_and_empty_data(ending):
    events, _ = decode(('data:x' + ending + ending + 'data' + ending + ending).encode())
    assert [event.data for event in events] == ['x', '']


def test_fields_whitespace_id_and_comments():
    events, _ = decode(b':comment\ndata:{"v":1}\n\n'
                       b'data: {"v":2}\n\n'
                       b'id: stable\nevent: custom\ndata:  leading trailing \n\n'
                       b'id: bad\x00id\ndata: last\n\n')
    assert [json.loads(event.data)['v'] for event in events[:2]] == [1, 2]
    assert (events[2].data, events[2].event, events[2].last_event_id) == (
        ' leading trailing ', 'custom', 'stable')
    assert (events[3].event, events[3].last_event_id) == ('message', 'stable')


def test_multiline_json_done_and_unterminated_eof():
    events, decoder = decode(b'data: {"v":\ndata: 1}\n\ndata: [DONE]\n\n'
                             b'data: unfinished')
    assert json.loads(events[0].data) == {'v': 1}
    assert events[1].data == '[DONE]'
    assert decoder.discarded_incomplete is True
    with pytest.raises(ValueError, match='closed'):
        decoder.feed(b'')


@pytest.mark.parametrize('raw', [b'data: x', b'data: x\n', b'event: delta\n'])
def test_eof_never_emits_partial_event(raw):
    events, decoder = decode(raw)
    assert events == []
    assert decoder.discarded_incomplete is True


def test_invalid_utf8_and_resource_limits():
    with pytest.raises(UnicodeDecodeError):
        decode(b'data:\xff\n\n')
    decoder = SSEDecoder()
    decoder.feed(b'data:\xe3')
    with pytest.raises(UnicodeDecodeError):
        decoder.finish()
    for raw, budget in [
        (b'data:0123456789', {'max_line_bytes': 8}),
        (b'data:12\ndata:34\n\n', {'max_event_bytes': 5}),
        (b':0123456789', {'max_total_bytes': 5}),
        (b'data:a\n\ndata:b\n\n', {'max_events': 1}),
    ]:
        with pytest.raises(StreamLimitError):
            decode(raw, **budget)
    for value in (0, -1, True, 1.5):
        with pytest.raises(ValueError, match='Positive integer'):
            SSEDecoder(max_events=value)


class LocalStream:
    def __init__(self, *, redirect=False):
        self.redirect = redirect
        self.release = asyncio.Event()
        self.first_sent = asyncio.Event()
        self.done = asyncio.Event()
        self.posts = 0
        self.errors: list[Exception] = []

    async def __aenter__(self):
        self.server = await asyncio.start_server(self.handle, '127.0.0.1', 0)
        self.url = f'http://127.0.0.1:{self.server.sockets[0].getsockname()[1]}'
        return self

    async def __aexit__(self, *args):
        self.release.set()
        self.server.close()
        await self.server.wait_closed()
        if self.posts:
            await asyncio.wait_for(self.done.wait(), 5)
        assert self.errors == []

    async def handle(self, reader, writer):
        try:
            headers = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 3)
            assert headers.startswith(b'POST ')
            self.posts += 1
            fields = dict(line.lower().split(b':', 1)
                          for line in headers.split(b'\r\n')[1:] if b':' in line)
            size = int(fields.get(b'content-length', b'0'))
            assert 0 < size <= 4096
            await reader.readexactly(size)
            if self.redirect:
                writer.write(('HTTP/1.1 307 Temporary Redirect\r\nLocation: '
                              f'{self.url}/redirected\r\nContent-Length: 0\r\n'
                              'Connection: close\r\n\r\n').encode())
            else:
                writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n'
                             b'Transfer-Encoding: chunked\r\nConnection: close\r\n\r\n')
                first = 'data:{"conversation_id":"fixture-chat","text":"日本語"}\r\n\r\n'.encode()
                for piece in (first[:5], first[5:31], first[31:]):
                    writer.write(f'{len(piece):x}\r\n'.encode() + piece + b'\r\n')
                await writer.drain()
                self.first_sent.set()
                await asyncio.wait_for(self.release.wait(), 5)
                last = b'data: {"tail":true}\n\ndata: [DONE]\n\n'
                writer.write(f'{len(last):x}\r\n'.encode() + last + b'\r\n0\r\n\r\n')
            await writer.drain()
        except Exception as error:
            self.errors.append(error)
        finally:
            writer.close()
            await writer.wait_closed()
            self.done.set()


@pytest.mark.parametrize('redirect', [False, True])
async def test_localhost_stream_keeps_one_post_and_does_not_follow_redirect(redirect):
    async with LocalStream(redirect=redirect) as fixture:
        async with httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(retries=0),
                                     follow_redirects=False, trust_env=False, timeout=5) as client:
            if redirect:
                response = await client.post(fixture.url + '/synthetic', json={'fixture': True})
                assert response.status_code == 307
                assert response.history == []
            else:
                first_received = asyncio.Event()

                async def receive():
                    decoder = SSEDecoder()
                    events = []
                    async with client.stream('POST', fixture.url + '/synthetic',
                                             json={'fixture': True}) as response:
                        assert response.status_code == 200
                        async for chunk in response.aiter_bytes():
                            for event in decoder.feed(chunk):
                                events.append(event)
                                first_received.set()
                    events.extend(decoder.finish())
                    return events

                running = asyncio.create_task(receive())
                try:
                    await asyncio.wait_for(first_received.wait(), 3)
                    assert fixture.release.is_set() is False
                    with pytest.raises(TimeoutError):
                        await asyncio.wait_for(asyncio.shield(running), .05)
                    assert running.done() is False
                    fixture.release.set()
                    events = await asyncio.wait_for(running, 3)
                    assert json.loads(events[0].data)['conversation_id'] == 'fixture-chat'
                    assert json.loads(events[1].data) == {'tail': True}
                    assert events[2].data == '[DONE]'
                finally:
                    fixture.release.set()
                    await asyncio.gather(running, return_exceptions=True)
        assert fixture.posts == 1
