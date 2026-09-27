import asyncio
import base64
import hashlib
import multiprocessing
import os
import sqlite3
import uuid
from pathlib import Path

import pytest
from test_http_client import http_remote as http_remote

from anywhere_computer.engine import Engine
from anywhere_computer.files import sha256
from anywhere_computer.models import (
    BeginUpload,
    Reply,
    Request,
    ResolveUpload,
    TransferId,
    UploadChunk,
)
from anywhere_computer.remote_bridge import RemoteAgent
from anywhere_computer.uploads import UPLOAD_TOOLS, UploadOutcomeUnknown, Uploads

pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="Upload publication requires safe directory-relative operations"
)


def transfer():
    return uuid.uuid4().hex


def request(tool, **arguments):
    return Request(operation_id=uuid.uuid4().hex, tool=tool, arguments=arguments)


@pytest.fixture
def uploads(tmp_path):
    locks = tmp_path / "file-locks"
    locks.mkdir()
    return Uploads(tmp_path, file_locks=locks)


def begin(store, path, content, identity=None):
    args = BeginUpload(
        transfer_id=identity or transfer(),
        path=str(path),
        total_bytes=len(content),
        sha256=sha256(content),
    )
    store.begin(args)
    return args


def chunk(store, identity, offset, content):
    return store.chunk(
        UploadChunk(
            transfer_id=identity, offset=offset, data_base64=base64.b64encode(content).decode()
        )
    )


def test_upload_status_binds_original_path_when_parent_is_alias(uploads, tmp_path):
    actual = tmp_path / "actual"
    actual.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(actual, target_is_directory=True)
    args = begin(uploads, alias / "result.bin", b"x")
    status = uploads.status(TransferId(transfer_id=args.transfer_id))
    assert status["requested_path"] == str(alias / "result.bin")
    assert status["path"] == str(actual / "result.bin")
    with pytest.raises(ValueError, match="different content or destination"):
        uploads.begin(args.model_copy(update={"path": str(actual / "result.bin")}))


def test_upload_larger_than_old_limit_streams_and_resumes_after_reopen(uploads, tmp_path):
    block = bytes(range(256)) * 1024
    total = len(block) * 68
    digest = hashlib.sha256()
    for _ in range(68):
        digest.update(block)
    identity, target = transfer(), tmp_path / "large.bin"
    args = BeginUpload(
        transfer_id=identity, path=str(target), total_bytes=total, sha256=digest.hexdigest()
    )
    uploads.begin(args)
    for index in range(34):
        chunk(uploads, identity, index * len(block), block)
    reopened = Uploads(tmp_path, file_locks=tmp_path / "file-locks")
    assert reopened.begin(args)["received_bytes"] == len(block) * 34
    for index in range(34, 68):
        chunk(reopened, identity, index * len(block), block)
    result = reopened.commit(TransferId(transfer_id=identity))
    assert result["state"] == "complete" and result["publication_verified"]
    with target.open("rb") as source:
        assert hashlib.file_digest(source, "sha256").hexdigest() == digest.hexdigest()
    assert target.stat().st_size == total > 16 * 1024**2
    assert reopened.commit(TransferId(transfer_id=identity)) == result
    with sqlite3.connect(reopened.database) as db:
        assert db.execute("SELECT count(*) FROM chunks").fetchone()[0] == 0


def test_duplicate_chunks_conflicts_and_invalid_content(uploads, tmp_path):
    args = begin(uploads, tmp_path / "out", b"abcdef")
    identity = args.transfer_id
    first = chunk(uploads, identity, 0, b"abc")
    assert chunk(uploads, identity, 0, b"abc") == first
    for offset, data in ((0, b"xyz"), (1, b"b"), (4, b"de"), (3, b"toolong")):
        with pytest.raises(ValueError):
            chunk(uploads, identity, offset, data)
    with pytest.raises(ValueError, match="incomplete"):
        uploads.commit(TransferId(transfer_id=identity))
    with pytest.raises(ValueError):
        uploads.begin(args.model_copy(update={"sha256": "f" * 64}))
    with pytest.raises(ValueError):
        uploads.chunk(UploadChunk(transfer_id=identity, offset=3, data_base64="aB=="))
    chunk(uploads, identity, 3, b"xyz")
    with pytest.raises(ValueError, match="hash"):
        uploads.commit(TransferId(transfer_id=identity))
    assert uploads.status(TransferId(transfer_id=identity))["state"] == "receiving"
    assert not (tmp_path / "out").exists()
    assert not list(tmp_path.glob(".anywhere-upload-*"))
    assert uploads.abort(TransferId(transfer_id=identity))["state"] == "aborted"


def test_empty_upload_target_reservations_and_external_create(uploads, tmp_path):
    args = begin(uploads, tmp_path / "Output", b"")
    with pytest.raises(ValueError, match="reserves"):
        begin(uploads, tmp_path / "output", b"")
    (tmp_path / "Output").write_bytes(b"external")
    with pytest.raises(FileExistsError):
        uploads.commit(TransferId(transfer_id=args.transfer_id))
    assert (tmp_path / "Output").read_bytes() == b"external"
    assert uploads.abort(TransferId(transfer_id=args.transfer_id))["state"] == "aborted"
    with pytest.raises(ValueError, match="unused"):
        begin(uploads, tmp_path / "Output", b"")
    empty = begin(uploads, tmp_path / "empty", b"")
    assert uploads.commit(empty)["state"] == "complete"
    assert (tmp_path / "empty").read_bytes() == b""


def test_parent_symlink_swap_after_begin_cannot_publish_elsewhere(uploads, tmp_path):
    parent = tmp_path / "destination"
    parent.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    args = begin(uploads, parent / "result.bin", b"abc")
    chunk(uploads, args.transfer_id, 0, b"abc")
    parent.rename(tmp_path / "moved")
    parent.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="parent directory changed"):
        uploads.commit(TransferId(transfer_id=args.transfer_id))
    assert not (outside / "result.bin").exists()
    assert not (tmp_path / "moved" / "result.bin").exists()
    assert uploads.status(TransferId(transfer_id=args.transfer_id))["state"] == "receiving"


def test_parent_swap_during_link_never_claims_original_path(uploads, tmp_path, monkeypatch):
    parent = tmp_path / "destination"
    parent.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    args = begin(uploads, parent / "result.bin", b"abc")
    chunk(uploads, args.transfer_id, 0, b"abc")
    original = os.link

    def moved(source, destination, **kwargs):
        parent.rename(tmp_path / "moved")
        parent.symlink_to(outside, target_is_directory=True)
        original(source, destination, **kwargs)

    monkeypatch.setattr("anywhere_computer.uploads.os.link", moved)
    with pytest.raises(UploadOutcomeUnknown):
        uploads.commit(TransferId(transfer_id=args.transfer_id))
    assert not (outside / "result.bin").exists()
    assert (tmp_path / "moved" / "result.bin").read_bytes() == b"abc"
    assert uploads.status(TransferId(transfer_id=args.transfer_id))["publication_verified"] is False
    with pytest.raises(ValueError, match="parent directory changed"):
        uploads.resolve(ResolveUpload(transfer_id=args.transfer_id,
                                      action="confirm_published"))


def test_legacy_upload_without_parent_identity_fails_closed(uploads, tmp_path):
    args = begin(uploads, tmp_path / "legacy.bin", b"abc")
    chunk(uploads, args.transfer_id, 0, b"abc")
    with sqlite3.connect(uploads.database) as db:
        db.execute("DELETE FROM upload_parents WHERE id=?", (args.transfer_id,))
    with pytest.raises(ValueError, match="parent identity is unavailable"):
        uploads.commit(TransferId(transfer_id=args.transfer_id))
    assert not (tmp_path / "legacy.bin").exists()
    assert uploads.status(TransferId(transfer_id=args.transfer_id))["state"] == "receiving"


def test_platform_without_safe_directory_operations_rejects_begin(
    uploads, tmp_path, monkeypatch,
):
    monkeypatch.setattr("anywhere_computer.uploads._SAFE_DIRFD_SUPPORTED", False)
    with pytest.raises(ValueError, match="unsupported on this platform"):
        begin(uploads, tmp_path / "unsupported.bin", b"abc")
    assert not (tmp_path / "unsupported.bin").exists()


def test_link_eexist_does_not_claim_publication(uploads, tmp_path, monkeypatch):
    target = tmp_path / "collision.bin"
    args = begin(uploads, target, b"abc")
    chunk(uploads, args.transfer_id, 0, b"abc")

    def collision(source, destination, **kwargs):
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                             0o600, dir_fd=kwargs["dst_dir_fd"])
        os.close(descriptor)
        raise FileExistsError("simulated kernel collision")

    monkeypatch.setattr("anywhere_computer.uploads.os.link", collision)
    with pytest.raises(UploadOutcomeUnknown, match="Publication outcome is unknown"):
        uploads.commit(TransferId(transfer_id=args.transfer_id))
    assert target.read_bytes() == b""
    status = uploads.status(TransferId(transfer_id=args.transfer_id))
    assert status["state"] == "unknown" and not status["publication_verified"]
    with pytest.raises(ValueError, match="unavailable"):
        uploads.commit(TransferId(transfer_id=args.transfer_id))


def test_active_count_and_reserved_bytes_are_transactional(uploads, tmp_path):
    ids = [begin(uploads, tmp_path / str(i), b"").transfer_id for i in range(8)]
    with pytest.raises(ValueError, match="capacity"):
        begin(uploads, tmp_path / "extra", b"")
    for identity in ids:
        uploads.abort(TransferId(transfer_id=identity))
    for index in range(4):
        uploads.begin(
            BeginUpload(
                transfer_id=transfer(),
                path=str(tmp_path / f"large-{index}"),
                total_bytes=1024**3,
                sha256="a" * 64,
            )
        )
    with pytest.raises(ValueError, match="capacity"):
        begin(uploads, tmp_path / "one-byte", b"x")


def crash_during_publish(directory, identity, link_first):
    import anywhere_computer.uploads as module

    store = Uploads(Path(directory), file_locks=Path(directory) / "file-locks")
    original = module.os.link
    if link_first == "stream":
        module.os.fsync = lambda descriptor: os._exit(18)

    def crash(source, target, **kwargs):
        if link_first:
            original(source, target, **kwargs)
        os._exit(17)

    module.os.link = crash
    store.commit(TransferId(transfer_id=identity))


def test_prepublication_crash_retains_staging_location_until_explicit_cleanup(uploads, tmp_path):
    args = begin(uploads, tmp_path / "prepublish", b"abc")
    chunk(uploads, args.transfer_id, 0, b"abc")
    process = multiprocessing.get_context("spawn").Process(
        target=crash_during_publish, args=(str(tmp_path), args.transfer_id, "stream")
    )
    process.start()
    try:
        process.join(20)
        assert process.exitcode == 18
    finally:
        if process.is_alive():
            process.kill()
            process.join()
    status = uploads.status(args)
    assert status["state"] == "receiving" and status["staging_exists"]
    with pytest.raises(ValueError, match="Previous staging"):
        uploads.commit(args)
    assert uploads.status(args)["staging_path"] == status["staging_path"]
    Path(status["staging_path"]).unlink()
    result = uploads.commit(args)
    assert result["state"] == "complete" and not result["staging_exists"]


def test_old_upload_schema_keeps_chunks_during_upgrade(uploads, tmp_path):
    args = begin(uploads, tmp_path / "migrated", b"abc")
    chunk(uploads, args.transfer_id, 0, b"abc")
    with sqlite3.connect(uploads.database) as db:
        db.execute("ALTER TABLE uploads DROP COLUMN temporary")
        db.execute("ALTER TABLE uploads DROP COLUMN requested_path")
        db.execute("PRAGMA user_version=1")
    reopened = Uploads(tmp_path, file_locks=tmp_path / "file-locks")
    assert reopened.status(args)["received_bytes"] == 3
    assert reopened.status(args)["requested_path"] is None
    assert reopened.commit(args)["state"] == "complete"
    assert (tmp_path / "migrated").read_bytes() == b"abc"


def test_version_two_upload_schema_preserves_active_transfer(uploads, tmp_path):
    args = begin(uploads, tmp_path / "version-two.bin", b"abc")
    with sqlite3.connect(uploads.database) as db:
        db.execute("ALTER TABLE uploads DROP COLUMN requested_path")
        db.execute("PRAGMA user_version=2")
    reopened = Uploads(tmp_path, file_locks=tmp_path / "file-locks")
    assert reopened.status(args)["requested_path"] is None
    reopened.begin(args)
    chunk(reopened, args.transfer_id, 0, b"abc")
    assert reopened.commit(args)["publication_verified"] is True
    assert (tmp_path / "version-two.bin").read_bytes() == b"abc"


@pytest.mark.parametrize("link_first", [False, True])
def test_real_process_crash_never_republishes_and_can_be_resolved(uploads, tmp_path, link_first):
    target = tmp_path / "crashed.bin"
    args = begin(uploads, target, b"\x00\xffdata")
    chunk(uploads, args.transfer_id, 0, b"\x00\xffdata")
    process = multiprocessing.get_context("spawn").Process(
        target=crash_during_publish, args=(str(tmp_path), args.transfer_id, link_first)
    )
    process.start()
    try:
        process.join(20)
        assert process.exitcode == 17
    finally:
        if process.is_alive():
            process.kill()
            process.join()
    reopened = Uploads(tmp_path, file_locks=tmp_path / "file-locks")
    state = reopened.status(args)
    assert state["state"] == "unknown" and not state["publication_verified"]
    assert Path(state["staging_path"]).is_file()
    with pytest.raises(ValueError):
        reopened.commit(args)
    if link_first:
        assert target.read_bytes() == b"\x00\xffdata"
        target.write_bytes(b"wrong")
        with pytest.raises(ValueError):
            reopened.resolve(
                ResolveUpload(transfer_id=args.transfer_id, action="confirm_published")
            )
        target.write_bytes(b"\x00\xffdata")
        result = reopened.resolve(
            ResolveUpload(transfer_id=args.transfer_id, action="confirm_published")
        )
        assert result["state"] == "complete"
    else:
        with pytest.raises(OSError):
            reopened.resolve(
                ResolveUpload(transfer_id=args.transfer_id, action="confirm_published")
            )
        result = reopened.resolve(
            ResolveUpload(transfer_id=args.transfer_id, action="discard_staging")
        )
        assert result["state"] == "discarded" and not target.exists()
    with sqlite3.connect(reopened.database) as db:
        assert db.execute("SELECT count(*) FROM chunks").fetchone()[0] == 0
    # Leftover filesystem staging is explicitly reported, never guessed or erased.
    Path(state["staging_path"]).unlink()


async def test_remote_transfer_ids_are_isolated_and_never_leak_in_lookup(tmp_path):
    engine = Engine(tmp_path / "agent")
    bridge = RemoteAgent(engine, {"a": UPLOAD_TOOLS | {"operations_get"}, "b": UPLOAD_TOOLS})
    identity = transfer()

    async def dispatch(peer, operation):
        return Reply.model_validate_json(
            await bridge.dispatch(peer, operation.model_dump_json().encode())
        )

    try:
        begin_op = request(
            "upload_begin",
            transfer_id=identity,
            path=str(tmp_path / "remote"),
            total_bytes=3,
            sha256=sha256(b"abc"),
        )
        assert (await dispatch("a", begin_op)).state == "completed"
        assert (
            await dispatch("b", request("upload_status", transfer_id=identity))
        ).state == "failed"
        outcome = await dispatch(
            "a", request("upload_chunk", transfer_id=identity, offset=0, data_base64="YWJj")
        )
        assert outcome.data["received_bytes"] == 3
        lookup = await dispatch("a", request("operations_get", operation_id=begin_op.operation_id))
        assert RemoteAgent.internal_id("a", identity) not in lookup.model_dump_json()
        assert (await dispatch("a", request("upload_commit", transfer_id=identity))).data[
            "state"
        ] == "complete"
        bridge.grant("a", frozenset({"operations_get"}))
        assert (
            await dispatch("a", request("upload_status", transfer_id=identity))
        ).state == "failed"
    finally:
        await engine.close()


async def test_concurrent_chunks_share_one_offset_and_report_unknown_effect(tmp_path, monkeypatch):
    engine = Engine(tmp_path / "state")
    identity, target = transfer(), tmp_path / "final"
    try:
        await engine.execute(
            request(
                "upload_begin",
                transfer_id=identity,
                path=str(target),
                total_bytes=3,
                sha256=sha256(b"abc"),
            )
        )
        replies = await asyncio.gather(
            *[
                engine.execute(
                    request(
                        "upload_chunk",
                        transfer_id=identity,
                        offset=0,
                        data_base64=base64.b64encode(data).decode(),
                    )
                )
                for data in (b"abc", b"xyz")
            ]
        )
        assert sorted(item.state for item in replies) == ["completed", "failed"]
        # Use whichever chunk won for this independent publication fault test.
        with sqlite3.connect(engine.uploads.database) as db:
            data = db.execute("SELECT data FROM chunks").fetchone()[0]
            db.execute("UPDATE uploads SET digest=?", (sha256(data),))
        original = os.link

        def lost(source, destination, **kwargs):
            original(source, destination, **kwargs)
            raise ConnectionError("synthetic lost publication result")

        monkeypatch.setattr("anywhere_computer.uploads.os.link", lost)
        outcome = await engine.execute(request("upload_commit", transfer_id=identity))
        assert outcome.state == "unknown" and target.read_bytes() == data
        assert engine.uploads.status(TransferId(transfer_id=identity))["state"] == "unknown"
    finally:
        await engine.close()


@pytest.mark.parametrize("http_remote", [UPLOAD_TOOLS | {"operations_get"}], indirect=True)
async def test_http_upload_resumes_after_lost_chunk_response(http_remote, tmp_path):
    backend, _, _, _, _, _ = http_remote
    original = backend.wire
    lost = False

    def wire(resource, method, packet, headers):
        nonlocal lost
        response = original(resource, method, packet, headers)
        if (
            not lost
            and packet
            and packet.get("method") == "tools/call"
            and packet["params"]["name"] == "upload_chunk"
        ):
            lost = True
            raise ConnectionError("synthetic chunk response loss")
        return response

    backend.wire = wire
    identity, target = transfer(), tmp_path / "http-final.bin"
    started = await backend.execute(
        request(
            "upload_begin",
            transfer_id=identity,
            path=str(target),
            total_bytes=6,
            sha256=sha256(b"abcdef"),
        )
    )
    assert started.state == "completed"
    outcome = await backend.execute(
        request("upload_chunk", transfer_id=identity, offset=0, data_base64="YWJj")
    )
    assert outcome.state == "unknown"
    status = await backend.execute(request("upload_status", transfer_id=identity))
    assert status.data["received_bytes"] == 3 and not target.exists()
    assert (
        await backend.execute(
            request("upload_chunk", transfer_id=identity, offset=3, data_base64="ZGVm")
        )
    ).state == "completed"
    assert (await backend.execute(request("upload_commit", transfer_id=identity))).data[
        "state"
    ] == "complete"
    assert target.read_bytes() == b"abcdef"
