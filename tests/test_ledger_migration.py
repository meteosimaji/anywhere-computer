import sqlite3

import pytest

from anywhere_computer.models import Reply
from anywhere_computer.state import Ledger, require_ledger_compatibility


def legacy(directory, partial=False):
    with sqlite3.connect(directory / "operations.sqlite3") as db:
        db.execute("CREATE TABLE operations (id TEXT PRIMARY KEY, tool TEXT, digest TEXT, "
                   "started REAL, reply TEXT)")
        for index, state in enumerate(["completed", "running"], 1):
            reply = Reply(operation_id=f"{index:032x}", state=state, data={"value": index})
            db.execute("INSERT INTO operations VALUES(?,?,?,?,?)",
                       (reply.operation_id, "fixture", "digest", 1, reply.model_dump_json()))
        if partial:
            db.execute("ALTER TABLE operations ADD COLUMN state TEXT")
            db.execute("ALTER TABLE operations ADD COLUMN result_sha256 TEXT")


def test_interrupted_migration_rolls_back_schema_and_can_retry(tmp_path, monkeypatch):
    legacy(tmp_path)
    original = Ledger._store
    calls = 0

    def interrupted(self, reply):
        nonlocal calls
        original(self, reply)
        calls += 1
        if calls == 1:
            raise RuntimeError("injected migration interruption")

    with monkeypatch.context() as patch:
        patch.setattr(Ledger, "_store", interrupted)
        with pytest.raises(RuntimeError, match="interruption"):
            Ledger(tmp_path)
    with sqlite3.connect(tmp_path / "operations.sqlite3") as db:
        assert len(db.execute("PRAGMA table_info(operations)").fetchall()) == 5
    ledger = Ledger(tmp_path)
    try:
        assert ledger.get(f"{1:032x}").data == {"value": 1}
        assert ledger.get(f"{2:032x}").state == "unknown"
    finally:
        ledger.close()


def test_partial_checkpoint_schema_is_repaired(tmp_path):
    legacy(tmp_path, partial=True)
    ledger = Ledger(tmp_path)
    try:
        assert ledger.get(f"{1:032x}").data == {"value": 1}
        assert ledger.get(f"{2:032x}").state == "unknown"
        assert ledger.connection.execute(
            "SELECT count(*) FROM operations WHERE state IS NULL"
        ).fetchone()[0] == 0
    finally:
        ledger.close()


@pytest.mark.parametrize("partial", [False, True])
def test_runtime_compatibility_is_checked_without_changing_ledger(tmp_path, partial):
    legacy(tmp_path, partial=partial)
    database = tmp_path / "operations.sqlite3"
    original = database.read_bytes()
    if partial:
        with pytest.raises(ValueError, match="outside runtime support"):
            require_ledger_compatibility(tmp_path, maximum=0)
    else:
        assert require_ledger_compatibility(tmp_path, maximum=0) == 0
    assert database.read_bytes() == original
    assert require_ledger_compatibility(tmp_path) == int(partial)


def test_future_schema_refused_before_opening_for_write(tmp_path):
    legacy(tmp_path)
    database = tmp_path / "operations.sqlite3"
    with sqlite3.connect(database) as db:
        db.execute("PRAGMA user_version=2")
    original = database.read_bytes()
    with pytest.raises(ValueError, match="outside runtime support"):
        Ledger(tmp_path)
    assert database.read_bytes() == original
    assert not database.with_name(database.name + "-wal").exists()


def test_incompatible_rollback_preserves_completed_operation(tmp_path):
    from anywhere_computer.models import Request

    ledger = Ledger(tmp_path)
    request = Request(operation_id="a" * 32, tool="fixture", arguments={"action": "write"})
    try:
        assert ledger.claim(request) is None
        ledger.finish(Reply(operation_id=request.operation_id, state="completed",
                            data={"external_effect_recorded": True}))
    finally:
        ledger.close()
    with pytest.raises(ValueError, match="outside runtime support"):
        require_ledger_compatibility(tmp_path, maximum=0)
    resumed = Ledger(tmp_path)
    try:
        duplicate = resumed.claim(request)
        assert duplicate.state == "completed"
        assert duplicate.data == {"external_effect_recorded": True}
    finally:
        resumed.close()
