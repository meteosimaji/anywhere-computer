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


async def collect_efforts(read: Read, step: Step) -> dict[str, object]:
    original = snapshot(await read())
    minimum, maximum, start, initial_description = original

    async def move(target: int) -> tuple[int, int, int, str]:
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

    positions: list[dict[str, object]] = []
    restored = False
    try:
        try:
            for index in range(minimum, maximum + 1):
                current = await move(index)
                positions.append({"index": index, "description": current[3]})
        finally:
            # Restoration is verified, not inferred from the key dispatch receipt.
            current = await move(start)
            restored = current[3] == initial_description
    except (EffortUnconfirmed, ConnectionError, TimeoutError) as error:
        return {"state": "efforts_unconfirmed", "restored": restored,
                "error_type": type(error).__name__}
    if not restored:
        return {"state": "efforts_unconfirmed", "restored": False}
    return {"state": "efforts_observed", "positions": positions,
            "original_index": start, "restored": True}
