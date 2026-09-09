import asyncio
import shlex
import sys

import pytest

from anywhere_computer.models import SessionInput, StartSession
from anywhere_computer.sessions import Sessions


async def start_script(tmp_path, source):
    script = tmp_path / 'interactive.py'
    script.write_text(source)
    quote = (lambda value: f'"{value}"') if sys.platform == 'win32' else shlex.quote
    sessions = Sessions()
    result = await sessions.start(StartSession(
        command=f'{quote(sys.executable)} -u {quote(str(script))}', cwd=str(tmp_path),
    ))
    return sessions, result['session_id']


async def test_prompt_split_across_chunks_and_serialized_inputs(tmp_path):
    sessions, identity = await start_script(tmp_path, '''import sys, time
for line in sys.stdin:
    print(line.strip(), flush=True)
    sys.stdout.write("REA"); sys.stdout.flush()
    time.sleep(0.08)
    sys.stdout.write("DY> "); sys.stdout.flush()
''')
    try:
        first, second = await asyncio.gather(*(
            sessions.send(SessionInput(
                session_id=identity, text=value + '\n', wait_ms=3000,
                wait_for_prompt='READY> ',
            )) for value in ('first', 'second')
        ))
        for page, value, other in ((first, 'first', 'second'), (second, 'second', 'first')):
            assert page['wait_reason'] == 'prompt' and page['prompt_matched']
            assert value in page['text'] and other not in page['text']
            assert page['text'].endswith('READY> ')
            assert page['bytes_sent'] == len(value) + 1
        assert first['next_cursor'] == second['start_cursor']
    finally:
        await sessions.close()


@pytest.mark.parametrize('mode', ['timeout', 'exited', 'output_limit', 'output'])
async def test_wait_reasons_and_bounded_output(tmp_path, mode):
    source = 'import time\ninput()\n'
    source += {
        'timeout': 'time.sleep(10)',
        'exited': 'print("finished")',
        'output_limit': 'print("x" * 1000, flush=True)\ntime.sleep(10)',
        'output': 'print("response", flush=True)\ntime.sleep(10)',
    }[mode]
    sessions, identity = await start_script(tmp_path, source)
    try:
        result = await sessions.send(SessionInput(
            session_id=identity, text='go\n', wait_ms=100 if mode == 'timeout' else 3000,
            wait_for_prompt=None if mode == 'output' else 'missing>', output_limit=100,
        ))
        assert result['wait_reason'] == mode
        assert not result['prompt_matched']
        assert len(result['text'].encode()) <= 100
        assert result['state'] == ('exited' if mode == 'exited' else 'running')
        assert result['next_cursor'] >= result['start_cursor']
    finally:
        await sessions.close()


async def test_evicted_response_is_not_reported_as_success(tmp_path, monkeypatch):
    monkeypatch.setattr('anywhere_computer.sessions.OUTPUT_CAP', 64)
    sessions, identity = await start_script(tmp_path, '''import time
input()
print("x" * 1000 + "READY>", flush=True)
time.sleep(10)
''')
    try:
        result = await sessions.send(SessionInput(
            session_id=identity, text='go\n', wait_ms=3000, wait_for_prompt='READY>',
        ))
        assert result['wait_reason'] == 'output_dropped'
        assert result['dropped_bytes'] > 0
    finally:
        await sessions.close()
