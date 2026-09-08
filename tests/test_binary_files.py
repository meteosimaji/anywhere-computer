import asyncio
import base64
import uuid

import pytest
from test_http_client import http_remote as http_remote
from test_remote_transport import certificates as certificates

from anywhere_computer.engine import Engine
from anywhere_computer.files import MAX_READ_BYTES, sha256
from anywhere_computer.models import Request
from anywhere_computer.remote_bridge import RemoteAgent, RemoteBackend
from anywhere_computer.remote_transport import RemoteListener


def operation(tool, **arguments):
    return Request(operation_id=uuid.uuid4().hex, tool=tool, arguments=arguments)


def encoded(data):
    return base64.b64encode(data).decode("ascii")


@pytest.fixture
async def binary_engine(tmp_path):
    engine = Engine(tmp_path / "state")
    try:
        yield engine
    finally:
        await engine.close()


async def test_binary_ranges_detect_changes_and_cover_empty_end(binary_engine, tmp_path):
    path = tmp_path / "data.bin"
    data = bytes(range(256)) * 1100
    path.write_bytes(data)
    first = await binary_engine.execute(operation("files_read_binary", path=str(path)))
    assert first.state == "completed"
    assert first.data["next_offset"] == 262144 and not first.data["eof"]
    second = await binary_engine.execute(
        operation(
            "files_read_binary",
            path=str(path),
            offset=first.data["next_offset"],
            expected_sha256=first.data["sha256"],
        )
    )
    assert second.data["eof"] and second.data["total_bytes"] == len(data)
    assert (
        base64.b64decode(first.data["data_base64"]) + base64.b64decode(second.data["data_base64"])
        == data
    )
    assert second.data["chunk_sha256"] == sha256(data[262144:])
    end = await binary_engine.execute(
        operation("files_read_binary", path=str(path), offset=len(data))
    )
    assert end.data["data_base64"] == "" and end.data["eof"]
    past = await binary_engine.execute(
        operation("files_read_binary", path=str(path), offset=len(data) + 1)
    )
    assert past.state == "failed"
    path.write_bytes(b"changed")
    conflict = await binary_engine.execute(
        operation("files_read_binary", path=str(path), expected_sha256=first.data["sha256"])
    )
    assert conflict.state == "failed" and "data_base64" not in conflict.data
    path.write_bytes(b"")
    empty = await binary_engine.execute(operation("files_read_binary", path=str(path)))
    assert empty.data["sha256"] == sha256(b"") and empty.data["eof"]


async def test_chunk_upload_restarts_and_replays_once_with_binary_restore(tmp_path):
    state, path = tmp_path / "state", tmp_path / "upload.partial"
    first_chunk, second_chunk = b"\xff\x00\x80", b"\xfe\x00\x81"
    engine = Engine(state)
    try:
        first = await engine.execute(
            operation("files_write_binary", path=str(path), data_base64=encoded(first_chunk))
        )
        append = operation(
            "files_write_binary",
            path=str(path),
            mode="append",
            data_base64=encoded(second_chunk),
            expected_sha256=first.data["sha256"],
        )
        second = await engine.execute(append)
        assert second.state == "completed" and second.data["bytes"] == 6
        assert path.read_bytes() == first_chunk + second_chunk
    finally:
        await engine.close()
    engine = Engine(state)
    try:
        replay = await engine.execute(append)
        assert replay == second and path.read_bytes() == first_chunk + second_chunk
        stale = await engine.execute(operation("files_write_binary", **append.arguments))
        assert stale.state == "failed" and path.read_bytes() == first_chunk + second_chunk
        restored = await engine.execute(
            operation(
                "files_restore",
                path=str(path),
                backup_id=second.data["backup_id"],
                expected_sha256=second.data["sha256"],
            )
        )
        assert restored.state == "completed" and path.read_bytes() == first_chunk
        published = await engine.execute(
            operation("files_move", source=str(path), destination=str(tmp_path / "final.bin"))
        )
        assert published.state == "completed" and not path.exists()
        assert (tmp_path / "final.bin").read_bytes() == first_chunk
    finally:
        await engine.close()


@pytest.mark.parametrize("bad", ["%%%", "aA", "aA==\n", "aB==", "====", "日"])
async def test_invalid_base64_never_changes_file(binary_engine, tmp_path, bad):
    path = tmp_path / "keep.bin"
    path.write_bytes(b"keep")
    result = await binary_engine.execute(
        operation(
            "files_write_binary",
            path=str(path),
            data_base64=bad,
            mode="replace",
            expected_sha256=sha256(b"keep"),
        )
    )
    assert result.state == "failed" and path.read_bytes() == b"keep"
    assert not list(binary_engine.files.backups.iterdir())


async def test_binary_limits_and_create_conflicts(binary_engine, tmp_path):
    path = tmp_path / "large.bin"
    result = await binary_engine.execute(
        operation("files_write_binary", path=str(path), data_base64=encoded(b"a" * 262145))
    )
    assert result.state == "failed" and not path.exists()
    path.write_bytes(b"a" * MAX_READ_BYTES)
    append = await binary_engine.execute(
        operation(
            "files_write_binary",
            path=str(path),
            mode="append",
            data_base64=encoded(b"b"),
            expected_sha256=sha256(b"a" * MAX_READ_BYTES),
        )
    )
    assert append.state == "failed" and path.stat().st_size == MAX_READ_BYTES
    create = await binary_engine.execute(
        operation("files_write_binary", path=str(path), data_base64="")
    )
    assert create.state == "failed" and path.stat().st_size == MAX_READ_BYTES
    with path.open("ab") as output:
        output.write(b"b")
    read = await binary_engine.execute(operation("files_read_binary", path=str(path)))
    assert read.state == "failed"


async def test_binary_chunks_traverse_authenticated_tls_and_respect_grants(certificates, tmp_path):
    context, fingerprint = certificates
    engine = Engine(tmp_path / "agent")
    grants = frozenset({"files_read_binary", "files_write_binary", "operations_get"})
    bridge = RemoteAgent(engine, {"peer": grants})
    listener = RemoteListener(
        context("server", False), {fingerprint("client"): "peer"}, bridge.dispatch
    )
    port = await listener.start("127.0.0.1", 0)
    backend = RemoteBackend(
        "127.0.0.1", port, "localhost", fingerprint("server"), context("client", True)
    )
    target = tmp_path / "remote.bin"
    content = bytes(range(256)) * 1024
    try:
        assert {item["name"] for item in await backend.catalog()} == grants
        write = operation("files_write_binary", path=str(target), data_base64=encoded(content))
        outcome = await backend.execute(write)
        assert outcome.state == "completed" and target.read_bytes() == content
        recovered = await backend.execute(
            operation("operations_get", operation_id=write.operation_id)
        )
        assert recovered.data["data"]["sha256"] == sha256(content)
        read = await backend.execute(
            operation("files_read_binary", path=str(target), expected_sha256=sha256(content))
        )
        assert base64.b64decode(read.data["data_base64"]) == content
        bridge.grant("peer", frozenset({"files_read", "operations_get"}))
        assert (await backend.execute(write)).state == "failed"
        assert (
            await backend.execute(operation("operations_get", operation_id=write.operation_id))
        ).state == "failed"
    finally:
        await listener.close()
        await engine.close()


async def test_concurrent_binary_appends_accept_only_one_hash(binary_engine, tmp_path):
    path = tmp_path / "race.bin"
    path.write_bytes(b"prefix")
    replies = await asyncio.gather(
        *[
            binary_engine.execute(
                operation(
                    "files_write_binary",
                    path=str(path),
                    mode="append",
                    data_base64=encoded(suffix),
                    expected_sha256=sha256(b"prefix"),
                )
            )
            for suffix in (b"one", b"two")
        ]
    )
    assert sorted(reply.state for reply in replies) == ["completed", "failed"]
    assert path.read_bytes() in (b"prefixone", b"prefixtwo")


@pytest.mark.parametrize(
    "http_remote",
    [frozenset({"files_read_binary", "files_write_binary", "operations_get"})],
    indirect=True,
)
async def test_http_binary_lost_response_is_recovered_without_reappend(http_remote, tmp_path):
    backend, _, _, _, calls, faults = http_remote
    path = tmp_path / "http-upload.bin"
    path.write_bytes(b"prefix")
    content = bytes(range(256)) * 1024
    faults["lose_write_response"] = True
    request = operation(
        "files_write_binary",
        path=str(path),
        mode="append",
        data_base64=encoded(content),
        expected_sha256=sha256(b"prefix"),
    )
    outcome = await backend.execute(request)
    assert outcome.state == "unknown" and outcome.operation_id == request.operation_id
    recovered = await backend.execute(
        operation("operations_get", operation_id=request.operation_id)
    )
    assert recovered.data["state"] == "completed"
    assert recovered.data["data"]["sha256"] == sha256(b"prefix" + content)
    read = await backend.execute(
        operation("files_read_binary", path=str(path), expected_sha256=sha256(b"prefix" + content))
    )
    assert read.data["total_bytes"] == len(b"prefix" + content)
    writes = [
        packet
        for packet in calls
        if packet
        and packet.get("method") == "tools/call"
        and packet["params"]["name"] == "files_write_binary"
    ]
    assert len(writes) == 1 and path.read_bytes() == b"prefix" + content
