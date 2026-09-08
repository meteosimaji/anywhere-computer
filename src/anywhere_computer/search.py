"""Progressive literal search with filters, limits and cooperative cancellation."""

import asyncio
import fnmatch
import io
import os
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import JsonValue

from .files import absolute_path, read_bytes
from .models import SearchPage, StartSearch


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
    cancelled: threading.Event = field(default_factory=threading.Event)
    task: asyncio.Task[None] | None = None


class Searches:
    def __init__(self) -> None:
        self.searches: dict[str, Search] = {}

    def start(self, args: StartSearch) -> dict[str, JsonValue]:
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
        search.task = asyncio.create_task(self._run(search, root, args))
        return {"search_id": search.search_id, "state": search.state}

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
                        if self._matches(subject, pattern, args.whole_word):
                            search.results.append({"path": str(path)})
                    else:
                        try:
                            matches = await asyncio.to_thread(
                                self._file_matches,
                                path,
                                pattern,
                                args.ignore_case,
                                args.max_results - len(search.results),
                                args.whole_word,
                                search.cancelled,
                            )
                            search.results.extend(matches)
                        except (OSError, UnicodeError, ValueError):
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
        except OSError:
            search.state = "failed"

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
    ) -> list[JsonValue]:
        if cancelled.is_set():
            return []
        # Bound actual bytes read, including when a file grows after stat. fstat
        # and O_NONBLOCK also reject special files without blocking on a FIFO.
        text = read_bytes(path).decode("utf-8")
        matches: list[JsonValue] = []
        with io.StringIO(text, newline=None) as stream:
            for number, line in enumerate(stream, 1):
                if cancelled.is_set():
                    break
                subject = line.casefold() if ignore_case else line
                if Searches._matches(subject, pattern, whole_word):
                    matches.append({"path": str(path), "line": number, "text": line[:2000]})
                    if len(matches) >= limit:
                        break
        return matches

    def get(self, search_id: str) -> Search:
        if search_id not in self.searches:
            raise ValueError("Search not found on this agent instance")
        return self.searches[search_id]

    def page(self, args: SearchPage) -> dict[str, JsonValue]:
        search = self.get(args.search_id)
        page = search.results[args.cursor : args.cursor + args.limit]
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
