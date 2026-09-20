"""Local JSON-lines subchat controller with one explicitly selected browser profile."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from typing import TYPE_CHECKING, Literal, TextIO

from pydantic import Field, TypeAdapter

from .models import Contract, OperationId
from .state import Ledger
from .subchat import (
    SubchatAccessError,
    SubchatBrowserClosed,
    SubchatInterrupted,
    SubchatOutcomeUnknown,
    Subchats,
)
from .subchat_content import SubchatResources
from .subchat_state import (
    SubchatAccountMismatch,
    SubchatHTTPSelection,
    SubchatList,
    SubchatSubmissions,
    SubchatWorkContext,
)

if TYPE_CHECKING:
    from playwright.async_api import APIRequestContext, BrowserContext, Playwright


class Command(Contract):
    action: Literal['send', 'recover', 'status', 'cancel']
    operation_id: str = Field(pattern=r'^[0-9a-f]{32}$')
    prompt: str | None = Field(default=None, min_length=1, max_length=100_000)
    model: str | None = Field(default=None, min_length=1, max_length=256)
    effort: str | None = Field(default=None, min_length=1, max_length=256)
    conversation_id: str | None = None
    work_context: SubchatWorkContext | None = None
    resources: SubchatResources | None = None
    http_selection: SubchatHTTPSelection | None = None


class CatalogCommand(Contract):
    action: Literal['catalog']


class ListCommand(SubchatList):
    action: Literal['list']


class QueueCommand(OperationId):
    action: Literal['queue']
    target_operation_id: str = Field(pattern=r'^[0-9a-f]{32}$')
    prompt: str = Field(min_length=1, max_length=100_000)


async def dispatch(service: Subchats,
                   command: Command | ListCommand | QueueCommand | CatalogCommand) -> str:
    if isinstance(command, CatalogCommand):
        observe = getattr(service.backend, 'http_catalog', None)
        if observe is None:
            raise ValueError('HTTP catalog is unavailable')
        return json.dumps(await observe(), ensure_ascii=False)
    if isinstance(command, ListCommand):
        return service.store.list(SubchatList(limit=command.limit, before=command.before),
                                  owner=None).model_dump_json()
    if isinstance(command, QueueCommand):
        return service.queue(command.operation_id, command.target_operation_id,
                             command.prompt, owner=None).model_dump_json()
    if command.action == 'send':
        if command.prompt is None or command.model is None or command.effort is None:
            raise ValueError('send requires prompt, model and effort')
        result = await service.send(command.operation_id, command.prompt, command.model,
                                    command.effort, owner=None,
                                    conversation_id=command.conversation_id,
                                    work_context=command.work_context, resources=command.resources,
                                    http_selection=command.http_selection)
    else:
        if any(value is not None for value in (command.prompt, command.model,
                                               command.effort, command.conversation_id,
                                               command.work_context, command.resources,
                                               command.http_selection)):
            raise ValueError('Recovery, status and cancellation accept only an operation identity')
        result = (await service.recover(command.operation_id, owner=None)
                  if command.action == 'recover'
                  else service.store.cancel(command.operation_id, owner=None)
                  if command.action == 'cancel'
                  else service.store.get(command.operation_id, owner=None))
    return result.model_dump_json()


async def process_lines(service: Subchats, source: TextIO, destination: TextIO) -> None:
    """Sequential request framing; errors preserve the durable operation identity."""
    while True:
        line = await asyncio.to_thread(source.readline)
        if not line:
            return
        command: Command | ListCommand | QueueCommand | CatalogCommand | None = None
        try:
            command = TypeAdapter(
                Command | ListCommand | QueueCommand | CatalogCommand).validate_json(line)
            output = await dispatch(service, command)
        except Exception as error:
            # Do not print provider errors or invalid input: both can contain secrets.
            output = json.dumps({
                'state': (error.code
                          if isinstance(error, SubchatAccessError | SubchatAccountMismatch)
                          else 'browser_closed' if isinstance(error, SubchatBrowserClosed)
                          else 'submission_unconfirmed'
                          if isinstance(error, SubchatOutcomeUnknown) else 'reply_interrupted'
                          if isinstance(error, SubchatInterrupted) else 'command_failed'),
                'operation_id': (command.operation_id
                                 if isinstance(command, Command | QueueCommand) else None),
                'error_type': type(error).__name__,
                'automatic_retry': False,
            })
        destination.write(output + '\n')
        destination.flush()


async def run(profile: Path, state: Path, *, mcp: bool = False, http_read: bool = False,
              minimized: bool = False) -> None:
    from .subchat_browser.backend import BrowserSubchatBackend

    ledger = Ledger(state)
    try:
        async with AsyncExitStack() as resources:
            driver: Playwright | None = None
            http_client: APIRequestContext | None = None

            async def runtime() -> Playwright:
                from playwright.async_api import async_playwright

                nonlocal driver
                if driver is None:
                    driver = await resources.enter_async_context(async_playwright())
                return driver

            async def open_http() -> APIRequestContext:
                nonlocal http_client
                if http_client is None:
                    http_client = await (await runtime()).request.new_context()
                    resources.push_async_callback(http_client.dispose)
                return http_client

            async def open_browser() -> BrowserContext:
                context = await (await runtime()).chromium.launch_persistent_context(
                    str(profile), channel='chrome', headless=False,
                    args=['--start-minimized'] if minimized else [])
                resources.push_async_callback(context.close)
                if minimized:
                    from .subchat_browser.catalog import minimize_window

                    page = context.pages[0] if context.pages else await context.new_page()
                    cdp = await context.new_cdp_session(page)
                    try:
                        window = await cdp.send('Browser.getWindowForTarget')
                        if not await minimize_window(cdp, window['windowId']):
                            raise ConnectionError('Browser minimization was not confirmed')
                    finally:
                        await cdp.detach()
                return context

            store = SubchatSubmissions(ledger.connection)

            def record_request(operation_id: str, message_id: str, account_id: str) -> None:
                store.observe_request(operation_id, message_id, owner=None,
                                      provider_account_id=account_id)

            backend = BrowserSubchatBackend(open_browser, http_read=http_read,
                http_request_factory=open_http if http_read else None,
                record_request=record_request if http_read else None)
            service = Subchats(store, backend)
            # Saved-state requests need no browser. Once needed, commands share
            # one dedicated context until EOF; no per-request restart or replay.
            if mcp:
                from .mcp_server import serve_stdio
                from .subchat_mcp import session

                server = session(service, observe_catalog=backend.catalog,
                                 observe_http_catalog=backend.http_catalog)
                try:
                    await serve_stdio(server, sys.stdin.buffer, sys.stdout.buffer)
                finally:
                    await server.close()
            else:
                await process_lines(service, sys.stdin, sys.stdout)
    finally:
        ledger.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--browser-profile', type=Path, required=True,
                        help='Dedicated logged-in Chrome profile, never your normal profile')
    parser.add_argument('--state-dir', type=Path, required=True,
                        help='Local subchat ledger directory')
    parser.add_argument("--mcp", action="store_true", help="Serve MCP over stdio")
    parser.add_argument('--http-read', action='store_true',
                        help='Read HTTP history; sends require an observed HTTP catalog selection')
    parser.add_argument('--minimized', action='store_true',
                        help='Verify the dedicated Chrome window is minimized before page work')
    args = parser.parse_args()
    asyncio.run(run(args.browser_profile.resolve(), args.state_dir.resolve(),
                    mcp=args.mcp, http_read=args.http_read, minimized=args.minimized))
