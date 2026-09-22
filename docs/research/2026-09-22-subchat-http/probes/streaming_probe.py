"""ローカルSSEの応答時期を比較する。外部通信・実Chat・ブラウザー起動は行わない。"""
from __future__ import annotations

import asyncio
import hashlib
import importlib.metadata
import json
from pathlib import Path
from typing import Any

import httpx
from playwright.async_api import BrowserType, async_playwright

FIRST = b'data: {"conversation_id":"11111111-2222-4333-8444-555555555555"}\n\n'
LAST = b'data: {"fixture_final":"done"}\n\ndata: [DONE]\n\n'
BROWSER_CALLS: list[str] = []


async def forbidden_browser(*args: Any, **kwargs: Any) -> None:
    BROWSER_CALLS.append('forbidden')
    raise AssertionError('この試験はブラウザーを起動しない')


class SSEFixture:
    def __init__(self) -> None:
        self.sent_first = asyncio.Event()
        self.release_tail = asyncio.Event()
        self.completed = asyncio.Event()
        self.posts = 0
        self.received_bodies: list[bytes] = []
        self.errors: list[str] = []
        self.tasks: set[asyncio.Task[Any]] = set()
        self.server: asyncio.Server | None = None
        self.url = ''

    async def __aenter__(self) -> SSEFixture:
        self.server = await asyncio.start_server(self.handle, '127.0.0.1', 0)
        port = self.server.sockets[0].getsockname()[1]
        self.url = f'http://127.0.0.1:{port}/synthetic-sse'
        return self

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        self.tasks.add(task)
        try:
            headers = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 10)
            assert headers.startswith(b'POST /synthetic-sse HTTP/1.1\r\n')
            fields = {k.strip().lower(): v.strip() for k, v in
                      (line.split(b':', 1) for line in headers.split(b'\r\n')[1:] if b':' in line)}
            length = int(fields.get(b'content-length', b'0'))
            assert 0 <= length <= 1024
            self.received_bodies.append(await reader.readexactly(length))
            self.posts += 1
            writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n'
                         b'Transfer-Encoding: chunked\r\nConnection: close\r\n\r\n')
            writer.write(f'{len(FIRST):x}\r\n'.encode() + FIRST + b'\r\n')
            await writer.drain()
            self.sent_first.set()
            await asyncio.wait_for(self.release_tail.wait(), 15)
            writer.write(f'{len(LAST):x}\r\n'.encode() + LAST + b'\r\n0\r\n\r\n')
            await writer.drain()
        except Exception as error:
            self.errors.append(type(error).__name__)
        finally:
            writer.close()
            await writer.wait_closed()
            self.completed.set()
            self.tasks.discard(task)

    async def __aexit__(self, *args: Any) -> None:
        self.release_tail.set()
        assert self.server is not None
        self.server.close()
        await self.server.wait_closed()
        if self.tasks:
            await asyncio.wait_for(asyncio.gather(*self.tasks), 10)


async def playwright_case() -> dict[str, Any]:
    async with SSEFixture() as fixture, async_playwright() as playwright:
        client = await playwright.request.new_context()
        request = asyncio.create_task(client.post(
            fixture.url, data=b'{"fixture":true}',
            headers={'Content-Type': 'application/json'}, timeout=10_000,
            max_redirects=0, max_retries=0))
        try:
            await asyncio.wait_for(fixture.sent_first.wait(), 5)
            try:
                await asyncio.wait_for(asyncio.shield(request), 1.0)
                returned_early = True
            except TimeoutError:
                returned_early = False
            result = {'client': 'Playwright APIRequestContext.post',
                      'server_sent_first_event': True,
                      'response_returned_before_tail_release': returned_early,
                      'tail_was_withheld': not fixture.release_tail.is_set(),
                      'observation_window_seconds': 1.0}
            fixture.release_tail.set()
            response = await asyncio.wait_for(request, 5)
            body = await response.body()
            result.update({'status': response.status, 'complete_body_matches': body == FIRST + LAST,
                           'post_count': fixture.posts, 'server_errors': fixture.errors})
            await response.dispose()
            assert result['complete_body_matches'] and fixture.posts == 1 and not fixture.errors
            return result
        finally:
            fixture.release_tail.set()
            if not request.done():
                await asyncio.wait_for(request, 5)
            await client.dispose()


async def httpx_case() -> dict[str, Any]:
    async with SSEFixture() as fixture:
        first_seen = asyncio.Event()
        pieces: list[bytes] = []
        first_before_tail: list[bool] = []
        async with httpx.AsyncClient(
                transport=httpx.AsyncHTTPTransport(retries=0), follow_redirects=False,
                trust_env=False, timeout=httpx.Timeout(10.0, connect=5.0)) as client:
            async def consume() -> None:
                async with client.stream('POST', fixture.url, content=b'{"fixture":true}',
                                         headers={'Content-Type': 'application/json'}) as response:
                    assert response.status_code == 200
                    async for chunk in response.aiter_bytes():
                        pieces.append(chunk)
                        if FIRST in b''.join(pieces) and not first_seen.is_set():
                            first_before_tail.append(not fixture.release_tail.is_set())
                            first_seen.set()
            request = asyncio.create_task(consume())
            try:
                await asyncio.wait_for(fixture.sent_first.wait(), 5)
                await asyncio.wait_for(first_seen.wait(), 5)
                result = {'client': 'HTTPX AsyncClient.stream',
                          'first_event_read_before_tail_release': first_before_tail == [True],
                          'stream_task_still_running': not request.done()}
                fixture.release_tail.set()
                await asyncio.wait_for(request, 5)
                result.update({'complete_body_matches': b''.join(pieces) == FIRST + LAST,
                               'post_count': fixture.posts, 'server_errors': fixture.errors})
                assert result['first_event_read_before_tail_release']
                assert result['complete_body_matches'] and fixture.posts == 1 and not fixture.errors
                return result
            finally:
                fixture.release_tail.set()
                if not request.done():
                    await asyncio.wait_for(request, 5)


async def main() -> None:
    original_launch = BrowserType.launch
    original_persistent = BrowserType.launch_persistent_context
    BrowserType.launch = forbidden_browser
    BrowserType.launch_persistent_context = forbidden_browser
    try:
        result = {'scope': 'localhost HTTP/1.1 chunked SSE fixture only; not ChatGPT acceptance',
                  'versions': {name: importlib.metadata.version(name) for name in ('playwright', 'httpx')},
                  'playwright': await playwright_case(), 'httpx': await httpx_case(),
                  'browser_launch_calls': len(BROWSER_CALLS), 'real_chat_requests': 0}
        assert not BROWSER_CALLS
        target = Path(__file__).with_name('streaming-result.json')
        with target.open('x', encoding='utf-8') as output:
            json.dump(result, output, ensure_ascii=False, indent=2)
            output.write('\n')
        print(json.dumps(result, ensure_ascii=False, indent=2))
        print('RESULT_SHA256=' + hashlib.sha256(target.read_bytes()).hexdigest())
    finally:
        BrowserType.launch = original_launch
        BrowserType.launch_persistent_context = original_persistent


if __name__ == '__main__':
    asyncio.run(main())
