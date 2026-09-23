"""Local subchat controller with explicit browser-assisted or HTTP-only transport."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from typing import TYPE_CHECKING, Literal, TextIO

import httpx
from pydantic import Field, TypeAdapter, ValidationError

from .models import Contract, OperationId
from .state import Ledger
from .subchat import (
    SubchatAccessError,
    SubchatBrowserClosed,
    SubchatInterrupted,
    SubchatOutcomeUnknown,
    Subchats,
    SubchatUnsupported,
)
from .subchat_content import SubchatResources
from .subchat_delete import DeleteRequest, SubchatDeletionUnknown, delete_saved
from .subchat_state import (
    SubchatAccountMismatch,
    SubchatHTTPSelection,
    SubchatList,
    SubchatOperationNotFound,
    SubchatSelectionError,
    SubchatSubmissions,
    SubchatWorkContext,
)

if TYPE_CHECKING:
    from playwright.async_api import APIRequestContext, BrowserContext, Playwright

    from .subchat_http_generation import ObservedHTTPGeneration
    from .subchat_http_session import ObservedHTTPSession


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


class CapabilitiesCommand(Contract):
    action: Literal['capabilities']


class CatalogCommand(Contract):
    action: Literal['catalog']


class ListCommand(SubchatList):
    action: Literal['list']


class QueueCommand(OperationId):
    action: Literal['queue']
    target_operation_id: str = Field(pattern=r'^[0-9a-f]{32}$')
    prompt: str = Field(min_length=1, max_length=100_000)


class DeleteCommand(DeleteRequest):
    action: Literal['delete']


async def dispatch(service: Subchats,
                   command: Command | ListCommand | QueueCommand | CatalogCommand
                   | CapabilitiesCommand | DeleteCommand) -> str:
    if isinstance(command, CapabilitiesCommand):
        capabilities = getattr(service.backend, 'capabilities', None)
        if capabilities is None:
            raise ValueError('This adapter does not report capabilities')
        return json.dumps(capabilities(), ensure_ascii=False)
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
    if isinstance(command, DeleteCommand):
        return (await delete_saved(service, DeleteRequest.model_validate(command.model_dump(
            exclude={'action'})), owner=None)).model_dump_json()
    if command.action == 'send':
        if command.prompt is None or command.model is None or command.effort is None:
            if command.prompt is None:
                raise ValueError('send requires prompt')
            if command.model is None:
                raise SubchatSelectionError('model', 'required')
            if command.effort is None:
                raise SubchatSelectionError('effort', 'required')
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
    data = result.model_dump(mode='json')
    progress = service.store.http_progress(result.operation_id, owner=None)
    if progress is not None:
        data['http_progress'] = progress
    return json.dumps(data, ensure_ascii=False, separators=(',', ':'))


async def process_lines(service: Subchats, source: TextIO, destination: TextIO) -> None:
    """Sequential request framing; errors preserve the durable operation identity."""
    while True:
        line = await asyncio.to_thread(source.readline)
        if not line:
            return
        command: (Command | ListCommand | QueueCommand | CatalogCommand
                  | CapabilitiesCommand | DeleteCommand | None) = None
        try:
            command = TypeAdapter(
                Command | ListCommand | QueueCommand | CatalogCommand
                | CapabilitiesCommand | DeleteCommand).validate_json(line)
            output = await dispatch(service, command)
        except Exception as error:
            # Do not print provider errors or invalid input: both can contain secrets.
            output = json.dumps({
                'state': ('invalid_parameter' if isinstance(error, ValidationError) else error.code
                          if isinstance(error, SubchatAccessError | SubchatAccountMismatch
                                        | SubchatOperationNotFound
                                        | SubchatUnsupported | SubchatSelectionError)
                          else 'browser_closed' if isinstance(error, SubchatBrowserClosed)
                          else 'submission_unconfirmed'
                          if isinstance(error, SubchatOutcomeUnknown) else 'reply_interrupted'
                          if isinstance(error, SubchatInterrupted) else 'delete_unknown'
                          if isinstance(error, SubchatDeletionUnknown) else 'command_failed'),
                'operation_id': (command.operation_id
                                 if isinstance(command, Command | QueueCommand | DeleteCommand)
                                 else None),
                'error_type': type(error).__name__,
                'automatic_retry': False,
                **({'field': error.field, 'reason': error.reason,
                    'dispatched': False, 'corrected_request_requires_new_operation_id': True}
                   if isinstance(error, SubchatSelectionError) else {}),
                **({'invalid_params': [
                    {'path': [str(part) for part in item['loc']], 'code': item['type']}
                    for item in error.errors(include_input=False, include_context=False)],
                    'dispatched': False}
                   if isinstance(error, ValidationError) else {}),
                **({'dispatched': False} if isinstance(error, SubchatUnsupported)
                   and error.code == 'http_generation_unavailable' else {}),
                **({'next_action': 'Check the operation ID and selected ledger; use list '
                                   'to inspect saved operations.'}
                   if isinstance(error, SubchatOperationNotFound) else {}),
            })
        destination.write(output + '\n')
        destination.flush()


async def run(profile: Path | None, state: Path, *, mcp: bool = False, http_read: bool = False,
              minimized: bool = False, http_only: bool = False,
              http_session: ObservedHTTPSession | None = None,
              http_generation: ObservedHTTPGeneration | None = None,
              chrome_login_profile: Path | None = None,
              chrome_generation_stdin: bool = False,
              expected_account_id: str | None = None,
              read_only_mcp: bool = False) -> None:
    if read_only_mcp and (not mcp or not http_only or http_generation is not None
                          or chrome_generation_stdin):
        raise ValueError('Read-only MCP requires HTTP-only mode without generation')
    if http_only and (profile is not None or http_read or minimized):
        raise ValueError('HTTP-only mode cannot use browser options')
    if not http_only and (profile is None or http_session is not None):
        raise ValueError('Browser mode requires a profile and cannot import an HTTP session')
    if http_generation is not None and (not http_only or http_session is None):
        raise ValueError('HTTP generation requires an explicit browser-free session')
    if chrome_login_profile is not None and (not http_only or http_session is not None):
        raise ValueError('Chrome login requires HTTP-only mode without a supplied session')
    if expected_account_id is not None and chrome_login_profile is None:
        raise ValueError('Expected account requires Chrome login')

    ledger = Ledger(state)
    try:
        async with AsyncExitStack() as resources:
            driver: Playwright | None = None
            http_client: APIRequestContext | None = None
            standalone_http_client: httpx.AsyncClient | None = None
            http_init_lock = asyncio.Lock()

            async def runtime() -> Playwright:
                from playwright.async_api import async_playwright

                nonlocal driver
                if driver is None:
                    driver = await resources.enter_async_context(async_playwright())
                return driver

            async def open_http() -> APIRequestContext:
                nonlocal http_client
                async with http_init_lock:
                    if http_client is None:
                        http_client = await (await runtime()).request.new_context()
                        resources.push_async_callback(http_client.dispose)
                    return http_client

            async def open_standalone_http() -> httpx.AsyncClient:
                nonlocal standalone_http_client
                async with http_init_lock:
                    if standalone_http_client is None:
                        standalone_http_client = await resources.enter_async_context(
                            httpx.AsyncClient(
                                trust_env=False,
                                follow_redirects=False,
                                transport=httpx.AsyncHTTPTransport(retries=0),
                            ))
                    return standalone_http_client

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

            if chrome_login_profile is not None:
                from .subchat_chrome_login import chrome_http_session

                chrome_context = await (await runtime()).chromium.launch_persistent_context(
                    str(chrome_login_profile), channel='chrome', headless=True)
                try:
                    http_session = await chrome_http_session(
                        chrome_context, await open_standalone_http(),
                        expected_account_id=expected_account_id)
                finally:
                    # Authentication is now held in memory by HTTPX. Do not leave a
                    # Chrome process open for the lifetime of the MCP controller.
                    await chrome_context.close()
                if chrome_generation_stdin:
                    from .subchat_http_generation import read_http_generation_handoff

                    http_generation = read_http_generation_handoff(
                        sys.stdin.buffer,
                        authorization=http_session.authorization.get_secret_value(),
                        account_id=http_session.account_id,
                        cookie=(http_session.cookie.get_secret_value()
                                if http_session.cookie is not None else None))

            store = SubchatSubmissions(ledger.connection)

            def record_request(operation_id: str, message_id: str, account_id: str) -> None:
                store.observe_request(operation_id, message_id, owner=None,
                                      provider_account_id=account_id)

            def record_conversation(operation_id: str, message_id: str,
                                    conversation_id: str, account_id: str) -> None:
                store.observe_conversation(operation_id, message_id, conversation_id, owner=None,
                                           provider_account_id=account_id)

            def record_rejection(operation_id: str, message_id: str,
                                 status: int, account_id: str) -> None:
                store.observe_rejection(operation_id, message_id, status, owner=None,
                                        provider_account_id=account_id)

            backend: BrowserSubchatBackend | HTTPOnlySubchatBackend
            if http_only:
                from .subchat_http import HTTPOnlySubchatBackend

                backend = HTTPOnlySubchatBackend(
                    open_standalone_http, http_session, generation=http_generation,
                    store=store,
                    chrome_login=chrome_login_profile is not None)
            else:
                from .subchat_browser.backend import BrowserSubchatBackend

                backend = BrowserSubchatBackend(open_browser, http_read=http_read,
                    http_request_factory=open_http if http_read else None,
                    record_request=record_request if http_read else None,
                    record_conversation=record_conversation if http_read else None,
                    record_rejection=record_rejection if http_read else None)
            service = Subchats(store, backend)
            # Saved-state requests need no browser. Once needed, commands share
            # one dedicated context until EOF; no per-request restart or replay.
            if mcp:
                from .mcp_server import serve_stdio
                from .subchat_mcp import session

                instructions = None
                if http_only:
                    instructions = (
                        'Ordinary Chat recovery and explicit deletion over HTTPX. A configured '
                        'Chrome login profile is read headlessly at startup only. No browser '
                        'fallback, '
                        'independent login, credential refresh or generation is implemented. '
                        'Use subchat_catalog source=http, saved status/list and recover/wait '
                        'with the original operation ID. UI catalog is unsupported. '
                        'Send and queue dispatch return http_generation_unavailable. '
                        'Never resend an uncertain submission or supply credentials in tools. '
                        'Recovery requires known conversation/input identity and a session '
                        'obtained at startup from Chrome login or an explicit handoff. Missing or '
                        'expired authorization requires operator action, not a retry loop. '
                        'A pending observation is not proof of Thinking. Interruption is not '
                        'a completed answer. Queued work is never sent by this adapter. '
                        'subchat_download_file retrieves one exact final-answer sandbox link '
                        'as bounded base64 bytes; it does not upload to another Chat or Library. '
                        'Deletion checks the saved conversation and bound account; an unknown '
                        'delete outcome is never replayed automatically.')
                    if http_generation is not None:
                        instructions = (
                            'Ordinary Chat over HTTPX with an explicit in-memory generation '
                            'handoff. Chrome login may initialize the session; no automatic '
                            'credential refresh or Chrome fallback. Send '
                            'requires an exact HTTP catalog selection. Queued follow-ups use '
                            'the saved final assistant parent and check current_node before '
                            'generation. A pending or unknown result is never resent. HTTP '
                            'status and SSE are not final-answer proof; recover the original '
                            'operation through HTTP history. Handoff headers may expire and '
                            'must be supplied again by the operator in a new process. '
                            'subchat_download_file retrieves one exact final-answer sandbox '
                            'link as bounded base64 bytes; it does not upload to another Chat '
                            'or Library. '
                            'Deletion checks the saved conversation and bound account; an '
                            'unknown delete outcome is never replayed automatically.')
                if read_only_mcp:
                    instructions = (
                        'This Plugin Subchat session observes saved submissions and Chat HTTP '
                        'history. Its dedicated Chrome profile is read headlessly at startup '
                        'for login; HTTPX performs later reads. No generation or remote '
                        'mutation is exposed here. subchat_capabilities reports '
                        'generation_transport=unavailable. Use subchat_catalog source=http, '
                        'subchat_list, subchat_status, subchat_recover and subchat_wait. '
                        'subchat_download_file retrieves one exact saved final-answer sandbox '
                        'link as bounded base64 bytes without writing a local file. It does '
                        'not upload to another Chat or Library. '
                        'Recover only the original operation ID; an unconfirmed or pending '
                        'state never permits resending. Login failure requires operator action.')
                server = session(service, observe_catalog=backend.catalog,
                                 observe_http_catalog=backend.http_catalog,
                                 instructions=instructions, serialize_recovery=not http_only,
                                 read_only=read_only_mcp)
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
    parser.add_argument('--browser-profile', type=Path,
                        help='Dedicated logged-in Chrome profile, never your normal profile')
    parser.add_argument('--state-dir', type=Path, required=True,
                        help='Local subchat ledger directory')
    parser.add_argument("--mcp", action="store_true", help="Serve MCP over stdio")
    parser.add_argument('--http-read', action='store_true',
                        help='Read HTTP history; sends require an observed HTTP catalog selection')
    parser.add_argument('--minimized', action='store_true',
                        help='Verify the dedicated Chrome window is minimized before page work')
    parser.add_argument('--http-only', action='store_true',
                        help='HTTPX transport; sends require an explicit generation handoff')
    parser.add_argument('--chrome-login-profile', type=Path,
                        help='Use a logged-in dedicated Chrome profile headlessly to GET an '
                             'HTTP session at startup; no login UI or automatic renewal')
    parser.add_argument('--expected-account-id',
                        help='Pin the selected Chat account ID before sending from Chrome login')
    parser.add_argument('--http-session-stdin', action='store_true',
                        help='Consume one bounded observed-session JSON line before the protocol; '
                             'HTTP-only mode only. No login, cookies or protection tokens.')
    parser.add_argument('--http-generation-stdin', action='store_true',
                        help='Consume a second in-memory line of observed generation headers '
                             'and body templates. Requires --http-only --http-session-stdin.')
    args = parser.parse_args()
    if args.http_only:
        if args.browser_profile is not None or args.http_read or args.minimized:
            parser.error('--http-only cannot be combined with browser options')
        if args.chrome_login_profile is not None and args.http_session_stdin:
            parser.error('Choose Chrome login or an explicit HTTP session')
    elif args.browser_profile is None or args.http_session_stdin or args.http_generation_stdin:
        parser.error('Browser mode requires --browser-profile; session handoff needs --http-only')
    if args.chrome_login_profile is not None and not args.http_only:
        parser.error('--chrome-login-profile requires --http-only')
    if args.expected_account_id is not None and args.chrome_login_profile is None:
        parser.error('--expected-account-id requires --chrome-login-profile')
    if (args.chrome_login_profile is not None and args.http_generation_stdin
            and args.expected_account_id is None):
        parser.error('Chrome-login generation requires --expected-account-id')
    if args.http_generation_stdin and not (args.http_session_stdin or args.chrome_login_profile):
        parser.error('--http-generation-stdin requires a session source')
    observed_session = None
    if args.http_session_stdin:
        from .subchat_http_session import read_http_session

        try:
            observed_session = read_http_session(sys.stdin.buffer)
        except ValueError:
            print(json.dumps({'state': 'invalid_http_session', 'automatic_retry': False}),
                  file=sys.stderr)
            raise SystemExit(2) from None
    observed_generation = None
    if args.http_generation_stdin and observed_session is not None:
        from .subchat_http_generation import read_http_generation_handoff

        assert observed_session is not None
        try:
            observed_generation = read_http_generation_handoff(
                sys.stdin.buffer,
                authorization=observed_session.authorization.get_secret_value(),
                account_id=observed_session.account_id)
        except ValueError:
            print(json.dumps({'state': 'invalid_http_generation_handoff',
                              'automatic_retry': False}), file=sys.stderr)
            raise SystemExit(2) from None
    asyncio.run(run(args.browser_profile.resolve() if args.browser_profile is not None else None,
                    args.state_dir.resolve(), mcp=args.mcp, http_read=args.http_read,
                    minimized=args.minimized, http_only=args.http_only,
                    http_session=observed_session, http_generation=observed_generation,
                    chrome_login_profile=(args.chrome_login_profile.resolve()
                                          if args.chrome_login_profile is not None else None),
                    chrome_generation_stdin=bool(args.chrome_login_profile
                                                 and args.http_generation_stdin),
                    expected_account_id=args.expected_account_id))
