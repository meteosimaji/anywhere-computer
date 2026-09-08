import asyncio
import os
import threading
from pathlib import Path

import pytest

from anywhere_computer.models import SearchPage, StartSearch
from anywhere_computer.search import Searches


async def results(root, **options):
    searches = Searches()
    try:
        started = searches.start(StartSearch(path=str(root), **options))
        entry = searches.get(started["search_id"])
        assert entry.task is not None
        await asyncio.wait_for(entry.task, 5)
        return searches.page(SearchPage(search_id=entry.search_id))
    finally:
        await searches.close()


async def test_search_globs_exclusions_and_unicode_word_boundaries(tmp_path):
    (tmp_path / "main.PY").write_text(
        "Cat\nconcatenate\ncat_name\n(cat)\nStraße\n", encoding="utf-8"
    )
    (tmp_path / "other.txt").write_text("cat", encoding="utf-8")
    generated = tmp_path / "generated-build"
    generated.mkdir()
    (generated / "copy.py").write_text("cat", encoding="utf-8")
    page = await results(
        tmp_path,
        pattern="cat",
        kind="text",
        filename_glob="*.py",
        excluded_directories=["generated-*"],
        whole_word=True,
    )
    assert [row["line"] for row in page["results"]] == [1, 4]
    assert not page["truncated"] and page["state"] == "completed"
    unicode = await results(tmp_path, pattern="STRASSE", kind="text", whole_word=True)
    assert len(unicode["results"]) == 1 and unicode["results"][0]["line"] == 5


async def test_search_depth_and_file_limits_are_reported(tmp_path):
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "needle.txt").touch()
    (tmp_path / "needle.txt").touch()
    shallow = await results(tmp_path, pattern="needle", max_depth=0)
    assert len(shallow["results"]) == 1
    assert shallow["truncated"] and shallow["limit_reason"] == "max_depth"
    limited = await results(tmp_path, pattern="needle", max_files=1)
    assert limited["visited_files"] == 1 and limited["limit_reason"] == "max_files"
    assert len(limited["results"]) == 1


async def test_search_hidden_filter_and_result_limit(tmp_path):
    (tmp_path / ".hidden.txt").write_text("needle")
    (tmp_path / "normal.txt").write_text("needle\nneedle\n")
    default = await results(tmp_path, pattern="needle", kind="text", max_results=1)
    assert default["limit_reason"] == "max_results"
    assert len(default["results"]) == 1
    hidden = await results(tmp_path, pattern="hidden", include_hidden=True, filename_glob="*.txt")
    assert len(hidden["results"]) == 1


async def test_search_skips_oversized_and_invalid_utf8(tmp_path):
    with (tmp_path / "large").open("wb") as output:
        output.truncate(16 * 1024**2 + 1)
    (tmp_path / "binary").write_bytes(b"\xff")
    page = await results(tmp_path, pattern="needle", kind="text")
    assert page["skipped"] == 2 and page["results"] == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX FIFO fixture")
async def test_search_rejects_fifo_without_waiting_for_writer(tmp_path):
    os.mkfifo(tmp_path / "pipe")
    page = await results(tmp_path, pattern="needle", kind="text")
    assert page["skipped"] == 1 and page["state"] == "completed"


def test_cancelled_search_worker_does_not_open_file(monkeypatch):
    cancelled = threading.Event()
    cancelled.set()

    def unexpected(_):
        raise AssertionError("Cancelled worker opened a file")

    monkeypatch.setattr("anywhere_computer.search.read_bytes", unexpected)
    assert Searches._file_matches(Path("unused"), "x", True, 1, False, cancelled) == []


async def test_search_stop_marks_cancelled(tmp_path):
    searches = Searches()
    started = searches.start(StartSearch(path=str(tmp_path), pattern="x"))
    stopped = await searches.stop(started["search_id"])
    assert stopped["state"] == "cancelled"
    assert searches.get(started["search_id"]).cancelled.is_set()
    await searches.close()


async def test_context_includes_boundaries_and_overlapping_matches(tmp_path):
    (tmp_path / "text").write_bytes(b"hit\r\nnear\r\nhit\r\nlast")
    page = await results(tmp_path, pattern="hit", kind="text", context_lines=2)
    first, second = page["results"]
    assert first["before"] == []
    assert [row["line"] for row in first["after"]] == [2, 3]
    assert [row["line"] for row in second["before"]] == [1, 2]
    assert second["after"] == [{"line": 4, "text": "last"}]
    assert first["text"] == "hit\n"


async def test_context_is_bounded_and_does_not_consume_match_limit(tmp_path):
    (tmp_path / "text").write_text("x" * 4000 + "\nhit\nafter\nhit")
    page = await results(tmp_path, pattern="hit", kind="text", context_lines=1, max_results=1)
    assert len(page["results"]) == 1
    match = page["results"][0]
    assert len(match["before"][0]["text"]) == 2000
    assert match["after"] == [{"line": 3, "text": "after\n"}]
    assert page["limit_reason"] == "max_results"


async def test_large_context_pages_preserve_every_match(tmp_path):
    (tmp_path / "text").write_text(("hit日本語" * 400 + "\n") * 30, encoding="utf-8")
    searches = Searches()
    try:
        started = searches.start(StartSearch(
            path=str(tmp_path), pattern="hit", kind="text", context_lines=10,
        ))
        search = searches.get(started["search_id"])
        await search.task
        cursor, found, pages = 0, [], 0
        while cursor < 30:
            page = searches.page(SearchPage(search_id=search.search_id, cursor=cursor))
            assert page["next_cursor"] > cursor
            assert len(page["results"]) < 30
            found.extend(row["line"] for row in page["results"])
            cursor = page["next_cursor"]
            pages += 1
        assert found == list(range(1, 31))
        assert pages > 1
    finally:
        await searches.close()
