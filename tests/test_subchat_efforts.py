import importlib.util
from pathlib import Path

import pytest

from anywhere_computer.subchat_browser.efforts import matches_effort

spec = importlib.util.spec_from_file_location(
    "subchat_efforts", Path(__file__).parents[1] / "scripts/subchat_efforts.py")
assert spec and spec.loader
collector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collector)


@pytest.mark.parametrize('description,label,expected', [
    ('Pro', 'Pro', True),
    ('Pro、5 件中 5 番目。', 'Pro', True),
    ('高、5 件中 5 番目。', 'Pro', False),
    ('Pro extra、5 件中 5 番目。', 'Pro', False),
    ('Pro、5 件中 4 番目。', 'Pro', False),
    ('Pro、6 件中 5 番目。', 'Pro', False),
    ('Pro、5 件中 5 番目。extra', 'Pro', False),
])
def test_effort_label_checks_complete_announcement(description, label, expected):
    assert matches_effort((0, 4, 4, description), label) is expected
    assert matches_effort((2, 6, 6, description), label) is expected


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


@pytest.mark.asyncio
@pytest.mark.parametrize("state", [
    "effort_control_unconfirmed", "effort_description_unconfirmed", "unsupported_effort_control",
])
async def test_initial_observation_failure_returns_without_keyboard_input(state):
    commands = []

    async def read():
        return {"state": state}

    async def step(key):
        commands.append(key)

    result = await collector.collect_efforts(read, step)
    assert result == {
        "state": "efforts_unconfirmed", "failure_stage": "initial_observation",
        "observation_state": state, "restored": None, "input_dispatched": False,
        "error_type": "EffortUnconfirmed",
    }
    assert commands == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["unknown_state", "transport"])
async def test_initial_failure_does_not_expose_peer_text(failure):
    async def read():
        if failure == "transport":
            raise ConnectionError("private peer body")
        return {"state": "private peer body"}

    async def step(key):
        pytest.fail("initial observation failure must not dispatch input")

    result = await collector.collect_efforts(read, step)
    assert result["failure_stage"] == "initial_observation"
    assert result["observation_state"] is None
    assert result["input_dispatched"] is False
    assert result["restored"] is None
    assert "private" not in str(result)
