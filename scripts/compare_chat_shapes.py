"""Compare two redacted request shapes: header names and JSON body key names only.

Each input is a JSON object with exactly `headers` and `body_keys`, both lists of
names. Any mapping, value or credential-like string is refused, and input content
is never printed. Sends nothing and reads no credentials. A matching shape does
not prove why a request was accepted or rejected, and a difference does not prove
the cause of a 403.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
from pathlib import Path

MAX_BYTES = 64 * 1024
MAX_NAMES = 200
FIELDS = ('headers', 'body_keys')
NAME = {
    'headers': re.compile(r'[A-Za-z][A-Za-z0-9-]{0,63}\Z'),
    'body_keys': re.compile(r'[A-Za-z_][A-Za-z0-9_]{0,63}\Z'),
}
CREDENTIAL_LIKE = re.compile(r'eyJ|\Asks?[-_]|\Abearer|\d{8,}|[0-9a-fA-F]{20,}', re.IGNORECASE)
MAX_WORD = 24


class ShapeError(ValueError):
    """Carries a fixed, value-free reason."""


def _suspicious(name: str) -> bool:
    return (CREDENTIAL_LIKE.search(name) is not None
            or any(len(word) > MAX_WORD for word in re.split(r'[-_]', name)))


def load_shape(path: Path) -> dict[str, frozenset[str]]:
    def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ShapeError('duplicate JSON field')
            result[key] = value
        return result

    try:
        if not stat.S_ISREG(path.lstat().st_mode):
            raise ShapeError('file must be regular')
        with path.open('rb') as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise ShapeError('file must be regular')
            raw = source.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ShapeError('file is too large')
        data = json.loads(raw, object_pairs_hook=unique)
    except OSError:
        raise ShapeError('file cannot be read') from None
    except ShapeError:
        raise
    except (ValueError, RecursionError):
        raise ShapeError('file is not valid JSON') from None
    if not isinstance(data, dict) or set(data) != set(FIELDS):
        raise ShapeError('expected an object with exactly headers and body_keys')
    shape: dict[str, frozenset[str]] = {}
    for field in FIELDS:
        names = data[field]
        if not isinstance(names, list):
            raise ShapeError(f'{field} must be a list of names, not a mapping or value')
        if len(names) > MAX_NAMES:
            raise ShapeError(f'{field} has too many names')
        seen: set[str] = set()
        for index, name in enumerate(names):
            if not isinstance(name, str) or NAME[field].fullmatch(name) is None:
                raise ShapeError(f'{field}[{index}] is not a plain name')
            if _suspicious(name):
                raise ShapeError(f'{field}[{index}] looks like a credential or value')
            key = name.lower() if field == 'headers' else name
            if key in seen:
                raise ShapeError(f'{field}[{index}] is a duplicate')
            seen.add(key)
        shape[field] = frozenset(seen)
    return shape


def compare(baseline: dict[str, frozenset[str]],
            candidate: dict[str, frozenset[str]]) -> dict[str, dict[str, list[str]]]:
    return {field: {'missing': sorted(baseline[field] - candidate[field]),
                    'added': sorted(candidate[field] - baseline[field])}
            for field in FIELDS}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('baseline', type=Path, help='Shape JSON from the accepted request')
    parser.add_argument('candidate', type=Path, help='Shape JSON from the request to compare')
    args = parser.parse_args(argv)
    shapes = []
    for label, path in (('baseline', args.baseline), ('candidate', args.candidate)):
        try:
            shapes.append(load_shape(path))
        except ShapeError as error:
            print(f'Refused {label}: {error}', file=sys.stderr)
            return 2
    report = compare(*shapes)
    differs = any(report[field][kind] for field in FIELDS for kind in ('missing', 'added'))
    for field in FIELDS:
        for kind in ('missing', 'added'):
            for name in report[field][kind]:
                print(f'{field} {kind}: {name}')
    print('shape differs' if differs else 'shape matches')
    return 1 if differs else 0


if __name__ == '__main__':
    raise SystemExit(main())
