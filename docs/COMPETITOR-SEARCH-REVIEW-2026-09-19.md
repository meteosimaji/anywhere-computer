# Search capacity review

[DesktopCommanderMCP issue #716](https://github.com/wonderwhy-er/DesktopCommanderMCP/issues/716)
reports excessive search memory use and proposes a global count/byte budget.
The reported memory totals and suspected ripgrep cause were not reproduced here.
Anywhere does not use that collector: `search.py` already applies a global result
count, a 16 MiB serialized result budget, file-read bounds, deadlines and bounded
pages. Those controls should be retained rather than replacing the search backend.

Inspecting the complete Anywhere search path nevertheless exposed a different
intermediate allocation: the literal-search worker built its complete list before
the outer collector applied the byte limit. With context lines, this could greatly
exceed the eventual retained output. A small regression fixture with a 500-byte
budget returned 1,000 rows before the fix, where two rows suffice.

The worker now receives the remaining global byte budget, counts UTF-8 JSON bytes
including context and paths, and stops at the first overflow row. That one row is
returned so the existing outer collector reports `output_bytes` and preserves
cursor behavior. Thus the intermediate batch is bounded by the remaining budget
plus one bounded row. This is a serialized-output bound, not a claim that total
Python RSS equals 16 MiB: source text, Python objects and other searches also use
memory. No upstream implementation was copied.

Tests cover pre-fix failure, Unicode/context byte accounting, an exhausted budget,
existing result pagination and the file/search/terminal workflow. Native Windows
verification is left to CI; local success alone does not establish it.
