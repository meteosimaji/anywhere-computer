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


async def test_timeout_preserves_results_and_cancels_pending_search(tmp_path, monkeypatch):
    cancelled = asyncio.Event()

    async def slow_run(search, root, args):
        search.results.append({"path": str(root / "found"), "line": 1, "text": "hit"})
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    searches = Searches()
    monkeypatch.setattr(searches, "_run", slow_run)
    try:
        started = searches.start(StartSearch(path=str(tmp_path), pattern="hit", timeout_ms=20))
        search = searches.get(started["search_id"])
        await asyncio.wait_for(search.task, 2)
        page = searches.page(SearchPage(search_id=search.search_id))
        assert cancelled.is_set() and search.cancelled.is_set()
        assert page["state"] == "completed"
        assert page["truncated"] and page["limit_reason"] == "timeout"
        assert len(page["results"]) == 1
    finally:
        await searches.close()


async def test_regex_search_supports_context_names_and_invalid_pattern(tmp_path):
    (tmp_path / "note12.txt").write_text("before\nitem42\nafter\nitemXX\n")
    page = await results(tmp_path, pattern=r"^item\d+$", mode="regex", kind="text",
                         context_lines=1)
    assert [row["line"] for row in page["results"]] == [2]
    assert page["results"][0]["before"][0]["text"] == "before\n"
    assert page["results"][0]["after"][0]["text"] == "after\n"
    names = await results(tmp_path, pattern=r"note\d+\.txt$", mode="regex")
    assert len(names["results"]) == 1
    with pytest.raises(ValueError, match="Invalid regular"):
        await results(tmp_path, pattern="[", mode="regex")


async def test_regex_search_deadline_terminates_pathological_match(tmp_path):
    (tmp_path / "text").write_text("a" * 10000 + "!")
    page = await results(tmp_path, pattern="(a+)+$", mode="regex", kind="text", timeout_ms=200)
    assert page["state"] == "completed"
    assert page["limit_reason"] == "timeout"


async def test_total_result_capacity_preserves_existing_cursor(tmp_path, monkeypatch):
    import anywhere_computer.search as module

    monkeypatch.setattr(module, "SEARCH_OUTPUT_LIMIT", 500)
    (tmp_path / "text").write_text("hit" + "x" * 200 + "\n" + "hit" + "x" * 200)
    page = await results(tmp_path, pattern="hit", kind="text")
    assert page["limit_reason"] == "output_bytes"
    assert len(page["results"]) == 1 and page["next_cursor"] == 1


@pytest.mark.parametrize("kind", ["names", "text"])
async def test_regex_worker_launch_failure_is_not_a_skipped_file(tmp_path, monkeypatch, kind):
    import anywhere_computer.search as module

    (tmp_path / "text").write_text("hit")

    async def failure(*args, **kwargs):
        raise OSError("synthetic launch failure")

    monkeypatch.setattr(module, "regex_line_numbers", failure)
    page = await results(tmp_path, pattern="hit", mode="regex", kind=kind)
    assert page["state"] == "failed" and page["skipped"] == 0


async def test_search_stop_reaps_live_regex_worker(tmp_path, monkeypatch):
    original = asyncio.create_subprocess_exec
    children = []
    started = asyncio.Event()

    async def create(*args, **kwargs):
        child = await original(*args, **kwargs)
        children.append(child)
        started.set()
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    (tmp_path / "text").write_text("a" * 10000 + "!")
    searches = Searches()
    try:
        entry = searches.start(StartSearch(path=str(tmp_path), pattern="(a+)+$",
                                          mode="regex", kind="text"))
        await asyncio.wait_for(started.wait(), 5)
        stopped = await asyncio.wait_for(searches.stop(entry["search_id"]), 5)
        assert stopped["state"] == "cancelled"
        assert children and all(child.returncode is not None for child in children)
    finally:
        await searches.close()


async def test_document_search_finds_word_paragraph_and_second_worksheet(tmp_path):
    from anywhere_computer.document_writer import create_word, create_workbooks

    (tmp_path / "word.docx").write_bytes(create_word("first\nneedle paragraph"))
    (tmp_path / "book.xlsx").write_bytes(create_workbooks(
        {"First": [["absent"]], "Second": [["needle cell"]]},
    ))
    (tmp_path / "plain.txt").write_text("needle")
    page = await results(tmp_path, pattern="needle", kind="documents")
    assert page["state"] == "completed" and len(page["results"]) == 2
    locations = [entry["document_location"] for entry in page["results"]]
    assert any(location.get("paragraph") == 2 for location in locations)
    assert any(location.get("sheet") == "Second" and location.get("cell") == "A1"
               for location in locations)
    limited = await results(tmp_path, pattern="needle", kind="documents", max_results=1)
    assert len(limited["results"]) == 1 and limited["limit_reason"] == "max_results"


async def test_document_search_reports_partial_text_and_malformed_package(tmp_path):
    from anywhere_computer.document_writer import create_word

    (tmp_path / "long.docx").write_bytes(create_word("x" * 32768 + "needle"))
    (tmp_path / "broken.docx").write_bytes(b"not a zip")
    page = await results(tmp_path, pattern="needle", kind="documents")
    assert not page["results"] and page["skipped"] == 1
    assert page["truncated"] and page["limit_reason"] == "document_text_limit"
    with pytest.raises(ValueError, match="context lines"):
        Searches().start(StartSearch(path=str(tmp_path), pattern="x", kind="documents",
                                    context_lines=1))


async def test_document_regex_worker_failure_is_not_a_skipped_file(tmp_path, monkeypatch):
    from anywhere_computer.document_writer import create_word

    async def fail(*args, **kwargs):
        raise OSError("synthetic unavailable worker")

    (tmp_path / "word.docx").write_bytes(create_word("needle"))
    monkeypatch.setattr("anywhere_computer.search.regex_line_numbers", fail)
    page = await results(tmp_path, pattern="needle", kind="documents", mode="regex")
    assert page["state"] == "failed" and page["skipped"] == 0


async def test_word_table_search_roundtrips_location_across_document_pages(tmp_path):
    import hashlib
    import zipfile

    from anywhere_computer.documents import REL, WORD, read_document
    from anywhere_computer.models import ReadDocument

    def paragraph(text):
        return f'<w:p><w:r><w:t>{text}</w:t></w:r></w:p>'

    path = tmp_path / "表検索.docx"
    relation = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    body = paragraph("plain") * 101
    body += ('<w:tbl><w:tr><w:tc>' + paragraph("outer")
             + '<w:tbl><w:tr><w:tc>' + paragraph("needle 日本語")
             + '</w:tc></w:tr></w:tbl></w:tc></w:tr></w:tbl>')
    with zipfile.ZipFile(path, "w") as package:
        package.writestr("_rels/.rels", f'<Relationships xmlns="{REL}">'
                         f'<Relationship Id="main" Type="{relation}/officeDocument" '
                         'Target="word/document.xml"/></Relationships>')
        package.writestr("word/document.xml", f'<w:document xmlns:w="{WORD[1:-1]}">'
                         f'<w:body>{body}</w:body></w:document>')
    before = path.read_bytes()
    page = await results(tmp_path, pattern="needle", kind="documents")
    assert page["state"] == "completed" and not page["truncated"]
    assert len(page["results"]) == 1
    match = page["results"][0]
    location = match["document_location"]
    assert location == {"paragraph": 103, "text": "needle 日本語", "table": 2,
                        "row_index": 1, "cell_index": 1}
    reread = read_document(ReadDocument(path=str(path), offset=location["paragraph"] - 1, limit=1))
    assert reread["entries"] == [location]
    assert reread["sha256"] == match["source_sha256"] == hashlib.sha256(before).hexdigest()
    assert path.read_bytes() == before
