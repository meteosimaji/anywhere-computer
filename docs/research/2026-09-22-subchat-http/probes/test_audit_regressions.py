"""Audit regressions: synthetic providers only; no browser or real Chat invocation."""
import asyncio
import json
import sys
from pathlib import Path

import pytest
from test_gui_mcp import Peer
from test_subchat_delivery import Provider
from test_subchat_queue_watch import watched

from anywhere_computer.direct_mcp_sessions import DirectMCPSessions
from anywhere_computer.gui_mcp import GUIMCP, GUIObserve, GUIType
from anywhere_computer.models import Request
from anywhere_computer.state import Ledger
from anywhere_computer.subchat import Subchats
from anywhere_computer.subchat_mcp import session
from anywhere_computer.subchat_state import SubchatSubmissions


async def test_disarmed_watch_releases_finished_pending_recovery(watched):
    service, provider, server, parent, child, watch = watched
    entered, release = asyncio.Event(), asyncio.Event()
    original = provider.read_answer

    async def delayed(submission):
        entered.set()
        await release.wait()
        return await original(submission)

    provider.read_answer = delayed
    await watch()
    await asyncio.wait_for(entered.wait(), 2)
    recovery = server.recoveries[child]
    try:
        assert (await watch(False)).data['state'] == 'disabled'
        assert not recovery.done()
        release.set()
        await asyncio.wait_for(asyncio.shield(recovery), 2)
        await asyncio.sleep(0)
        print(json.dumps({'case': 'disarmed_watch', 'state': service.store.get(child, owner=None).state,
                          'recovery_done': recovery.done(), 'retained': child in server.recoveries,
                          'sends': len(provider.sends)}))
        assert provider.sends == [parent]
        assert child not in server.recoveries
    finally:
        release.set()


async def test_eight_late_pending_reads_do_not_block_ninth_operation(tmp_path):
    class SlowPending(Provider):
        async def read_answer(self, submission):
            self.entered.set()
            await self.release.wait()
            return None

    ledger = Ledger(tmp_path / 'ledger')
    provider = SlowPending()
    service = Subchats(SubchatSubmissions(ledger.connection), provider)
    server = session(service, serialize_recovery=False)
    try:
        for index in range(8):
            op = f'{index + 1:032x}'
            provider.user_message_id = f'user-{index}'
            provider.entered, provider.release = asyncio.Event(), asyncio.Event()
            await service.send(op, f'fixture-{index}', 'model', 'effort', owner=None)
            waiter = asyncio.create_task(server.execute(Request(operation_id=f'{index + 100:032x}',
                tool='subchat_wait', arguments={'operation_id': op, 'wait_ms': 100})))
            await asyncio.wait_for(provider.entered.wait(), 2)
            assert (await waiter).data['state'] == 'submitted'
            recovery = server.recoveries[op]
            provider.release.set()
            await asyncio.wait_for(asyncio.shield(recovery), 2)
            await asyncio.sleep(0)
        retained = len(server.recoveries)
        all_done = all(task.done() for task in server.recoveries.values())
        provider.user_message_id = 'user-ninth'
        ninth = 'f' * 32
        await service.send(ninth, 'fixture-ninth', 'model', 'effort', owner=None)
        result = await server.execute(Request(operation_id='e' * 32, tool='subchat_recover',
                                              arguments={'operation_id': ninth}))
        print(json.dumps({'case': 'late_pending_reads', 'retained': retained, 'all_done': all_done,
                          'ninth_state': result.state, 'ninth_data': result.data}))
        assert result.state == 'completed'
    finally:
        await server.close()
        ledger.close()


async def test_capture_only_provider_cannot_authorize_snapshot_input():
    class CaptureOnly(Peer):
        async def tools(self, session_id, *, owner, **kwargs):
            self.status(session_id, owner=owner)
            return {'tools': [
                {'name': 'see', 'inputSchema': {'type': 'object', 'properties': {
                    'app_target': {'type': 'string'}, 'window_id': {'type': 'integer'}}}},
                {'name': 'type', 'inputSchema': {'type': 'object', 'properties': {
                    'text': {'type': 'string'}}}},
            ]}

    peer = CaptureOnly()
    gui = GUIMCP(peer)
    sid = 'a' * 32
    refused = False
    try:
        observed = await gui.observe(GUIObserve(session_id=sid, app='Editor', window_id=42),
                                     owner='owner')
        if observed.get('action_ready'):
            await gui.act(GUIType(session_id=sid, observation_id=observed['observation_id'],
                                  text='synthetic input only'), owner='owner')
    except ValueError:
        refused = True
    inputs = [name for name, _ in peer.calls if name == 'type']
    print(json.dumps({'case': 'capture_only_contract', 'refused': refused, 'input_calls': inputs}))
    assert not inputs


async def test_same_app_wrong_window_cannot_authorize_input():
    class WrongWindow(Peer):
        async def call(self, session_id, name, arguments, *, owner):
            result = await super().call(session_id, name, arguments, owner=owner)
            if name == 'see':
                result['content'][0]['text'] += '\nWindow ID: 43'
                result['_meta'] = {'window_id': 43}
            return result

    peer = WrongWindow()
    gui = GUIMCP(peer)
    result = await gui.observe(GUIObserve(session_id='a' * 32, app='Editor', window_id=42),
                               owner='owner')
    print(json.dumps({'case': 'wrong_window', 'reported_window_id': result.get('window_id'),
                      'action_ready': result.get('action_ready')}))
    assert result['action_ready'] is False


PROGRAM = r'''
import asyncio, sys
from pathlib import Path
from anywhere_computer.state import Ledger
from anywhere_computer.subchat import Subchats, SubchatReceipt
from anywhere_computer.subchat_state import SubchatSubmissions
from anywhere_computer.subchat_mcp import session
from anywhere_computer.mcp_server import serve_stdio
root = Path(sys.argv[1])
class Provider:
    def queue_watch_ready(self, submission): return True
    async def prepare(self, submission): return ()
    async def send(self, submission):
        with (root / 'sends').open('a') as output: output.write(submission.operation_id + '\n')
        return SubchatReceipt(conversation_id='fixture-chat', user_message_id=submission.operation_id,
                              prompt=submission.prompt)
    async def find_submission(self, submission): return None
    async def read_answer(self, submission):
        (root / 'watch_read').write_text('observed')
        return None
async def main():
    ledger = Ledger(root / 'state')
    controller = session(Subchats(SubchatSubmissions(ledger.connection), Provider()))
    try: await serve_stdio(controller, sys.stdin.buffer, sys.stdout.buffer)
    finally:
        await controller.close()
        ledger.close()
asyncio.run(main())
'''


async def test_active_queue_watch_keeps_outer_session_alive(tmp_path):
    now = [0.0]
    sessions = DirectMCPSessions(clock=lambda: now[0])
    parent, child = '7' * 32, '8' * 32
    try:
        opened = await sessions.open([sys.executable, '-c', PROGRAM, str(tmp_path)],
                                     Path.cwd(), owner='audit')
        sid = opened['session_id']
        for name, arguments in [
            ('subchat_send', {'request_id': parent, 'prompt': 'fixture-parent',
                              'model': 'fixture', 'effort': 'fixture'}),
            ('subchat_message', {'request_id': child, 'mode': 'queue',
                                 'target_operation_id': parent, 'prompt': 'fixture-child'}),
            ('subchat_queue_watch', {'operation_id': child}),
        ]:
            result = await sessions.call(sid, name, arguments, owner='audit')
            assert not result.get('isError'), result
        async with asyncio.timeout(3):
            while not (tmp_path / 'watch_read').exists():
                await asyncio.sleep(.01)
        now[0] = 301.0
        await sessions.expire_idle()
        status = sessions.status(sid, owner='audit')
        ledger = Ledger(tmp_path / 'state')
        try:
            state = SubchatSubmissions(ledger.connection).get(child, owner=None).state
        finally:
            ledger.close()
        print(json.dumps({'case': 'active_watch_idle', 'outer_state': status['state'],
                          'cleanup_confirmed': status['cleanup_confirmed'],
                          'child_state': state, 'sends': len((tmp_path / 'sends').read_text().splitlines()),
                          'clock': 'injected monotonic +301s; not elapsed wall time'}))
        assert status['state'] == 'open'
    finally:
        await sessions.close()
