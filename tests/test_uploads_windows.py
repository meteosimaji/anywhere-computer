"""Windows upload ancestry pinning and recovery."""

import base64
import hashlib
import os
import sqlite3
import uuid

import pytest

from anywhere_computer.engine import Engine
from anywhere_computer.models import BeginUpload, Request, ResolveUpload, TransferId, UploadChunk
from anywhere_computer.upload_win32 import PinnedRegular
from anywhere_computer.uploads import UploadOutcomeUnknown, Uploads

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows-specific upload boundary")


async def test_windows_upload_begin_and_commit_use_pinned_parent(tmp_path):
    target = tmp_path / "target.bin"
    arguments = {
        "transfer_id": uuid.uuid4().hex,
        "path": str(target),
        "total_bytes": 1,
        "sha256": hashlib.sha256(b"x").hexdigest(),
    }
    locks = tmp_path / "locks"
    locks.mkdir()
    uploads = Uploads(tmp_path / "direct", file_locks=locks)
    assert uploads.begin(BeginUpload(**arguments))["state"] == "receiving"
    uploads.chunk(UploadChunk(transfer_id=arguments["transfer_id"], offset=0,
                              data_base64=base64.b64encode(b"x").decode()))
    assert uploads.commit(TransferId(transfer_id=arguments["transfer_id"]))[
        "publication_verified"] is True
    assert target.read_bytes() == b"x"
    engine = Engine(tmp_path / "engine")
    try:
        second = {**arguments, "transfer_id": uuid.uuid4().hex,
                  "path": str(tmp_path / "engine.bin")}
        reply = await engine.execute(Request(
            operation_id=uuid.uuid4().hex, tool="upload_begin", arguments=second))
        assert reply.state == "completed"
    finally:
        await engine.close()


def test_windows_legacy_unknown_can_discard_database_reservation(tmp_path):
    locks = tmp_path / "locks"
    locks.mkdir()
    uploads = Uploads(tmp_path, file_locks=locks)
    identity = uuid.uuid4().hex
    with sqlite3.connect(uploads.database) as db:
        db.execute("INSERT INTO uploads(id,path,total,digest,received,state) "
                   "VALUES(?,?,?,?,?,?)", (
                       identity, str(tmp_path / "target.bin"), 0,
                       hashlib.sha256(b"").hexdigest(), 0, "publishing"))
    with pytest.raises(ValueError, match="parent identity is unavailable"):
        uploads.resolve(ResolveUpload(transfer_id=identity,
                                      action="confirm_published"))
    discarded = uploads.resolve(ResolveUpload(transfer_id=identity,
                                               action="discard_staging"))
    assert discarded["state"] == "discarded"
    assert uploads.status(TransferId(transfer_id=identity))["state"] == "discarded"


def test_windows_parent_swap_after_begin_is_rejected(tmp_path):
    locks = tmp_path / "locks"
    locks.mkdir()
    uploads = Uploads(tmp_path / "state", file_locks=locks)
    parent = tmp_path / "destination"
    parent.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    identity = uuid.uuid4().hex
    uploads.begin(BeginUpload(transfer_id=identity, path=str(parent / "result.bin"),
                              total_bytes=0, sha256=hashlib.sha256(b"").hexdigest()))
    parent.rename(tmp_path / "moved")
    try:
        parent.symlink_to(outside, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"Windows test account cannot create a directory symlink: {error}")
    with pytest.raises(ValueError, match="directory"):
        uploads.commit(TransferId(transfer_id=identity))
    assert not (outside / "result.bin").exists()


def test_windows_parent_cannot_be_renamed_during_link(tmp_path, monkeypatch):
    locks = tmp_path / "locks"
    locks.mkdir()
    uploads = Uploads(tmp_path / "state", file_locks=locks)
    parent = tmp_path / "destination"
    parent.mkdir()
    identity = uuid.uuid4().hex
    target = parent / "result.bin"
    uploads.begin(BeginUpload(transfer_id=identity, path=str(target),
                              total_bytes=0, sha256=hashlib.sha256(b"").hexdigest()))
    original = PinnedRegular.link
    rename_blocked = False

    def guarded_link(source, destination):
        nonlocal rename_blocked
        try:
            parent.rename(tmp_path / "moved")
        except OSError:
            rename_blocked = True
        else:
            raise AssertionError("Pinned parent was renamed during publication")
        original(source, destination)

    monkeypatch.setattr(PinnedRegular, "link", guarded_link)
    result = uploads.commit(TransferId(transfer_id=identity))
    assert rename_blocked and result["publication_verified"] is True
    assert target.read_bytes() == b""


def test_windows_staging_name_reuse_never_publishes_replacement(tmp_path, monkeypatch):
    locks = tmp_path / "locks"
    locks.mkdir()
    uploads = Uploads(tmp_path / "state", file_locks=locks)
    identity = uuid.uuid4().hex
    target = tmp_path / "result.bin"
    uploads.begin(BeginUpload(transfer_id=identity, path=str(target),
                              total_bytes=3, sha256=hashlib.sha256(b"abc").hexdigest()))
    uploads.chunk(UploadChunk(transfer_id=identity, offset=0,
                              data_base64=base64.b64encode(b"abc").decode()))
    original = PinnedRegular.link
    replacement_attempted = False

    def guarded_link(source, destination):
        nonlocal replacement_attempted
        staged = next(target.parent.glob(".anywhere-upload-*"))
        try:
            staged.unlink()
        except OSError:
            pass
        else:
            staged.write_bytes(b"bad")
            replacement_attempted = True
        original(source, destination)

    monkeypatch.setattr(PinnedRegular, "link", guarded_link)
    try:
        result = uploads.commit(TransferId(transfer_id=identity))
    except UploadOutcomeUnknown:
        assert not target.exists() or target.read_bytes() == b"abc"
        assert uploads.status(TransferId(transfer_id=identity))["publication_verified"] is False
    else:
        assert result["publication_verified"] is True
        assert target.read_bytes() == b"abc"
    if replacement_attempted:
        assert not target.exists() or target.read_bytes() == b"abc"


def test_windows_link_collision_never_claims_publication(tmp_path, monkeypatch):
    locks = tmp_path / "locks"
    locks.mkdir()
    uploads = Uploads(tmp_path / "state", file_locks=locks)
    identity = uuid.uuid4().hex
    target = tmp_path / "collision.bin"
    uploads.begin(BeginUpload(transfer_id=identity, path=str(target),
                              total_bytes=0, sha256=hashlib.sha256(b"").hexdigest()))

    def collision(source, destination):
        with open(destination, "xb"):
            pass
        original(source, destination)

    original = PinnedRegular.link
    monkeypatch.setattr(PinnedRegular, "link", collision)
    with pytest.raises(UploadOutcomeUnknown):
        uploads.commit(TransferId(transfer_id=identity))
    assert uploads.status(TransferId(transfer_id=identity))["publication_verified"] is False


def test_windows_wrong_source_link_is_detected_by_file_identity(tmp_path, monkeypatch):
    locks = tmp_path / "locks"
    locks.mkdir()
    uploads = Uploads(tmp_path / "state", file_locks=locks)
    identity = uuid.uuid4().hex
    target = tmp_path / "wrong-source.bin"
    other = tmp_path / "other.bin"
    other.write_bytes(b"abc")
    uploads.begin(BeginUpload(transfer_id=identity, path=str(target),
                              total_bytes=3, sha256=hashlib.sha256(b"abc").hexdigest()))
    uploads.chunk(UploadChunk(transfer_id=identity, offset=0,
                              data_base64=base64.b64encode(b"abc").decode()))
    original = os.link

    def wrong_source(source, destination):
        original(other, destination)

    monkeypatch.setattr(PinnedRegular, "link", wrong_source)
    with pytest.raises(UploadOutcomeUnknown):
        uploads.commit(TransferId(transfer_id=identity))
    assert target.read_bytes() == b"abc"
    assert uploads.status(TransferId(transfer_id=identity))["publication_verified"] is False
