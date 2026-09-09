"""Progressive literal search with filters, limits and cooperative cancellation."""

import asyncio
import fnmatch
import io
import json
import os
import re
import threading
import uuid
import zipfile
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import JsonValue

from .documents import read_document
from .files import absolute_path, read_bytes
from .models import ReadDocument, SearchPage, StartSearch
from .regex_worker import regex_line_numbers

SEARCH_OUTPUT_LIMIT = 16 * 1024 * 1024


@dataclass
class Search:
    search_id: str
    results: list[JsonValue] = field(default_factory=list)
    state: str = "running"
    skipped: int = 0
    truncated: bool = False
    visited_files: int = 0
    directory_errors: int = 0
    limit_reason: str | None = None
    result_bytes: int = 0
    cancelled: threading.Event = field(default_factory=threading.Event)
    task: asyncio.Task[None] | None = None


class Searches:
    def __init__(self) -> None:
        self.searches: dict[str, Search] = {}

    def start(self, args: StartSearch) -> dict[str, JsonValue]:
        if args.kind == "documents" and args.context_lines:
            raise ValueError("Document search returns entry locations, not context lines")
        if args.mode == "regex":
            try:
                re.compile(args.pattern)
            except re.error:
                raise ValueError("Invalid regular expression") from None
        root = absolute_path(args.path)
        if not root.is_dir():
            raise ValueError("Search root must be a directory")
        if len(self.searches) >= 32:
            for key, entry in list(self.searches.items()):
                if entry.state != "running":
                    del self.searches[key]
                    break
            else:
                raise ValueError("Too many active searches")
        search = Search(uuid.uuid4().hex)
        self.searches[search.search_id] = search
        search.task = asyncio.create_task(self._run_with_deadline(search, root, args))
        return {"search_id": search.search_id, "state": search.state}

    async def _run_with_deadline(self, search: Search, root: Path, args: StartSearch) -> None:
        try:
            await asyncio.wait_for(self._run(search, root, args), args.timeout_ms / 1000)
        except TimeoutError:
            search.cancelled.set()
            search.truncated = True
            search.limit_reason = "timeout"
            search.state = "completed"
        except asyncio.CancelledError:
            search.cancelled.set()
            search.state = "cancelled"
            raise

    async def _run(self, search: Search, root: Path, args: StartSearch) -> None:
        pattern = args.pattern.casefold() if args.ignore_case else args.pattern
        filename_glob = args.filename_glob.casefold() if args.ignore_case else args.filename_glob
        excluded = [p.casefold() if args.ignore_case else p for p in args.excluded_directories]

        def directory_error(_: OSError) -> None:
            search.directory_errors += 1

        try:
            for directory, folders, files in os.walk(
                root,
                followlinks=False,
                onerror=directory_error,
            ):
                await asyncio.sleep(0)
                folders[:] = [
                    name
                    for name in folders
                    if (args.include_hidden or not name.startswith("."))
                    and not (Path(directory) / name).is_symlink()
                    and not any(
                        fnmatch.fnmatchcase(
                            name.casefold() if args.ignore_case else name,
                            rule,
                        )
                        for rule in excluded
                    )
                ]
                if len(Path(directory).relative_to(root).parts) >= args.max_depth and folders:
                    folders.clear()
                    search.truncated = True
                    search.limit_reason = "max_depth"
                for name in files:
                    await asyncio.sleep(0)
                    if search.visited_files >= args.max_files:
                        search.truncated = True
                        search.limit_reason = "max_files"
                        search.state = "completed"
                        return
                    search.visited_files += 1
                    if not args.include_hidden and name.startswith("."):
                        continue
                    path = Path(directory) / name
                    if path.is_symlink():
                        continue
                    subject = name.casefold() if args.ignore_case else name
                    if not fnmatch.fnmatchcase(subject, filename_glob):
                        continue
                    if args.kind == "names":
                        found = (
                            bool(
                                await regex_line_numbers(
                                    name,
                                    args.pattern,
                                    ignore_case=args.ignore_case,
                                    whole_word=args.whole_word,
                                    limit=1,
                                    timeout=600,
                                )
                            )
                            if args.mode == "regex"
                            else self._matches(subject, pattern, args.whole_word)
                        )
                        if found and not self._append_result(search, {"path": str(path)}):
                            return
                    else:
                        try:
                            if args.kind == "documents":
                                if path.suffix.lower() not in {".docx", ".xlsx", ".pptx"}:
                                    continue
                                await self._document_file(search, path, args)
                                if search.state != "running":
                                    return
                            elif args.mode == "regex":
                                text = (await asyncio.to_thread(read_bytes, path)).decode("utf-8")
                                try:
                                    selected = await regex_line_numbers(
                                        text,
                                        args.pattern,
                                        ignore_case=args.ignore_case,
                                        whole_word=args.whole_word,
                                        limit=args.max_results - len(search.results),
                                        timeout=600,
                                    )
                                except (OSError, ValueError):
                                    search.state = "failed"
                                    return
                                lines = list(io.StringIO(text, newline=None))
                                for number in selected:
                                    entry: dict[str, JsonValue] = {
                                        "path": str(path),
                                        "line": number,
                                        "text": lines[number - 1][:2000],
                                    }
                                    if args.context_lines:
                                        entry["before"] = [
                                            {"line": i + 1, "text": lines[i][:2000]}
                                            for i in range(
                                                max(0, number - 1 - args.context_lines), number - 1
                                            )
                                        ]
                                        entry["after"] = [
                                            {"line": i + 1, "text": lines[i][:2000]}
                                            for i in range(
                                                number, min(len(lines), number + args.context_lines)
                                            )
                                        ]
                                    if not self._append_result(search, entry):
                                        return
                            else:
                                await self._literal_file(search, path, pattern, args)
                                if search.limit_reason == "output_bytes":
                                    return
                        except (OSError, UnicodeError, ValueError, zipfile.BadZipFile):
                            search.skipped += 1
                    if len(search.results) >= args.max_results:
                        search.truncated = True
                        search.limit_reason = "max_results"
                        search.state = "completed"
                        return
            search.state = "completed"
        except asyncio.CancelledError:
            search.state = "cancelled"
            raise
        except (OSError, ValueError):
            search.state = "failed"

    async def _literal_file(
        self, search: Search, path: Path, pattern: str, args: StartSearch
    ) -> None:
        matches = await asyncio.to_thread(
            self._file_matches,
            path,
            pattern,
            args.ignore_case,
            args.max_results - len(search.results),
            args.whole_word,
            search.cancelled,
            args.context_lines,
        )
        for entry in matches:
            if not self._append_result(search, entry):
                break

    async def _document_file(self, search: Search, path: Path, args: StartSearch) -> None:
        first = await asyncio.to_thread(read_document, ReadDocument(path=str(path)))
        sections: list[str | None] = [None]
        if first["format"] == "xlsx":
            raw_sections = first["sections"]
            assert isinstance(raw_sections, list)
            sections = [str(item["name"]) for item in raw_sections if isinstance(item, dict)]
        for section in sections:
            offset = 0
            while True:
                page = await asyncio.to_thread(read_document, ReadDocument(
                    path=str(path), section=section, offset=offset, limit=100,
                ))
                if page["sha256"] != first["sha256"]:
                    raise ValueError("Document changed during search")
                entries = page["entries"]
                assert isinstance(entries, list)
                for entry in entries:
                    if not isinstance(entry, dict):
                        continue
                    if entry.get("text_truncated"):
                        search.truncated = True
                        search.limit_reason = "document_text_limit"
                    text = " ".join(str(entry[key]) for key in ("text", "value", "formula")
                                    if entry.get(key) is not None)
                    if args.mode == "regex":
                        try:
                            matched = bool(await regex_line_numbers(
                                text, args.pattern, ignore_case=args.ignore_case,
                                whole_word=args.whole_word, limit=1, timeout=600,
                            ))
                        except (OSError, ValueError):
                            search.state = "failed"
                            return
                    else:
                        matched = self._matches(
                            text.casefold() if args.ignore_case else text,
                            args.pattern.casefold() if args.ignore_case else args.pattern,
                            args.whole_word,
                        )
                    if matched:
                        result: dict[str, JsonValue] = {"path": str(path), "text": text[:2000],
                            "document_location": entry, "source_sha256": first["sha256"]}
                        if not self._append_result(search, result):
                            return
                        if len(search.results) >= args.max_results:
                            search.state = "completed"
                            search.truncated = True
                            search.limit_reason = "max_results"
                            return
                    await asyncio.sleep(0)
                if not page["truncated"]:
                    break
                next_offset = page["next_offset"]
                if not isinstance(next_offset, int) or next_offset <= offset:
                    raise ValueError("Document cursor did not advance")
                offset = next_offset

    @staticmethod
    def _append_result(search: Search, entry: JsonValue) -> bool:
        size = len(json.dumps(entry, ensure_ascii=False).encode("utf-8"))
        if search.result_bytes + size > SEARCH_OUTPUT_LIMIT:
            search.truncated = True
            search.limit_reason = "output_bytes"
            search.state = "completed"
            return False
        search.results.append(entry)
        search.result_bytes += size
        return True

    @staticmethod
    def _matches(subject: str, pattern: str, whole_word: bool) -> bool:
        offset = subject.find(pattern)
        while offset >= 0:
            end = offset + len(pattern)
            if not whole_word or (
                (offset == 0 or not (subject[offset - 1].isalnum() or subject[offset - 1] == "_"))
                and (end == len(subject) or not (subject[end].isalnum() or subject[end] == "_"))
            ):
                return True
            offset = subject.find(pattern, offset + 1)
        return False

    @staticmethod
    def _file_matches(
        path: Path,
        pattern: str,
        ignore_case: bool,
        limit: int,
        whole_word: bool,
        cancelled: threading.Event,
        context_lines: int = 0,
    ) -> list[JsonValue]:
        if cancelled.is_set():
            return []
        # Bound actual bytes read, including when a file grows after stat. fstat
        # and O_NONBLOCK also reject special files without blocking on a FIFO.
        text = read_bytes(path).decode("utf-8")
        matches: list[JsonValue] = []
        with io.StringIO(text, newline=None) as stream:
            lines = enumerate(stream, 1)
            upcoming = deque(next(lines, None) for _ in range(context_lines + 1))
            before: deque[tuple[int, str]] = deque(maxlen=context_lines)
            while upcoming:
                current = upcoming.popleft()
                if current is None:
                    break
                number, line = current
                if cancelled.is_set():
                    break
                subject = line.casefold() if ignore_case else line
                if Searches._matches(subject, pattern, whole_word):
                    entry: dict[str, JsonValue] = {
                        "path": str(path),
                        "line": number,
                        "text": line[:2000],
                    }
                    if context_lines:
                        entry["before"] = [{"line": n, "text": value[:2000]} for n, value in before]
                        entry["after"] = [
                            {"line": item[0], "text": item[1][:2000]}
                            for item in upcoming
                            if item is not None
                        ]
                    matches.append(entry)
                    if len(matches) >= limit:
                        break
                before.append((number, line[:2000]))
                upcoming.append(next(lines, None))
        return matches

    def get(self, search_id: str) -> Search:
        if search_id not in self.searches:
            raise ValueError("Search not found on this agent instance")
        return self.searches[search_id]

    def page(self, args: SearchPage) -> dict[str, JsonValue]:
        search = self.get(args.search_id)
        page: list[JsonValue] = []
        page_bytes = 0
        for result in search.results[args.cursor : args.cursor + args.limit]:
            size = len(json.dumps(result, ensure_ascii=False).encode("utf-8"))
            if page and page_bytes + size > 512000:
                break
            page.append(result)
            page_bytes += size
        return {
            "search_id": search.search_id,
            "state": search.state,
            "results": page,
            "next_cursor": args.cursor + len(page),
            "total": len(search.results),
            "skipped": search.skipped,
            "truncated": search.truncated,
            "visited_files": search.visited_files,
            "directory_errors": search.directory_errors,
            "limit_reason": search.limit_reason,
        }

    async def stop(self, search_id: str) -> dict[str, JsonValue]:
        search = self.get(search_id)
        search.cancelled.set()
        if search.task and not search.task.done():
            search.task.cancel()
            await asyncio.gather(search.task, return_exceptions=True)
            search.state = "cancelled"
        return {"search_id": search_id, "state": search.state}

    async def close(self) -> None:
        await asyncio.gather(*(self.stop(key) for key in self.searches))
