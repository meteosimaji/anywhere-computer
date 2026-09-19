import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "subchat_efforts", Path(__file__).parents[1] / "scripts/subchat_efforts.py")
assert spec and spec.loader
collector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collector)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [
    None, "dropped_key", "range_change", "lost_read", "description_change",
])
async def test_traversal_restores_or_reports_unconfirmed(failure):
    position = 3
    maximum = 6
    commands = []
    failed = False

    async def read():
        nonlocal failed
        if failure == "lost_read" and commands and not failed:
            failed = True
            raise ConnectionError("private transport details")
        return {"state": "effort_observed", "minimum": 0, "maximum": maximum,
                "index": position, "description": (f"Changed {position}"
                    if failure == "description_change" and commands else f"Choice {position}"),
                "disabled": False}

    async def step(key):
        nonlocal position, maximum, failed
        commands.append(key)
        if not failed and failure in {"dropped_key", "range_change"}:
            failed = True
            if failure == "range_change":
                maximum = 7
            return
        position += 1 if key == "ArrowRight" else -1

    result = await collector.collect_efforts(read, step)
    if failure is None:
        assert result == {"state": "efforts_observed", "original_index": 3, "restored": True,
                          "positions": [{"index": n, "description": f"Choice {n}"}
                                        for n in range(7)]}
        assert position == 3
    else:
        assert result["state"] == "efforts_unconfirmed"
        assert "positions" not in result
        assert "private" not in str(result)
        assert result["restored"] is (failure not in {"range_change", "description_change"})
        assert position == 3
