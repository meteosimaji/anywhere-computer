import pytest

from anywhere_computer.engine import Engine
from anywhere_computer.models import History, Request


async def test_history_filters_before_limit_and_hides_arguments(tmp_path):
    engine = Engine(tmp_path)
    try:
        for index, tool in enumerate(["files_read", "computer_status", "files_read"], 1):
            await engine.execute(Request(
                operation_id=f"{index:032x}", tool=tool,
                arguments={"path": str(tmp_path / "private-input")} if tool == "files_read" else {},
            ))
            engine.ledger.connection.execute(
                "UPDATE operations SET started=? WHERE id=?", (float(index), f"{index:032x}"),
            )
        engine.ledger.connection.commit()
        rows = engine.ledger.recent(1, tool_name="computer_status", since=2)
        assert len(rows) == 1 and rows[0]["operation_id"] == f"{2:032x}"
        assert engine.ledger.recent(10, tool_name="computer_status", since=2.1) == []
        reply = await engine.execute(Request(
            operation_id="a" * 32, tool="operations_recent",
            arguments={"tool_name": "files_read", "since": 2, "limit": 1},
        ))
        assert reply.state == "completed"
        assert reply.data["operations"][0]["operation_id"] == f"{3:032x}"
        assert "private-input" not in reply.model_dump_json()
        assert engine.ledger.recent(10, tool_name="' OR 1=1 --") == []
    finally:
        await engine.close()


@pytest.mark.parametrize("since", [-1, float("nan"), float("inf")])
def test_history_rejects_invalid_timestamp(since):
    with pytest.raises(ValueError):
        History(since=since)
