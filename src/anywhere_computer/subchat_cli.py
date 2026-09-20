"""Local JSON-lines subchat controller with one explicitly selected browser profile."""

import argparse
import asyncio
import json
from pathlib import Path
from typing import Literal

from pydantic import Field

from .models import Contract
from .state import Ledger
from .subchat import SubchatOutcomeUnknown, Subchats
from .subchat_state import SubchatSubmissions


class Command(Contract):
    action: Literal['send', 'recover', 'status']
    operation_id: str = Field(pattern=r'^[0-9a-f]{32}$')
    prompt: str | None = Field(default=None, min_length=1, max_length=100_000)
    model: str | None = Field(default=None, min_length=1, max_length=256)
    effort: str | None = Field(default=None, min_length=1, max_length=256)
    conversation_id: str | None = None


async def dispatch(service: Subchats, command: Command) -> str:
    if command.action == 'send':
        if command.prompt is None or command.model is None or command.effort is None:
            raise ValueError('send requires prompt, model and effort')
        result = await service.send(command.operation_id, command.prompt, command.model,
                                    command.effort, owner=None,
                                    conversation_id=command.conversation_id)
    else:
        if any(value is not None for value in (command.prompt, command.model,
                                               command.effort, command.conversation_id)):
            raise ValueError('Recovery and status accept only an operation identity')
        result = (await service.recover(command.operation_id, owner=None)
                  if command.action == 'recover'
                  else service.store.get(command.operation_id, owner=None))
    return result.model_dump_json()


async def run(profile: Path, state: Path) -> None:
    # Keep this dependency optional for all non-browser installations.
    from playwright.async_api import async_playwright

    from .subchat_browser.backend import BrowserSubchatBackend

    ledger = Ledger(state)
    try:
        async with async_playwright() as driver:
            context = await driver.chromium.launch_persistent_context(
                str(profile), channel='chrome', headless=False)
            try:
                service = Subchats(SubchatSubmissions(ledger.connection),
                                   BrowserSubchatBackend(context))
                # Multiple commands share one browser. EOF is explicit shutdown;
                # no per-request window closing and no automatic message retry.
                while True:
                    try:
                        line = await asyncio.to_thread(input)
                    except EOFError:
                        break
                    try:
                        command = Command.model_validate_json(line)
                        output = await dispatch(service, command)
                    except Exception as error:
                        # Provider errors can contain private page/account data.
                        output = json.dumps({
                            'state': ('submission_unconfirmed'
                                      if isinstance(error, SubchatOutcomeUnknown)
                                      else 'command_failed'),
                            'error_type': type(error).__name__,
                            'automatic_retry': False,
                        })
                    print(output, flush=True)
            finally:
                await context.close()
    finally:
        ledger.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--browser-profile', type=Path, required=True,
                        help='Dedicated logged-in Chrome profile, never your normal profile')
    parser.add_argument('--state-dir', type=Path, required=True,
                        help='Local subchat ledger directory')
    args = parser.parse_args()
    asyncio.run(run(args.browser_profile.resolve(), args.state_dir.resolve()))
