"""Progressive bounded literal search without blocking the agent event loop."""

import asyncio
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import JsonValue

from .files import MAX_READ_BYTES, absolute_path
from .models import SearchPage, StartSearch


@dataclass
class Search:
    search_id: str
    results: list[JsonValue] = field(default_factory=list)
    state: str = "running"
    skipped: int = 0
    truncated: bool = False
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
        try:
            for directory, folders, files in os.walk(root, followlinks=False):
                folders[:] = [
                    name
                    for name in folders
                    if (args.include_hidden or not name.startswith("."))
                    and not (Path(directory) / name).is_symlink()
                ]
                for name in files:
                    await asyncio.sleep(0)
                    if not args.include_hidden and name.startswith("."):
                        continue
                    path = Path(directory) / name
                    if path.is_symlink():
                        continue
                    if args.kind == "names":
                        subject = name.casefold() if args.ignore_case else name
                        if pattern in subject:
                            search.results.append({"path": str(path)})
                    else:
                        try:
                            matches = await asyncio.to_thread(
                                self._file_matches,
                                path,
                                pattern,
                                args.ignore_case,
                                args.max_results - len(search.results),
                            )
                            search.results.extend(matches)
                        except (OSError, UnicodeError, ValueError):
                            search.skipped += 1
                    if len(search.results) >= args.max_results:
                        search.truncated = True
                        search.state = "completed"
                        return
            search.state = "completed"
        except asyncio.CancelledError:
            search.state = "cancelled"
            raise
        except OSError:
            search.state = "failed"

    @staticmethod
    def _file_matches(path: Path, pattern: str, ignore_case: bool, limit: int) -> list[JsonValue]:
        if not path.is_file() or path.stat().st_size > MAX_READ_BYTES:
            raise ValueError("Unsupported search file")
        matches: list[JsonValue] = []
        with path.open(encoding="utf-8") as stream:
            for number, line in enumerate(stream, 1):
                subject = line.casefold() if ignore_case else line
                if pattern in subject:
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
        }

    async def stop(self, search_id: str) -> dict[str, JsonValue]:
        search = self.get(search_id)
        if search.task and not search.task.done():
            search.task.cancel()
            await asyncio.gather(search.task, return_exceptions=True)
            search.state = "cancelled"
        return {"search_id": search_id, "state": search.state}

    async def close(self) -> None:
        await asyncio.gather(*(self.stop(key) for key in self.searches))
