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
from .subchat import SubchatOutcomeUnknown, Subchats
from .subchat_state import SubchatList, SubchatSubmissions, SubchatWorkContext

if TYPE_CHECKING:
    from playwright.async_api import BrowserContext, Playwright


class Command(Contract):
    action: Literal['send', 'recover', 'status', 'cancel']
    operation_id: str = Field(pattern=r'^[0-9a-f]{32}$')
    prompt: str | None = Field(default=None, min_length=1, max_length=100_000)
    model: str | None = Field(default=None, min_length=1, max_length=256)
    effort: str | None = Field(default=None, min_length=1, max_length=256)
    conversation_id: str | None = None
    work_context: SubchatWorkContext | None = None


class ListCommand(SubchatList):
    action: Literal['list']


class QueueCommand(OperationId):
    action: Literal['queue']
    target_operation_id: str = Field(pattern=r'^[0-9a-f]{32}$')
    prompt: str = Field(min_length=1, max_length=100_000)


async def dispatch(service: Subchats, command: Command | ListCommand | QueueCommand) -> str:
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
                                    work_context=command.work_context)
    else:
        if any(value is not None for value in (command.prompt, command.model,
                                               command.effort, command.conversation_id,
                                               command.work_context)):
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
        command: Command | ListCommand | QueueCommand | None = None
        try:
            command = TypeAdapter(Command | ListCommand | QueueCommand).validate_json(line)
            output = await dispatch(service, command)
        except Exception as error:
            # Do not print provider errors or invalid input: both can contain secrets.
            output = json.dumps({
                'state': ('submission_unconfirmed'
                          if isinstance(error, SubchatOutcomeUnknown) else 'command_failed'),
                'operation_id': (command.operation_id
                                 if isinstance(command, Command | QueueCommand) else None),
                'error_type': type(error).__name__,
                'automatic_retry': False,
            })
        destination.write(output + '\n')
        destination.flush()


async def run(profile: Path, state: Path, *, mcp: bool = False, http_read: bool = False) -> None:
    from .subchat_browser.backend import BrowserSubchatBackend

    ledger = Ledger(state)
    try:
        async with AsyncExitStack() as resources:
            driver: Playwright | None = None

            async def open_browser() -> BrowserContext:
                from playwright.async_api import async_playwright

                nonlocal driver
                if driver is None:
                    driver = await resources.enter_async_context(async_playwright())
                context = await driver.chromium.launch_persistent_context(
                    str(profile), channel='chrome', headless=False)
                resources.push_async_callback(context.close)
                return context

            backend = BrowserSubchatBackend(open_browser, http_read=http_read)
            service = Subchats(SubchatSubmissions(ledger.connection), backend)
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
                        help='Read saved answers through observed browser HTTP history')
    args = parser.parse_args()
    asyncio.run(run(args.browser_profile.resolve(), args.state_dir.resolve(),
                    mcp=args.mcp, http_read=args.http_read))
