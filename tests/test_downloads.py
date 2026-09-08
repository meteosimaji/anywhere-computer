import base64
import hashlib
import multiprocessing
import os
import sqlite3
import uuid

import pytest
from test_http_client import http_remote as http_remote

from anywhere_computer.downloads import DOWNLOAD_TOOLS, Downloads
from anywhere_computer.engine import Engine
from anywhere_computer.models import BeginDownload, DownloadRange, Reply, Request, TransferId
from anywhere_computer.remote_bridge import RemoteAgent


def request(tool, **arguments):
    return Request(operation_id=uuid.uuid4().hex, tool=tool, arguments=arguments)


def test_download_survives_source_deletion_and_restart(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    source = tmp_path / "source.bin"
    block = bytes(range(256)) * 1024
    digest = hashlib.sha256()
    with source.open("wb") as output:
        for _ in range(68):
            output.write(block)
            digest.update(block)
    identity = uuid.uuid4().hex
    args = BeginDownload(
        transfer_id=identity,
        path=str(source),
        expected_sha256=digest.hexdigest(),
    )
    downloads = Downloads(state)
    ready = downloads.begin(args)
    assert ready["state"] == "ready" and ready["total_bytes"] == 17 * 1024**2
    source.unlink()
    downloads = Downloads(state)
    assert downloads.begin(args) == ready
    received = hashlib.sha256()
    offset = 0
    while offset < ready["total_bytes"]:
        chunk = downloads.read(DownloadRange(transfer_id=identity, offset=offset, limit=123457))
        data = base64.b64decode(chunk["data_base64"])
        assert hashlib.sha256(data).hexdigest() == chunk["chunk_sha256"]
        received.update(data)
        offset = chunk["next_offset"]
    assert received.hexdigest() == digest.hexdigest() and chunk["eof"]
    end = downloads.read(DownloadRange(transfer_id=identity, offset=offset))
    assert end["data_base64"] == "" and end["eof"]
    with pytest.raises(ValueError, match="offset"):
        downloads.read(DownloadRange(transfer_id=identity, offset=offset + 1))
    closed = downloads.close(TransferId(transfer_id=identity))
    assert closed["state"] == "closed"
    assert downloads.close(TransferId(transfer_id=identity)) == closed
    assert downloads.begin(args) == closed
    with pytest.raises(ValueError, match="closed"):
        downloads.read(DownloadRange(transfer_id=identity))
    with sqlite3.connect(downloads.database) as db:
        assert db.execute("SELECT count(*) FROM chunks").fetchone()[0] == 0


def test_download_failure_rolls_back_and_closed_ids_are_not_reused(tmp_path):
    downloads = Downloads(tmp_path)
    source = tmp_path / "source"
    source.write_bytes(b"hello")
    identity = uuid.uuid4().hex
    with pytest.raises(ValueError, match="hash"):
        downloads.begin(
            BeginDownload(transfer_id=identity, path=str(source), expected_sha256="0" * 64)
        )
    with pytest.raises(ValueError, match="unknown"):
        downloads.status(TransferId(transfer_id=identity))
    ready = downloads.begin(BeginDownload(transfer_id=identity, path=str(source)))
    source.write_bytes(b"changed")
    assert downloads.begin(BeginDownload(transfer_id=identity, path=str(source))) == ready
    with pytest.raises(ValueError, match="different arguments"):
        downloads.begin(BeginDownload(transfer_id=identity, path=str(source) + "other"))
    with sqlite3.connect(downloads.database) as db:
        db.execute("UPDATE chunks SET data=? WHERE id=?", (b"wrong", identity))
    with pytest.raises(ValueError, match="corrupt"):
        downloads.read(DownloadRange(transfer_id=identity))
    downloads.close(TransferId(transfer_id=identity))
    assert source.read_bytes() == b"changed"


def test_download_empty_capacity_and_oversize(tmp_path):
    downloads = Downloads(tmp_path)
    source = tmp_path / "empty"
    source.touch()
    identities = [uuid.uuid4().hex for _ in range(8)]
    for identity in identities:
        downloads.begin(BeginDownload(transfer_id=identity, path=str(source)))
    assert downloads.read(DownloadRange(transfer_id=identities[0]))["eof"]
    with pytest.raises(ValueError, match="capacity"):
        downloads.begin(BeginDownload(transfer_id=uuid.uuid4().hex, path=str(source)))
    downloads.close(TransferId(transfer_id=identities[0]))
    with source.open("wb") as output:
        output.truncate(1024**3 + 1)
    with pytest.raises(ValueError, match="limit"):
        downloads.begin(BeginDownload(transfer_id=uuid.uuid4().hex, path=str(source)))


def crash_copy(state, source, identity):
    import anywhere_computer.downloads as module

    downloads = Downloads(state)

    calls = 0

    def interrupted(data):
        nonlocal calls
        calls += 1
        if calls == 2:
            os._exit(19)
        return hashlib.sha256(data).hexdigest()

    module.sha256 = interrupted
    downloads.begin(BeginDownload(transfer_id=identity, path=str(source)))


def test_process_crash_leaves_no_partial_download(tmp_path):
    source = tmp_path / "source"
    source.write_bytes(b"a" * 262144 + b"bc")
    identity = uuid.uuid4().hex
    process = multiprocessing.get_context("spawn").Process(
        target=crash_copy,
        args=(tmp_path, source, identity),
    )
    process.start()
    try:
        process.join(20)
        assert process.exitcode == 19
    finally:
        if process.is_alive():
            process.kill()
            process.join(5)
        process.close()
    downloads = Downloads(tmp_path)
    with pytest.raises(ValueError, match="unknown"):
        downloads.status(TransferId(transfer_id=identity))
    ready = downloads.begin(BeginDownload(transfer_id=identity, path=str(source)))
    assert ready["sha256"] == hashlib.sha256(b"a" * 262144 + b"bc").hexdigest()


async def test_remote_download_ids_are_scoped_and_operations_hide_internal_id(tmp_path):
    engine = Engine(tmp_path / "state")
    bridge = RemoteAgent(engine, {"a": DOWNLOAD_TOOLS | {"operations_get"}, "b": DOWNLOAD_TOOLS})
    source = tmp_path / "source"
    source.write_bytes(b"one")
    identity = uuid.uuid4().hex

    async def call(peer, item):
        return Reply.model_validate_json(
            await bridge.dispatch(peer, item.model_dump_json().encode())
        )

    try:
        beginning = request("download_begin", transfer_id=identity, path=str(source))
        assert (await call("a", beginning)).state == "completed"
        assert (await call("b", request("download_status", transfer_id=identity))).state == "failed"
        source.write_bytes(b"two")
        assert (await call("b", beginning)).state == "completed"
        for peer, data in (("a", b"one"), ("b", b"two")):
            reply = await call(peer, request("download_read", transfer_id=identity))
            assert base64.b64decode(reply.data["data_base64"]) == data
        recovered = await call("a", request("operations_get", operation_id=beginning.operation_id))
        assert bridge.internal_id("a", identity) not in recovered.model_dump_json()
        bridge.grant("a", frozenset({"operations_get"}))
        assert (await call("a", request("download_read", transfer_id=identity))).state == "failed"
    finally:
        await engine.close()


@pytest.mark.parametrize("http_remote", [DOWNLOAD_TOOLS], indirect=True)
async def test_http_download_copy_is_independent_of_source(http_remote, tmp_path):
    backend, _, _, _, _, _ = http_remote
    source = tmp_path / "http-source"
    content = bytes(range(256)) * 2048
    source.write_bytes(content)
    identity = uuid.uuid4().hex
    ready = await backend.execute(request("download_begin", transfer_id=identity, path=str(source)))
    assert ready.state == "completed"
    source.unlink()
    first = await backend.execute(request("download_read", transfer_id=identity, offset=262143))
    assert first.state == "completed"
    assert base64.b64decode(first.data["data_base64"]) == content[262143:524287]
    assert (
        await backend.execute(request("download_close", transfer_id=identity))
    ).state == "completed"


def test_download_detects_source_mutation_and_rolls_back_chunks(tmp_path, monkeypatch):
    import anywhere_computer.downloads as module

    downloads = Downloads(tmp_path)
    source = tmp_path / "changing"
    source.write_bytes(b"before")
    identity = uuid.uuid4().hex
    real_hash = module.sha256

    def change_source(data):
        source.write_bytes(b"longer after")
        return real_hash(data)

    monkeypatch.setattr(module, "sha256", change_source)
    with pytest.raises(ValueError, match="changed"):
        downloads.begin(BeginDownload(transfer_id=identity, path=str(source)))
    with sqlite3.connect(downloads.database) as db:
        assert db.execute("SELECT count(*) FROM chunks").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM downloads").fetchone()[0] == 0


def test_path_and_fd_ctime_can_have_different_meanings(tmp_path, monkeypatch):
    from pathlib import Path
    from types import SimpleNamespace

    from anywhere_computer.files import Files
    from anywhere_computer.models import ReadBinary

    source = tmp_path / "ctime"
    source.write_bytes(b"content")
    downloads = Downloads(tmp_path)
    files = Files(tmp_path)
    real_stat = Path.stat

    def path_stat(path, *args, **kwargs):
        metadata = real_stat(path, *args, **kwargs)
        if path != source:
            return metadata
        values = {name: getattr(metadata, name) for name in dir(metadata) if name.startswith("st_")}
        values["st_ctime_ns"] = metadata.st_ctime_ns - 1000000
        return SimpleNamespace(**values)

    monkeypatch.setattr(Path, "stat", path_stat)
    assert files.read_binary(ReadBinary(path=str(source)))["total_bytes"] == 7
    ready = downloads.begin(BeginDownload(transfer_id=uuid.uuid4().hex, path=str(source)))
    assert ready["total_bytes"] == 7
