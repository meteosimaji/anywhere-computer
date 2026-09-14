# Common skills (development)

`skills_list` and `skills_read` discover and read local skill packages without
starting Codex or another model. The existing `codex_skills_list` and
`codex_skill_read` remain optional compatibility tools.

Place each package under `~/.agents/skills/<package>/SKILL.md`, or under
`<workspace>/.agents/skills/<package>/SKILL.md` and supply `cwd`. Only these
collection directories and their immediate children are inspected. The home
directory is not recursively searched.

For another collection, pass `roots` as an explicit list of absolute directories.
This selects directories for that request; it does not save a registration. Use
the same `roots` and `cwd` in subsequent reads. To save engine-wide defaults,
call `settings_update` with `key: "skill_roots"` and an array of absolute
collection paths as `value`. Existing directories are validated and canonicalized.
Saved roots replace convention directories when a request omits `roots`; explicit
request roots always override them. Set `value: []` to restore convention discovery.
The setting survives engine restart and applies to all authorized clients of that
engine. Missing saved directories fail rather than silently scanning elsewhere.
Registration does not change remote tool grants. Older engines do not understand
this setting; clear it before downgrading to an older engine.

```json
{"roots": ["/absolute/skills"], "limit": 30}
```

The list contains package directory names, opaque IDs, locations and SHA-256
hashes of SKILL.md. Names are directory names, not parsed YAML frontmatter.
Full metadata and instructions remain in the selected SKILL.md. Read it with:

```json
{
  "roots": ["/absolute/skills"],
  "skill_id": "<returned skill_id>",
  "expected_skill_sha256": "<returned skill_sha256>",
  "relative_path": "SKILL.md"
}
```

Use the same operation with `relative_path: "references/example.md"` or
`"scripts/example.py"` to read supporting text. Paths use `/` on both OSes.
The content and hash are returned; scripts are never executed by this operation.
Binary assets can be handled with existing file tools under the user's request.

Reads are limited to UTF-8 regular files of 64 KiB. Directory traversal,
absolute paths, Windows alternate streams, and symlinks escaping the selected
package are rejected. Links within the package are allowed. Changed SKILL.md
content requires listing again; moving a package changes its ID. A resource has
its own returned hash. The SKILL.md hash is not a digest of every package asset.
Concurrent file replacement is checked during reads; this does not make the
entire collection a transactional snapshot or an OS security sandbox.

At most 16 explicit roots and 5,000 total immediate collection entries are
inspected per request. Invalid packages contribute to `catalog_errors` without
returning their private contents or exception text. Missing default collections
are empty; missing explicit collections are errors. Paginate with `after` and
`next_cursor`; changes during pagination require a new listing when a complete
consistent inventory matters.

New tools need explicit remote grants. Updating the engine does not silently
expand existing saved grants. Skill text cannot grant permissions, override the
connected client's instructions, install missing tools, or authorize execution.
