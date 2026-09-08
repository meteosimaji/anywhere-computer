import json
import sqlite3
import uuid

import pytest

from anywhere_computer import cli
from anywhere_computer.downloads import Downloads
from anywhere_computer.models import BeginDownload, BeginUpload
from anywhere_computer.transfer_admin import list_transfers, release_transfer
from anywhere_computer.uploads import Uploads


def test_missing_inventory_does_not_create_state(tmp_path):
    state = tmp_path / "missing"
    result = list_transfers(state, area="http", kind="download")
    assert not result["registry_exists"] and result["transfers"] == []
    assert not state.exists()
    with pytest.raises(ValueError, match="does not exist"):
        release_transfer(state, area="http", kind="download", storage_id=uuid.uuid4().hex)
    assert not state.exists()


def test_inventory_pages_and_release_in_selected_area(tmp_path):
    source = tmp_path / "source"
    source.write_bytes(b"hello")
    local = Downloads(tmp_path)
    http = Downloads(tmp_path / "http-server" / "engine")
    identities = [f"{i:032x}" for i in range(3)]
    for identity in identities:
        local.begin(BeginDownload(transfer_id=identity, path=str(source)))
    http.begin(BeginDownload(transfer_id=identities[0], path=str(source)))
    first = list_transfers(tmp_path, area="local", kind="download", limit=2)
    assert [row["storage_id"] for row in first["transfers"]] == identities[:2]
    assert first["next_after"] == identities[1]
    last = list_transfers(
        tmp_path, area="local", kind="download", after=first["next_after"], limit=2
    )
    assert len(last["transfers"]) == 1 and last["next_after"] is None
    result = release_transfer(tmp_path, area="http", kind="download", storage_id=identities[0])
    assert result["state"] == "closed" and source.read_bytes() == b"hello"
    assert (
        list_transfers(tmp_path, area="local", kind="download")["transfers"][0]["state"] == "ready"
    )
    assert (
        release_transfer(
            tmp_path,
            area="http",
            kind="download",
            storage_id=identities[0],
        )["state"]
        == "closed"
    )


def test_upload_release_never_resolves_uncertain_publication(tmp_path):
    uploads = Uploads(tmp_path, file_locks=tmp_path / "file-locks")
    identity = uuid.uuid4().hex
    target = tmp_path / "target"
    uploads.begin(
        BeginUpload(transfer_id=identity, path=str(target), total_bytes=0, sha256="0" * 64)
    )
    with sqlite3.connect(uploads.database) as db:
        db.execute("UPDATE uploads SET state='publishing' WHERE id=?", (identity,))
    target.write_bytes(b"keep")
    assert (
        list_transfers(tmp_path, area="local", kind="upload")["transfers"][0]["state"] == "unknown"
    )
    with pytest.raises(ValueError, match="uncertain"):
        release_transfer(tmp_path, area="local", kind="upload", storage_id=identity)
    assert target.read_bytes() == b"keep"
    with sqlite3.connect(uploads.database) as db:
        db.execute("UPDATE uploads SET state='receiving' WHERE id=?", (identity,))
    assert (
        release_transfer(
            tmp_path,
            area="local",
            kind="upload",
            storage_id=identity,
        )["state"]
        == "aborted"
    )
    assert target.read_bytes() == b"keep"


def test_cli_inventory_requires_explicit_area_kind(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["anywhere", "transfers", "--state-dir", str(tmp_path)])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
    monkeypatch.setattr(
        "sys.argv",
        [
            "anywhere",
            "transfers",
            "--state-dir",
            str(tmp_path),
            "--transfer-area",
            "http",
            "--transfer-kind",
            "download",
        ],
    )
    cli.main()
    assert json.loads(capsys.readouterr().out)["registry_exists"] is False


def test_inventory_rejects_unknown_versions_and_invalid_cursor(tmp_path):
    downloads = Downloads(tmp_path)
    with sqlite3.connect(downloads.database) as db:
        db.execute("PRAGMA user_version=99")
    with pytest.raises(ValueError, match="version"):
        list_transfers(tmp_path, area="local", kind="download")
    with pytest.raises(ValueError):
        list_transfers(tmp_path, area="local", kind="download", after="../outside")


def test_old_upload_schema_inventory_does_not_migrate(tmp_path):
    uploads = Uploads(tmp_path, file_locks=tmp_path / "file-locks")
    identity = uuid.uuid4().hex
    uploads.begin(BeginUpload(
        transfer_id=identity, path=str(tmp_path / "target"), total_bytes=0, sha256="0" * 64,
    ))
    with sqlite3.connect(uploads.database) as db:
        db.execute("ALTER TABLE uploads DROP COLUMN temporary")
        db.execute("PRAGMA user_version=1")
    result = list_transfers(tmp_path, area="local", kind="upload")
    assert result["transfers"][0]["storage_id"] == identity
    assert result["transfers"][0]["staging_path"] is None
    with sqlite3.connect(uploads.database) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
        assert "temporary" not in {row[1] for row in db.execute("PRAGMA table_info(uploads)")}
