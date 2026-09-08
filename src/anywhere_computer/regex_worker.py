"""Bounded regex matching in a disposable interpreter, isolated from the agent loop."""

import asyncio
import io
import json
import math
import re
import sys

MAX_REGEX_INPUT = 32 * 1024 * 1024


def run_worker() -> None:
    raw = sys.stdin.buffer.read(MAX_REGEX_INPUT + 1)
    if len(raw) > MAX_REGEX_INPUT:
        raise ValueError("Regex input exceeds limit")
    request = json.loads(raw)
    expression = request["pattern"]
    try:
        compiled = re.compile(expression, re.IGNORECASE if request["ignore_case"] else 0)
    except re.error:
        print(json.dumps({"error": "Invalid regular expression"}))
        return
    found = []
    for number, line in enumerate(io.StringIO(request["text"], newline=None), 1):
        matched = False
        for match in compiled.finditer(line.rstrip("\n")):
            start, end = match.span()
            if not request["whole_word"] or (
                (start == 0 or not (line[start - 1].isalnum() or line[start - 1] == "_"))
                and (end == len(line) or not (line[end].isalnum() or line[end] == "_"))
            ):
                matched = True
                break
        if matched:
            found.append(number)
            if len(found) >= request["limit"]:
                break
    print(json.dumps({"lines": found}))


async def regex_line_numbers(
    text: str, pattern: str, *, ignore_case: bool, whole_word: bool,
    limit: int, timeout: float,
) -> list[int]:
    if (not 1 <= limit <= 10000 or not 1 <= len(pattern) <= 500
            or not math.isfinite(timeout) or not 0 < timeout <= 600):
        raise ValueError("Invalid regex bounds")
    payload = json.dumps({
        "text": text, "pattern": pattern, "ignore_case": ignore_case,
        "whole_word": whole_word, "limit": limit,
    }, ensure_ascii=False).encode("utf-8")
    if len(payload) > MAX_REGEX_INPUT:
        raise ValueError("Regex input exceeds limit")
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "anywhere_computer.regex_worker",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(payload), timeout)
        if process.returncode != 0 or len(output) > 200000:
            raise ValueError("Regex worker failed")
        response = json.loads(output)
        if "error" in response:
            raise ValueError("Invalid regular expression")
        lines = response.get("lines")
        if (not isinstance(lines, list) or len(lines) > limit
                or any(type(line) is not int or line < 1 for line in lines)
                or lines != sorted(set(lines))):
            raise ValueError("Invalid regex worker response")
        return lines
    finally:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        await process.wait()


if __name__ == "__main__":
    run_worker()
