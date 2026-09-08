import asyncio

import pytest

from anywhere_computer.regex_worker import regex_line_numbers


async def test_regex_real_worker_handles_syntax_and_word_boundaries():
    options = dict(ignore_case=True, whole_word=True, limit=100, timeout=5)
    assert await regex_line_numbers("cat\nconcatenate\nCAT!\ndog", "c[ae]t", **options) == [1, 3]
    assert await regex_line_numbers("first\nsecond", "^second$", **options) == [2]
    with pytest.raises(ValueError, match="Invalid regular"):
        await regex_line_numbers("content", "[", **options)


async def test_regex_pathological_pattern_is_terminated():
    with pytest.raises(TimeoutError):
        await regex_line_numbers("a" * 10000 + "!", "(a+)+$", ignore_case=False,
                                 whole_word=False, limit=1, timeout=0.2)


async def test_regex_cancellation_reaps_child(monkeypatch):
    original = asyncio.create_subprocess_exec
    children = []
    ready = asyncio.Event()

    async def create(*args, **kwargs):
        process = await original(*args, **kwargs)
        children.append(process)
        ready.set()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    task = asyncio.create_task(regex_line_numbers(
        "a" * 10000 + "!", "(a+)+$", ignore_case=False, whole_word=False, limit=1, timeout=30,
    ))
    await asyncio.wait_for(ready.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(children) == 1 and children[0].returncode is not None
