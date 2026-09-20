"""Observe one existing Chat tab without sending, focusing, launching or closing it.

Only an allowlisted metadata projection is retained. This is a development
probe, not a standalone authenticated Chat client or a completion oracle.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import Page, Request, Response, async_playwright

PATHS = frozenset({
    '/backend-api/f/conversation', '/backend-api/f/conversation/prepare',
    '/backend-api/stop_conversation', '/backend-api/sentinel/chat-requirements/prepare',
})
CHAT = re.compile(r'https://chatgpt\.com/(?:c/[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})?\Z')
MAX_EVENTS = 1000


class Observation:
    def __init__(self, page: Page) -> None:
        self.events: list[dict[str, object]] = []
        self.dropped = 0
        self.pending: dict[Request, int] = {}
        self.sequence = 0
        self.page = page
        self.listeners = (
            ('request', self.request), ('response', self.response),
            ('requestfinished', self.finished), ('requestfailed', self.failed),
        )
        for event, handler in self.listeners:
            page.on(event, handler)

    def record(self, event: dict[str, object]) -> None:
        if len(self.events) < MAX_EVENTS:
            self.events.append(event)
        else:
            self.dropped += 1

    def request(self, request: Request) -> None:
        url = urlsplit(request.url)
        if (url.scheme != 'https' or url.netloc != 'chatgpt.com'
                or url.path not in PATHS or request.method != 'POST'):
            return
        self.sequence += 1
        if len(self.pending) >= MAX_EVENTS:
            self.dropped += 1
            return
        self.pending[request] = self.sequence
        # Do not read headers, cookies, post_data or URL queries at all.
        self.record({'event': 'request', 'request': self.sequence, 'path': url.path})

    def response(self, response: Response) -> None:
        number = self.pending.get(response.request)
        if number is not None:
            content_type = response.headers.get('content-type', '').split(';', 1)[0].strip()
            self.record({'event': 'response', 'request': number, 'status': response.status,
                         'format': content_type if content_type in
                         {'text/event-stream', 'application/json'} else 'other'})

    def finished(self, request: Request) -> None:
        self.end(request, 'transport_finished')

    def failed(self, request: Request) -> None:
        # Error text can contain URLs or provider data; do not persist it.
        self.end(request, 'transport_failed')

    def end(self, request: Request, event: str) -> None:
        number = self.pending.pop(request, None)
        if number is not None:
            self.record({'event': event, 'request': number})

    def close(self) -> dict[str, object]:
        for event, handler in self.listeners:
            self.page.remove_listener(event, handler)
        pending = sorted(self.pending.values())
        self.pending.clear()
        return {'events': self.events, 'pending_requests': pending, 'dropped_events': self.dropped,
                'answer_completion_verified': False, 'browser_free_client_verified': False}


async def observe(endpoint: str, page_url: str, seconds: int) -> dict[str, object]:
    address = urlsplit(endpoint)
    if (address.scheme != 'http' or address.hostname not in {'127.0.0.1', '[::1]', '::1'}
            or address.username or address.password or address.query or address.fragment
            or address.path not in {'', '/'}):
        raise ValueError('Use an existing loopback HTTP CDP endpoint')
    if CHAT.fullmatch(page_url) is None or not 1 <= seconds <= 300:
        raise ValueError('Use an exact Chat URL and observation duration from 1 to 300 seconds')
    async with async_playwright() as driver:
        browser = await driver.chromium.connect_over_cdp(endpoint)
        matches = [page for context in browser.contexts for page in context.pages
                   if page.url == page_url]
        if len(matches) != 1:
            raise ValueError('Exactly one existing matching tab is required')
        observation = Observation(matches[0])
        try:
            await asyncio.sleep(seconds)
        finally:
            result = observation.close()
        # Leaving Playwright disconnects this client; never call browser.close().
        return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cdp-endpoint', required=True)
    parser.add_argument('--page-url', required=True)
    parser.add_argument('--seconds', type=int, default=30)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    # Reserve a new private output before attaching; do not overwrite artifacts.
    descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
        result = asyncio.run(observe(args.cdp_endpoint, args.page_url, args.seconds))
        json.dump(result, output, indent=2)
        output.write('\n')


if __name__ == '__main__':
    main()
