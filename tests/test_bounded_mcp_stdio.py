import sys

import anyio
import pytest

from anywhere_computer.bounded_mcp_stdio import bounded_lines
from anywhere_computer.direct_mcp import DirectMCPContext


class Chunks:
    def __init__(self, chunks):
        self.chunks = iter(chunks)
        self.received = 0

    async def receive(self, maximum):
        assert maximum == 16384
        try:
            chunk = next(self.chunks)
        except StopIteration:
            raise anyio.EndOfStream from None
        self.received += len(chunk)
        return chunk


async def test_fragmented_utf8_and_multiple_frames_are_preserved():
    encoded = '日本語😀'.encode()
    source = Chunks([encoded[:2], encoded[2:] + b'\nnext\n'])
    assert [line async for line in bounded_lines(source)] == [encoded, b'next']


@pytest.mark.parametrize('newline', [False, True])
async def test_oversized_frame_stops_reading_at_limit(newline):
    source = Chunks([b'x' * 16, b'x' * 16, b'x' * 16 + (b'\n' if newline else b''),
                     b'x' * 16])
    with pytest.raises(ValueError, match='byte limit'):
        [line async for line in bounded_lines(source, limit=32)]
    assert source.received <= 49


async def test_truncated_frame_is_not_accepted_as_complete():
    with pytest.raises(ValueError, match='inside a frame'):
        [line async for line in bounded_lines(Chunks([b'{']))]


async def test_real_oversized_peer_is_closed_without_logging_payload(tmp_path, caplog):
    context = DirectMCPContext([
        sys.executable, '-I', '-c',
        'import sys; sys.stdout.buffer.write(b"synthetic-private-output" * 500000); '
        'sys.stdout.flush()',
    ], tmp_path)
    with pytest.raises(RuntimeError, match='did not initialize'):
        await context.open()
    await context.close()
    assert 'synthetic-private-output' not in caplog.text
