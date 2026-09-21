"""Real engine/ledger with synthetic audio_helper output; never records a device."""
import asyncio
from pathlib import Path

import pytest

from anywhere_computer import audio_status
from anywhere_computer.engine import Engine
from anywhere_computer.models import Request


@pytest.fixture
def audio_helper(monkeypatch):
    calls = []
    async def inspect():
        return {'state': 'available', 'screen_capture_allowed': True,
                'capture_started': False, 'capture_tool_available': True}
    async def capture(command, *, timeout):
        calls.append(command)
        assert command[1] == 'system' and len(command) == 4
        seconds = int(command[2])
        assert timeout == seconds + 20
        directory = Path(command[3])
        directory.mkdir(mode=0o700)
        (directory / 'system.caf').write_bytes(b'caff synthetic capture artifact')
        await asyncio.sleep(0)
        return {'state': 'captured', 'source': 'system', 'microphone_device_id': 'none',
                'output_directory': str(directory), 'requested_seconds': seconds,
                'frames': {'system': seconds * 48000}, 'measurements': {'system': {
                    'sample_rate': 48000, 'duration_seconds': seconds, 'peak': 0, 'rms': 0}}}
    monkeypatch.setattr(audio_status, 'inspect_audio', inspect)
    monkeypatch.setattr('anywhere_computer.engine.inspect_audio', inspect)
    monkeypatch.setattr(audio_status, 'verified_audio_helper', lambda: Path('/fixture/helper'))
    monkeypatch.setattr(audio_status, '_query', capture)
    return calls


def request(directory, **overrides):
    return Request(operation_id='a' * 32, tool='audio_capture', arguments={
        'source': 'system', 'seconds': 1, 'output_directory': str(directory), **overrides})


async def test_capture_once_and_recover_after_engine_restart(tmp_path, audio_helper):
    command = request(tmp_path / 'recording')
    engine = Engine(tmp_path / 'state')
    try:
        first, duplicate = await asyncio.gather(engine.execute(command), engine.execute(command))
        assert first == duplicate and first.state == 'completed'
        assert first.data['state'] == 'captured'
        assert first.data['speaker_output_verified'] is False
        assert first.data['microphone_used'] is False
        assert first.data['rms'] == 0  # Silence is not a failed recording or playback proof.
        assert len(audio_helper) == 1
    finally:
        await engine.close()
    engine = Engine(tmp_path / 'state')
    try:
        assert await engine.execute(command) == first
        assert len(audio_helper) == 1
    finally:
        await engine.close()


@pytest.mark.parametrize('arguments', [{'source': 'microphone'}, {'seconds': 31},
                                      {'seconds': True}, {'output_directory': 'relative'}])
async def test_invalid_capture_never_dispatches(tmp_path, audio_helper, arguments):
    engine = Engine(tmp_path / 'state')
    try:
        result = await engine.execute(request(tmp_path / 'recording', **arguments))
        assert result.state == 'failed'
        assert audio_helper == [] and not (tmp_path / 'recording').exists()
    finally:
        await engine.close()


async def test_partial_failure_is_unknown_and_not_replayed(tmp_path, audio_helper, monkeypatch):
    async def interrupted(command, **kwargs):
        audio_helper.append(command)
        Path(command[3]).mkdir()
        raise TimeoutError('private audio_helper failure')
    monkeypatch.setattr(audio_status, '_query', interrupted)
    engine = Engine(tmp_path / 'state')
    try:
        command = request(tmp_path / 'partial')
        result = await engine.execute(command)
        assert result.state == 'unknown' and result.data['partial_files_possible']
        assert 'private audio_helper failure' not in result.model_dump_json()
        assert await engine.execute(command) == result
        assert len(audio_helper) == 1
    finally:
        await engine.close()


@pytest.mark.parametrize('case', ['existing_directory', 'permission_missing'])
async def test_preflight_preserves_files_and_never_records(
        tmp_path, audio_helper, monkeypatch, case):
    output = tmp_path / 'recording'
    if case == 'existing_directory':
        output.mkdir()
        (output / 'keep').write_text('preserve')
    else:
        async def denied():
            return {'state': 'available', 'screen_capture_allowed': False}
        monkeypatch.setattr(audio_status, 'inspect_audio', denied)
    engine = Engine(tmp_path / 'state')
    try:
        result = await engine.execute(request(output))
        assert result.state == 'failed' and audio_helper == []
        if case == 'existing_directory':
            assert (output / 'keep').read_text() == 'preserve'
        else:
            assert not output.exists()
    finally:
        await engine.close()


@pytest.mark.parametrize('case', ['wrong_source', 'inconsistent_duration', 'missing_artifact',
                                  'symlink_artifact', 'invalid_format'])
async def test_unverified_capture_never_becomes_success(tmp_path, audio_helper, monkeypatch, case):
    original = audio_status._query
    async def corrupt(command, *, timeout):
        result = await original(command, timeout=timeout)
        artifact = Path(command[3]) / 'system.caf'
        if case == 'wrong_source':
            result['source'] = 'microphone'
        elif case == 'inconsistent_duration':
            result['measurements']['system']['duration_seconds'] = 2
        elif case == 'missing_artifact':
            artifact.unlink()
        elif case == 'symlink_artifact':
            target = tmp_path / 'unrelated.caf'
            artifact.rename(target)
            artifact.symlink_to(target)
        else:
            artifact.write_bytes(b'not audio')
        return result
    monkeypatch.setattr(audio_status, '_query', corrupt)
    engine = Engine(tmp_path / 'state')
    try:
        command = request(tmp_path / 'recording')
        result = await engine.execute(command)
        assert result.state == 'unknown'
        assert result.data['error_code'] == 'audio_capture_outcome_unknown'
        assert await engine.execute(command) == result
        assert len(audio_helper) == 1
    finally:
        await engine.close()
