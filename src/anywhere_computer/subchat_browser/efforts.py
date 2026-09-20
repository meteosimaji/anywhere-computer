"""Experimental traversal for an owned, empty Chat's already open effort control.

The caller supplies real DOM reads and keyboard steps. This does not send prompts,
choose models, reconnect a browser, or establish ownership of an arbitrary page.
"""
from collections.abc import Awaitable, Callable

Read = Callable[[], Awaitable[dict[str, object]]]
Step = Callable[[str], Awaitable[None]]


class EffortUnconfirmed(ValueError):
    pass


def snapshot(value: dict[str, object]) -> tuple[int, int, int, str]:
    numbers = [value.get(key) for key in ("minimum", "maximum", "index")]
    description = value.get("description")
    if (value.get("state") != "effort_observed" or value.get("disabled") is not False
            or any(type(number) is not int for number in numbers)
            or not isinstance(description, str) or not description.strip()):
        raise EffortUnconfirmed("effort snapshot is not confirmed")
    minimum, maximum, index = numbers
    assert isinstance(minimum, int) and isinstance(maximum, int) and isinstance(index, int)
    if not minimum <= index <= maximum or maximum - minimum >= 32:
        raise EffortUnconfirmed("effort range is unsupported")
    return minimum, maximum, index, description


async def move_effort(
    read: Read, step: Step, original: tuple[int, int, int, str], target: int,
) -> tuple[int, int, int, str]:
    minimum, maximum = original[:2]
    if not minimum <= target <= maximum:
        raise EffortUnconfirmed("target position is outside the observed range")
    current = snapshot(await read())
    if current[:2] != original[:2]:
        raise EffortUnconfirmed("effort range changed")
    for _ in range(maximum - minimum):
        if current[2] == target:
            break
        direction = 1 if current[2] < target else -1
        await step("ArrowRight" if direction == 1 else "ArrowLeft")
        updated = snapshot(await read())
        if updated[:2] != original[:2] or updated[2] != current[2] + direction:
            raise EffortUnconfirmed("keyboard step was not confirmed")
        current = updated
    if current[2] != target:
        raise EffortUnconfirmed("target position was not confirmed")
    return current


async def collect_efforts(read: Read, step: Step) -> dict[str, object]:
    observed: dict[str, object] = {}
    try:
        observed = await read()
        original = snapshot(observed)
    except (EffortUnconfirmed, ConnectionError, TimeoutError) as error:
        state = observed.get("state")
        known = {"menu_unconfirmed", "effort_control_unconfirmed",
                 "unsupported_effort_control", "effort_description_unconfirmed",
                 "effort_observed"}
        return {"state": "efforts_unconfirmed", "failure_stage": "initial_observation",
                "observation_state": state if isinstance(state, str) and state in known else None,
                "restored": None, "input_dispatched": False,
                "error_type": type(error).__name__}
    minimum, maximum, start, initial_description = original

    positions: list[dict[str, object]] = []
    restored = False
    try:
        try:
            for index in range(minimum, maximum + 1):
                current = await move_effort(read, step, original, index)
                positions.append({"index": index, "description": current[3]})
        finally:
            # Restoration is verified, not inferred from the key dispatch receipt.
            current = await move_effort(read, step, original, start)
            restored = current[3] == initial_description
    except (EffortUnconfirmed, ConnectionError, TimeoutError) as error:
        return {"state": "efforts_unconfirmed", "restored": restored,
                "error_type": type(error).__name__}
    if not restored:
        return {"state": "efforts_unconfirmed", "restored": False}
    return {"state": "efforts_observed", "positions": positions,
            "original_index": start, "restored": True}
