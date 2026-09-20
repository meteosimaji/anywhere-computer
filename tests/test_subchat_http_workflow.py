"""Actual HTTP -> engine -> stdio subchat route; provider is deterministic."""
import asyncio
import sys
import uuid

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from test_http_mcp import HEADERS
from test_http_mcp import http_agent as http_agent

PROGRAM = '''
import asyncio, sys
from pathlib import Path
from anywhere_computer.state import Ledger
from anywhere_computer.subchat import Subchats, SubchatReceipt, SubchatAnswer
from anywhere_computer.subchat_state import SubchatSubmissions
from anywhere_computer.subchat_mcp import session
from anywhere_computer.mcp_server import serve_stdio
class Provider:
    async def prepare(self, submission): return ()
    async def send(self, submission):
        with (Path(sys.argv[1]) / 'sends').open('a') as output: output.write('sent\\n')
        return None
    async def find_submission(self, submission):
        return SubchatReceipt(conversation_id='fixture-chat', user_message_id='user',
                              prompt=submission.prompt)
    async def read_answer(self, submission):
        if not (Path(sys.argv[1]) / 'answer-ready').exists(): return None
        return SubchatAnswer(conversation_id='fixture-chat', user_message_id='user',
                              prompt=submission.prompt, answer_message_id='answer', text='42')
async def main():
    ledger = Ledger(Path(sys.argv[1]))
    try:
        service = Subchats(SubchatSubmissions(ledger.connection), Provider())
        await serve_stdio(session(service), sys.stdin.buffer, sys.stdout.buffer)
    finally: ledger.close()
asyncio.run(main())
'''


async def test_http_direct_mcp_subchat_receipt_and_process_restart(http_agent, tmp_path):
    _, _, _, port = http_agent
    operation = uuid.uuid4().hex
    sent_args = {'request_id': operation, 'prompt': '日本語\n42',
                 'model': 'observed model', 'effort': 'observed effort'}
    async with asyncio.timeout(40):
        for iteration in range(2):
            async with httpx.AsyncClient(headers=HEADERS, trust_env=False) as http:
                async with streamable_http_client(
                    f'http://127.0.0.1:{port}/mcp', http_client=http,
                ) as (reader, writer, _):
                    async with ClientSession(reader, writer) as client:
                        await client.initialize()

                        async def call(name, args):
                            reply = await client.call_tool(name, args)
                            assert not reply.isError, reply
                            assert reply.structuredContent['state'] == 'completed'
                            return reply.structuredContent['data']

                        opened = await call('mcp_session_open', {
                            'command': [sys.executable, '-u', '-c', PROGRAM, str(tmp_path)],
                            'cwd': str(tmp_path),
                        })
                        target = {'session_id': opened['session_id']}
                        try:
                            catalog = await call('mcp_tools', target)
                            assert 'subchat_wait' in {tool['name'] for tool in catalog['tools']}
                            sent = await call('mcp_call', {
                                **target, 'name': 'subchat_send', 'arguments': sent_args,
                            })
                            assert not sent['is_error']
                            inner = sent['structured_content']
                            assert inner['operation_id'] == operation
                            assert inner['data']['state'] == ('sending' if iteration == 0
                                                              else 'completed')
                            if iteration == 0:
                                invalid = await client.call_tool('mcp_call', {
                                    **target, 'name': 'subchat_wait',
                                    'arguments': {'operation_id': operation, 'wait_ms': 60_000},
                                })
                                assert invalid.isError
                                pending = await call('mcp_call', {
                                    **target, 'name': 'subchat_wait',
                                    'arguments': {'operation_id': operation, 'wait_ms': 100},
                                })
                                assert pending['structured_content']['data']['state'] == 'submitted'
                                status = await call('mcp_session_status', target)
                                assert status['state'] == 'open'
                                (tmp_path / 'answer-ready').touch()
                            done = await call('mcp_call', {
                                **target, 'name': 'subchat_wait',
                                'arguments': {'operation_id': operation, 'wait_ms': 1000},
                            })
                            assert not done['is_error']
                            assert done['structured_content']['data']['answer'] == '42'
                        finally:
                            closed = await call('mcp_session_close', target)
                            assert closed['cleanup_confirmed']
    # A new HTTP connection and a new child process must not send again.
    assert (tmp_path / 'sends').read_text().splitlines() == ['sent']
