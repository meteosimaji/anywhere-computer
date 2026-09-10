import asyncio

import pytest

from anywhere_computer.http_diagnostics import MetadataHTTPError, _metadata


async def test_metadata_preserves_status_and_bounded_provider_code():
    async def reply(reader, writer):
        await reader.readuntil(b'\r\n\r\n')
        writer.write(b'HTTP/1.1 530 error\r\nConnection: close\r\n\r\nerror code: 1033')
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(reply, '127.0.0.1', 0)
    async with server:
        port = server.sockets[0].getsockname()[1]
        with pytest.raises(MetadataHTTPError) as captured:
            await _metadata(port)
        assert captured.value.status == 530
        assert captured.value.subcode == 1033
        assert '1033' not in str(captured.value)
