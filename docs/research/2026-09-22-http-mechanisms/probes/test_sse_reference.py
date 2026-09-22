"""SSE framing and localhost transport acceptance; no real Chat or credentials."""
from __future__ import annotations

import asyncio
import json
import socket
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from sse_reference import SSEDecoder, StreamLimitError

METRICS = {'split_positions_checked': 0, 'localhost_posts': 0, 'external_connects': 0,
           'process_launch_attempts': 0, 'stream_first_event_before_tail': False,
           'stream_survived_caller_wait': False, 'redirect_followed': False}


def decode(raw: bytes, **kwargs):
    parser = SSEDecoder(**kwargs)
    events = parser.feed(raw)
    events.extend(parser.finish())
    return events, parser


class FramingTests(unittest.TestCase):
    def test_every_split_of_utf8_bom_and_crlf(self):
        raw = ('\ufeffid: fixture\r\nevent: delta\r\ndata: 日本語🙂\r\n'
               'data: two lines\r\n\r\n').encode()
        expected, _ = decode(raw)
        self.assertEqual(expected[0].data, '日本語🙂\ntwo lines')
        for index in range(len(raw) + 1):
            parser = SSEDecoder()
            events = parser.feed(raw[:index]) + parser.feed(raw[index:]) + parser.finish()
            self.assertEqual(events, expected, index)
            METRICS['split_positions_checked'] += 1

    def test_bytewise_chunks(self):
        raw = 'data: 日本語🙂\n\n'.encode()
        parser = SSEDecoder()
        result = []
        for byte in raw:
            result.extend(parser.feed(bytes([byte])))
        result.extend(parser.finish())
        self.assertEqual(result[0].data, '日本語🙂')

    def test_all_standard_line_endings(self):
        for ending in ('\r', '\n', '\r\n'):
            with self.subTest(ending=repr(ending)):
                result, _ = decode(('data:x' + ending + ending).encode())
                self.assertEqual(result[0].data, 'x')

    def test_optional_one_space_and_no_space(self):
        output, _ = decode(b'data:{"v":1}\n\ndata: {"v":2}\n\n')
        self.assertEqual([json.loads(item.data)['v'] for item in output], [1, 2])

    def test_do_not_strip_user_whitespace(self):
        output, _ = decode(b'data:  leading trailing \n\n')
        self.assertEqual(output[0].data, ' leading trailing ')

    def test_multiline_json(self):
        output, _ = decode(b'data: {"v":\ndata: 1}\n\n')
        self.assertEqual(json.loads(output[0].data), {'v': 1})

    def test_comments_and_empty_data(self):
        output, _ = decode(b':comment\nunknown:ignored\ndata\n\n')
        self.assertEqual(output[0].data, '')

    def test_event_type_reset_and_id_persistence(self):
        output, _ = decode(b'id: one\nevent: custom\ndata:a\n\ndata:b\n\n')
        self.assertEqual([(x.event, x.last_event_id) for x in output],
                         [('custom', 'one'), ('message', 'one')])

    def test_nul_id_rejected_without_changing_previous(self):
        output, _ = decode(b'id: valid\ndata:a\n\nid: bad\x00id\ndata:b\n\n')
        self.assertEqual(output[1].last_event_id, 'valid')

    def test_unterminated_tail_discarded(self):
        for raw in [b'data: {"v":1}', b'data: {"v":1}\n']:
            output, parser = decode(raw)
            self.assertEqual(output, [])
            self.assertTrue(parser.discarded_incomplete)

    def test_done_is_data_not_completion(self):
        output, _ = decode(b'data: [DONE]\n\n')
        self.assertEqual(output[0].data, '[DONE]')
        self.assertFalse(hasattr(output[0], 'completed'))

    def test_invalid_utf8_is_explicit_local_rejection(self):
        with self.assertRaises(UnicodeDecodeError):
            decode(b'data:\xff\n\n')
        parser = SSEDecoder()
        parser.feed(b'data:\xe3')
        with self.assertRaises(UnicodeDecodeError):
            parser.finish()

    def test_line_budget(self):
        with self.assertRaises(StreamLimitError):
            decode(b'data:0123456789', max_line_bytes=8)

    def test_event_budget_across_lines(self):
        with self.assertRaises(StreamLimitError):
            decode(b'data:12\ndata:34\n\n', max_event_bytes=5)

    def test_total_budget_before_decode(self):
        with self.assertRaises(StreamLimitError):
            decode(b':0123456789', max_total_bytes=5)

    def test_event_count_budget(self):
        with self.assertRaises(StreamLimitError):
            decode(b'data:a\n\ndata:b\n\n', max_events=1)

    def test_closed_after_finish_and_error(self):
        parser = SSEDecoder()
        parser.finish()
        with self.assertRaises(ValueError):
            parser.feed(b'')
        parser = SSEDecoder(max_line_bytes=1)
        with self.assertRaises(StreamLimitError):
            parser.feed(b'xx')
        with self.assertRaises(ValueError):
            parser.feed(b'')

    def test_invalid_budgets(self):
        for value in (0, -1, True, 1.5):
            with self.assertRaises(ValueError):
                SSEDecoder(max_events=value)


class LocalFixture:
    def __init__(self, redirect=False):
        self.release = asyncio.Event()
        self.first_sent = asyncio.Event()
        self.done = asyncio.Event()
        self.posts = 0
        self.errors = []
        self.redirect = redirect
    async def __aenter__(self):
        self.server = await asyncio.start_server(self.handle, '127.0.0.1', 0)
        self.url = 'http://127.0.0.1:' + str(self.server.sockets[0].getsockname()[1])
        return self
    async def handle(self, reader, writer):
        try:
            headers = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 3)
            self.posts += headers.startswith(b'POST ')
            METRICS['localhost_posts'] += headers.startswith(b'POST ')
            fields = dict(line.lower().split(b':', 1) for line in headers.split(b'\r\n')[1:] if b':' in line)
            size = int(fields.get(b'content-length', b'0'))
            assert size <= 4096
            await reader.readexactly(size)
            if self.redirect:
                writer.write(('HTTP/1.1 307 Temporary Redirect\r\nLocation: ' + self.url +
                              '/redirect-target\r\nContent-Length: 0\r\nConnection: close\r\n\r\n').encode())
                await writer.drain()
                return
            writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n'
                         b'Transfer-Encoding: chunked\r\nConnection: close\r\n\r\n')
            first = 'data:{"conversation_id":"fixture-chat","text":"日本語"}\r\n\r\n'.encode()
            for piece in (first[:5], first[5:31], first[31:]):
                writer.write(f'{len(piece):x}\r\n'.encode() + piece + b'\r\n')
            await writer.drain()
            self.first_sent.set()
            await asyncio.wait_for(self.release.wait(), 5)
            last = b'data: {"fixture_end":true}\n\ndata: [DONE]\n\n'
            writer.write(f'{len(last):x}\r\n'.encode() + last + b'\r\n0\r\n\r\n')
            await writer.drain()
        except Exception as error:
            self.errors.append(type(error).__name__)
        finally:
            writer.close()
            await writer.wait_closed()
            self.done.set()
    async def __aexit__(self, *args):
        self.release.set()
        self.server.close()
        await self.server.wait_closed()
        if self.posts:
            await asyncio.wait_for(self.done.wait(), 5)


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_http_first_event_and_short_wait(self):
        async with LocalFixture() as fixture:
            first_received = asyncio.Event()
            async with httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(retries=0),
                    follow_redirects=False, trust_env=False, timeout=5) as client:
                async def worker():
                    parser = SSEDecoder()
                    values = []
                    async with client.stream('POST', fixture.url + '/synthetic', json={'fixture': True}) as response:
                        self.assertEqual(response.status_code, 200)
                        async for chunk in response.aiter_bytes():
                            for event in parser.feed(chunk):
                                values.append(event.data)
                                if not first_received.is_set():
                                    METRICS['stream_first_event_before_tail'] = not fixture.release.is_set()
                                    first_received.set()
                    values.extend(event.data for event in parser.finish())
                    return values
                running = asyncio.create_task(worker())
                try:
                    await asyncio.wait_for(first_received.wait(), 3)
                    with self.assertRaises(TimeoutError):
                        await asyncio.wait_for(asyncio.shield(running), .05)
                    self.assertFalse(running.done())
                    METRICS['stream_survived_caller_wait'] = True
                    fixture.release.set()
                    values = await asyncio.wait_for(running, 3)
                    self.assertEqual(json.loads(values[0])['conversation_id'], 'fixture-chat')
                    self.assertEqual(json.loads(values[1]), {'fixture_end': True})
                    self.assertEqual(values[2], '[DONE]')
                    self.assertEqual(fixture.posts, 1)
                    self.assertEqual(fixture.errors, [])
                finally:
                    fixture.release.set()
                    await asyncio.gather(running, return_exceptions=True)

    async def test_redirect_does_not_replay_post(self):
        async with LocalFixture(redirect=True) as fixture:
            async with httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(retries=0),
                    follow_redirects=False, trust_env=False, timeout=5) as client:
                response = await client.post(fixture.url + '/synthetic', json={'fixture': True})
                self.assertEqual(response.status_code, 307)
                self.assertEqual(response.history, [])
            self.assertEqual(fixture.posts, 1)
            self.assertEqual(fixture.errors, [])

    async def test_async_semaphore_cancellation_preserves_capacity(self):
        sem = asyncio.Semaphore(1)
        await sem.acquire()
        task = asyncio.create_task(sem.acquire())
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        sem.release()
        await asyncio.wait_for(sem.acquire(), 1)
        sem.release()


def main():
    original_connect = socket.socket.connect
    def loopback_only(sock, address):
        if isinstance(address, tuple) and address[0] not in ('127.0.0.1', '::1'):
            METRICS['external_connects'] += 1
            raise AssertionError('External connection prohibited')
        return original_connect(sock, address)
    def no_process(*args, **kwargs):
        METRICS['process_launch_attempts'] += 1
        raise AssertionError('No subprocess or browser in transport fixture')
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    with patch.object(socket.socket, 'connect', loopback_only), patch.object(subprocess, 'Popen', no_process):
        result = unittest.TextTestRunner(verbosity=2).run(suite)
    summary = {'scope': 'reference framing plus localhost HTTP only; not provider send acceptance',
               'tests_run': result.testsRun, 'failures': len(result.failures), 'errors': len(result.errors),
               'live_chat_requests': 0, 'metrics': METRICS,
               'client_versions': {'httpx': httpx.__version__}}
    with Path(__file__).with_name('reference-test-result.json').open('x') as output:
        json.dump(summary, output, indent=2)
        output.write('\n')
    print(json.dumps(summary, indent=2))
    raise SystemExit(0 if result.wasSuccessful() else 1)


if __name__ == '__main__':
    main()
