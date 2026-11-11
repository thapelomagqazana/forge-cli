# Dry-Run Plan Contract (v1.0.0)

`forge create --dry-run` produces a **plan** — a complete,
machine-readable description of what `forge create` *would* do —
without touching the destination filesystem. This document is the
contract for that plan's JSON output.

**Current version:** `1.0.0`
**Canonical source:** `src/forge/dryrun/plan.py` (`GenerationPlan`)
**Renderer:** `src/forge/dryrun/renderers/json.py` (`render_json`)
**Golden fixture:** `tests/fixtures/dryrun/golden-plan.json`
**Regenerate:** `python -m tests.fixtures.dryrun._regenerate`

---

## Table of contents

1. [Invocation](#invocation)
2. [The two output modes](#the-two-output-modes)
3. [JSON envelope](#json-envelope)
4. [Field reference](#field-reference)
5. [The four guarantees](#the-four-guarantees)
6. [Exit codes](#exit-codes)
7. [What is *not* part of the contract](#what-is-not-part-of-the-contract)
8. [Change policy](#change-policy)
9. [Example payloads](#example-payloads)
10. [Consuming the plan from a script](#consuming-the-plan-from-a-script)

---

## Invocation

```
forge create <BLUEPRINT> --dry-run [--json] [--overwrite]
    --name <NAME> --var key=value --no-interactive
```

The plan is produced whenever `--dry-run` is passed, *regardless of
`--json`*. `--json` only changes *how* the plan is rendered.

`--dry-run` respects every other input-validation path that
`create` uses:

- **Non-interactive mode** (`--no-interactive` or a non-TTY stdin)
  requires every flag the blueprint needs, exactly as it does for a
  real create. Missing flags fail before the plan is built.
- **Interactive mode** runs the same wizard a real create would run,
  collecting the same values. The plan built in interactive mode is
  identical to the plan built in non-interactive mode *given the
  same inputs*.

A dry-run is **not** a rehearsal of the CLI's argument parsing — it
is a rehearsal of the *filesystem write*. If a real create would
fail during input collection, a dry-run fails the same way.

---

## The two output modes

### Human mode (default)

Rendered by `render_human` in `src/forge/dryrun/renderers/human.py`.
Produces a plain-text report for a terminal user.

**The human format is not a contract.** Its wording, layout,
grouping, and (soon) localisation may change between minor
releases. Automation must consume `--json`.

The human format is guaranteed to include:

- the blueprint id and version,
- the destination path,
- a summary line with four counters,
- the list of files to create (with source and kind),
- the list of files to skip (with the condition that excluded them),
- the list of collisions (with the existing kind),
- any warnings.

A user reading the human output should be able to answer
"what would change if I ran this for real?" without running `--json`.

### JSON mode (`--json`)

Rendered by `render_json` in `src/forge/dryrun/renderers/json.py`.
Produces a single-line, deterministic JSON document on stdout.

**The JSON format is a versioned contract** (this document). Every
guarantee below applies.

---

## JSON envelope

```json
{
  "schema_version": "1.0.0",
  "blueprint_id": "python.fastapi.api",
  "blueprint_version": "1.0.0",
  "destination": "/abs/path/to/project",
  "overwrite": false,
  "files_to_create": [ ... ],
  "files_to_skip":   [ ... ],
  "collisions":      [ ... ],
  "warnings":        [ ... ]
}
```

The top-level keys, their types, and their semantics are fixed for
`schema_version == "1.0.0"`. The set is exactly these nine keys —
no more, no fewer. Consumers should treat unknown keys as a signal
that the plan was produced by a newer version and, if strict
validation is required, refuse to process it.

### `files_to_create[]` — one entry per file the create would write

```json
{
  "source": "templates/main.py.j2",
  "destination": "src/main.py",
  "kind": "render"
}
```

- **`source`**: path to the template or literal file, relative to
  the blueprint directory. Always non-empty.
- **`destination`**: resolved, safe, relative path inside the
  target project. Always non-empty.
- **`kind`**: `"render"` for Jinja templates (the file body will be
  rendered with the resolved variables), `"copy"` for literal files
  (bytes will be copied verbatim — binaries, non-`.j2` sources).

**Ordering:** the list is sorted by `destination` ascending, with
byte-wise comparison (POSIX path semantics, `/` as separator).

### `files_to_skip[]` — one entry per conditional file excluded

```json
{
  "source": "templates/Dockerfile.j2",
  "destination": "Dockerfile",
  "condition": "include_docker == true",
  "reason": "condition evaluated false"
}
```

- **`source`**, **`destination`**: same semantics as
  `files_to_create[]`.
- **`condition`**: the condition text, *verbatim* as written in the
  blueprint (including its whitespace). This is what a user greps
  their YAML for when they want to understand why a file was
  skipped.
- **`reason`**: human-readable explanation. Currently
  `"condition evaluated false"` for every skip; future revisions may
  add other reasons (e.g. `"condition errored"`) without a schema
  bump, because the field is free-form.

**Ordering:** sorted by `destination`, same as `files_to_create`.

**Completeness:** every conditional file whose group's condition
evaluated false appears here. There is no "silently dropped" case —
the skip list is derived by set difference between all conditional
files and the surviving groups, not by trusting the evaluator to
report exclusions.

### `collisions[]` — one entry per destination that already exists

```json
{
  "destination": "pyproject.toml",
  "existing_kind": "file",
  "sources": ["templates/pyproject.toml.j2"]
}
```

- **`destination`**: the colliding destination path.
- **`existing_kind`**: what is currently on disk at that path.
  One of:
  - `"file"` — a regular file. `--overwrite` will replace it (a
    backup is created first).
  - `"dir"` — a directory. `--overwrite` **cannot** resolve this; a
    directory at a file's destination is a structural conflict the
    user must resolve by hand (remove the directory, choose a
    different destination, or change the blueprint).
  - `"symlink"` — a symbolic link. `--overwrite` **cannot** resolve
    this either; symlinks in the destination path are refused by the
    safety layer (ADR-0017).
  - `"other"` — a FIFO, socket, or device node. `--overwrite`
    cannot resolve this; the user must remove it manually.
  - `"unreadable"` — the destination could not be inspected (rare;
    usually a permissions issue on a parent directory).
  - `"missing"` — reserved for a future use where a race condition
    results in a destination that existed at plan time but does not
    at render time. Not currently emitted.
- **`sources`**: every entry in `files_to_create[]` whose
  `destination` matches this collision. Normally a single entry; a
  plan-internal collision (two blueprint files targeting the same
  destination) will show multiple sources, which is diagnostic
  signal that the blueprint itself needs fixing.

**Ordering:** sorted by `destination`.

**Reporting, not raising:** collisions are *data*. The plan reports
them; the caller decides what to do (pass `--overwrite`, change the
destination, or fix the blueprint). This is the primary difference
between the dry-run and the write path — `forge create` (no
`--dry-run`) *raises* on collision (exit code 6), while
`forge create --dry-run` reports collisions in the plan (exit code
7).

### `warnings[]` — non-fatal observations about the destination

```json
[
  "pyproject.toml: parent directory is not writable — Grant write and execute permission on the parent directory, or choose a different destination."
]
```

Each warning is a **single string** that begins with the path the
warning is about, followed by `": "` and a human-readable message
that typically embeds its own hint after `—`. The exact wording is
free-form and may change between minor releases.

Warnings are **informational**. They do not affect exit codes, do
not block the plan, and do not indicate a bug in the blueprint —
they are observations the safety layer made about the *destination
environment*. The three current warning sources are:

- the destination's parent directory is not writable,
- the destination could not be inspected (permission on a parent),
- the destination's root has no existing ancestor to inspect.

**Ordering:** sorted lexically. Determinism matters because the
JSON output is byte-stable across runs; a warning list that
reordered itself would fail every subsequent snapshot test.

**Note on environment-sensitivity:** warnings are the *one* field
whose content depends on the filesystem the plan was built against.
For a *golden fixture* comparison (see `test_json_contract.py`), the
fixture's plan is built with `warnings=[]` cleared, because the
golden file must be reproducible across machines. This is documented
at the fixture level, not at the schema level — the *contract*
allows warnings; the *fixture* pins an empty array for stability.

### `overwrite` — whether `--overwrite` was passed

```json
false
```

The flag as it was passed. Recorded in the plan so:

- the human renderer can describe the plan mode correctly,
- a consumer inspecting JSON can branch without re-plumbing the
  CLI's flag state.

**Semantics:** `overwrite: true` does *not* mean "the plan would
overwrite." It means "the user consented to overwrite, so existing
destinations are not reported as collisions." The two are related
but distinct:

- `overwrite: false, collisions: [...]` — user did not consent;
  they see what would collide.
- `overwrite: true, collisions: []` — user consented; they see
  what would be written but not what would be replaced (the backup
  creation is the write path's job, not the plan's).

### `blueprint_id`, `blueprint_version`

Provenance. The identifier and version of the blueprint the plan was
built from. Used by:

- reproducibility ("which blueprint produced this plan?"),
- error reporting ("the plan for `python.fastapi.api` v1.0.0 shows
  a missing file"),
- future cache/invalidation schemes.

### `destination`

The absolute path of the target project root (after `--output` /
`--name` resolution, and after `resolve()` on the path). Consumers
comparing two plans should compare on this field, not on the raw
CLI arguments — a plan against `./my-project` and a plan against
`/home/user/my-project` are the *same* plan if the path resolves
identically, and the `destination` field reflects that.

### `schema_version`

The version of this document. See [Change policy](#change-policy).

---

## Field reference

| Field | Type | Contract? | Ordering | Notes |
|-------|------|-----------|----------|-------|
| `schema_version` | `string` | **Yes** | — | Literal `"1.0.0"` for this revision. |
| `blueprint_id` | `string` | **Yes** | — | Non-empty. |
| `blueprint_version` | `string` | **Yes** | — | SemVer per the schema. |
| `destination` | `string` | **Yes** | — | Absolute path. |
| `overwrite` | `boolean` | **Yes** | — | Whether the flag was passed. |
| `files_to_create` | `array<object>` | **Yes** | Sorted by `destination` | Each item has `source`, `destination`, `kind`. |
| `files_to_skip` | `array<object>` | **Yes** | Sorted by `destination` | Each item has `source`, `destination`, `condition`, `reason`. |
| `collisions` | `array<object>` | **Yes** | Sorted by `destination` | Each item has `destination`, `existing_kind`, `sources`. |
| `warnings` | `array<string>` | Presence | Lexically sorted | Content is environment-sensitive. |

The distinction between "**Yes**" and "**Presence**" for the
contract column:

- **Yes**: field exists, has the documented type, and appears in
  every plan.
- **Presence**: field exists, has the documented type, and appears
  in every plan — but its *content* may vary by environment.
  Consumers treating `warnings` as a diagnostic should expect
  differing values across machines; consumers treating it as a
  structural field will be frustrated.

---

## The four guarantees

Every JSON dry-run output satisfies all four. Each has a
corresponding test in `tests/unit/dryrun/test_json_contract.py`.

### 1. Deterministic

Given the same plan (same inputs, same blueprint, same filesystem
state), `render_json` produces **byte-identical** output across
runs, across machines, across Python versions.

This is achieved by:

- `sort_keys=True` on every `json.dumps` call (sorts all mappings
  recursively),
- `separators=(",", ":")` (no whitespace variance),
- lists pre-sorted by the service (`files_to_create`,
  `files_to_skip`, `collisions`, `warnings`),
- no timestamps, random IDs, or other sources of nondeterminism in
  the DTO.

A consumer can hash the JSON for change detection (e.g. `sha256sum`
a plan) and rely on the hash being stable for identical inputs.

**Test:** `test_render_is_deterministic` and
`test_render_is_deterministic_across_rebuilt_plans`.

### 2. Stable schema

The envelope's `schema_version` is the literal string `"1.0.0"`.
The nine top-level keys are fixed. Item fields are fixed for each
list.

Adding a new field to an item, adding a new list, or renaming any
field requires a `schema_version` bump and a corresponding
regeneration of the golden fixture. Removing a field is a breaking
change and requires a **major** version bump (see [Change
policy](#change-policy)).

**Test:** `test_envelope_has_expected_top_level_keys`,
`test_schema_version_is_literal_1_0_0`,
`test_item_shapes_have_stable_field_names`.

### 3. ANSI-free

The JSON output contains **no** ANSI escape sequences. Colour, if
any, is applied *after* the JSON is produced (see
`forge.cli.ui.emit`) — the data path itself is pure.

This is a stronger guarantee than "no colour codes": any escape
sequence (colour, cursor movement, OSC, DCS) would be a regression.
Consumers piping `--json` through `jq`, `python -m json.tool`, or a
dashboard will never see raw control bytes in the data.

**Test:** `test_no_ansi_escape_sequences`.

### 4. Unicode-faithful

Non-ASCII characters appear **literally** in the JSON output, not
escaped as `\uXXXX`. A blueprint with a `café` or `文件` or `🚀` in
its file tree produces JSON that contains those exact characters.

This is achieved by `ensure_ascii=False` on `json.dumps`. The
literal form is:

- **greppable** — a user searching for a filename finds it,
- **diffable** — `git diff` of two plans shows the character, not
  its escape code,
- **smaller** — 4 bytes for most CJK characters vs 6 for the
  escape, 4 vs 8 for non-BMP characters.

The output is still valid JSON (RFC 8259 permits non-ASCII in
UTF-8-encoded JSON), and the subprocess writes UTF-8 bytes.

**Test:** `test_non_ascii_characters_are_emitted_literally`,
`test_unicode_round_trips_through_parsing`.

---

## Exit codes

`forge create --dry-run` returns one of the following exit codes.
These are part of the CLI's public contract (see ADR-0013).

| Exit | Meaning |
|------|---------|
| `0` | Plan succeeded, no collisions. |
| `1` | User error: missing required flag, invalid blueprint variable, cancellation, condition evaluation error. |
| `2` | Blueprint error: blueprint not found, malformed, incompatible. |
| `5` | Internal error: an unexpected exception (a Forge bug — the user should report it). |
| `7` | Plan succeeded, but **collisions were detected**. The JSON was emitted; the exit code signals "review before running for real." |

Note the distinction:

- **`6`** is reserved for the *write* path (`forge create` without
  `--dry-run`) when a collision *prevents* the write. A dry-run
  never returns 6 — collisions are reported *in* the plan (exit 7)
  rather than *raised* (exit 6).

- **`7`** exists so a CI script can gate on "would this create
  conflict?" without actually running `forge create`:

  ```bash
  forge create my.blueprint --dry-run --json > /tmp/plan.json
  case $? in
    0) echo "safe to proceed" ;;
    7) echo "collisions found; review /tmp/plan.json" ; exit 1 ;;
    *) echo "unexpected error"; exit 1 ;;
  esac
  ```

- **`5`** is emitted when the *plan itself* cannot be built (e.g.
  a bug in the dry-run service). This is distinct from a *user
  error* (1) or a *blueprint error* (2) — 5 means "the tool is
  broken, not your input."

---

## What is *not* part of the contract

The following are explicitly **not** guaranteed and may change
without a schema bump:

- **The human format's layout** (column widths, wording, emoji,
  grouping). Only the JSON is contracted.
- **The `warnings[]` content** — text may be reworded as the safety
  layer evolves.
- **The order of `warnings[]`** across *different* runs — the list
  is lexically sorted within a single plan, but the set of warnings
  can differ by environment (see "Note on environment-sensitivity"
  under `warnings[]`).
- **The exact `source` and `destination` paths** — these depend on
  the blueprint's templates and are not part of the *schema*.
  Consumer code should not assume any specific file is present;
  it should inspect the list.
- **The presence of any specific exit code beyond `0`, `1`, `2`,
  `5`, `7`** — a future revision may add new codes, and
  `forge create --dry-run` will not start returning them without a
  documented change. Code that branches on `$? == 0` or `$? == 7`
  is stable; code that assumes those are the *only* possible codes
  is not.

---

## Change policy

This document and the schema it describes are covered by the
[ADR-0012 output-contract policy](../../adr/0012-cli-output-contracts-and-renderers.md)
and [ADR-0018 dry-run contract](../../adr/0018-dry-run-is-a-pure-plan-builder.md).

### Additive changes (no schema bump)

Adding a new field to an item in `files_to_create[]`,
`files_to_skip[]`, or `collisions[]` is **non-breaking**, but
requires:

- a corresponding field in the `src/forge/dryrun/plan.py` DTO,
- a `Golden-plan.json` regeneration (`python -m
  tests.fixtures.dryrun._regenerate`),
- a change to `test_item_shapes_have_stable_field_names` to add the
  new field to the expected set,
- a note in this document's changelog.

The `schema_version` **stays** `"1.0.0"` for additive changes; the
field `schema_version` is the *major.minor* envelope version, not a
per-field version.

### Breaking changes (schema bump required)

Removing a field, renaming a field, changing a field's type, or
adding a field that is required (missing from older plans) is a
**breaking change** and requires:

- a new `schema_version` value (e.g. `"2.0.0"` for a removal;
  `"1.1.0"` for a rename if the old field is retained as
  deprecated),
- a new section in this document,
- a migration note for consumers,
- a `docs/adr/` entry documenting the change and its motivation.

### Deprecation

Any field planned for removal will first be marked deprecated in
this document (and in the DTO's docstring) for at least one **minor**
release. A deprecated field continues to appear in plans (with its
current semantics) until the next **major** version, at which point
it is removed and the `schema_version` is bumped accordingly.

---

## Example payloads

### Clean plan (no collisions)

```json
{
  "schema_version": "1.0.0",
  "blueprint_id": "python.fastapi.api",
  "blueprint_version": "1.0.0",
  "destination": "/home/user/projects/my-api",
  "overwrite": false,
  "files_to_create": [
    {"source": "templates/pyproject.toml.j2", "destination": "pyproject.toml", "kind": "render"},
    {"source": "templates/main.py.j2", "destination": "src/main.py", "kind": "render"}
  ],
  "files_to_skip": [
    {
      "source": "templates/Dockerfile.j2",
      "destination": "Dockerfile",
      "condition": "include_docker == true",
      "reason": "condition evaluated false"
    }
  ],
  "collisions": [],
  "warnings": []
}
```

### Plan with a collision

```json
{
  "schema_version": "1.0.0",
  "blueprint_id": "python.fastapi.api",
  "blueprint_version": "1.0.0",
  "destination": "/home/user/projects/my-api",
  "overwrite": false,
  "files_to_create": [
    {"source": "templates/pyproject.toml.j2", "destination": "pyproject.toml", "kind": "render"},
    {"source": "templates/main.py.j2", "destination": "src/main.py", "kind": "render"}
  ],
  "files_to_skip": [],
  "collisions": [
    {
      "destination": "pyproject.toml",
      "existing_kind": "file",
      "sources": ["templates/pyproject.toml.j2"]
    }
  ],
  "warnings": []
}
```

Exit code: `7`.

### Plan with `--overwrite` and an existing file

```json
{
  "schema_version": "1.0.0",
  "blueprint_id": "python.fastapi.api",
  "blueprint_version": "1.0.0",
  "destination": "/home/user/projects/my-api",
  "overwrite": true,
  "files_to_create": [
    {"source": "templates/pyproject.toml.j2", "destination": "pyproject.toml", "kind": "render"},
    {"source": "templates/main.py.j2", "destination": "src/main.py", "kind": "render"}
  ],
  "files_to_skip": [],
  "collisions": [],
  "warnings": []
}
```

Exit code: `0`. The existing `pyproject.toml` is not reported as a
collision because `--overwrite` consented to replace it.

### Plan with a Unicode destination

```json
{
  "schema_version": "1.0.0",
  "blueprint_id": "sample.unicode",
  "blueprint_version": "1.0.0",
  "destination": "/tmp/café/文件",
  "overwrite": false,
  "files_to_create": [
    {"source": "templates/🚀.j2", "destination": "docs/café.md", "kind": "render"}
  ],
  "files_to_skip": [],
  "collisions": [],
  "warnings": []
}
```

Non-ASCII characters are emitted literally. The JSON is valid
UTF-8.

---

## Consuming the plan from a script

### Shell

```bash
#!/usr/bin/env bash
set -euo pipefail

forge create python.fastapi.api --name my-api --dry-run --json > plan.json
code=$?

if [ "$code" -eq 7 ]; then
    echo "Collisions detected; review before applying:"
    jq '.collisions' plan.json
    exit 1
elif [ "$code" -ne 0 ]; then
    echo "Dry-run failed (exit $code)"
    exit 1
fi

echo "Files that would be created:"
jq -r '.files_to_create[] | "  + \(.destination)"' plan.json
```

### Python

```python
import json
import subprocess

proc = subprocess.run(
    ["forge", "create", "python.fastapi.api",
     "--name", "my-api", "--dry-run", "--json"],
    capture_output=True, text=True,
)

plan = json.loads(proc.stdout)

if plan["schema_version"] != "1.0.0":
    raise RuntimeError(f"unsupported plan schema: {plan['schema_version']}")

if proc.returncode == 7:
    for collision in plan["collisions"]:
        print(f"would overwrite: {collision['destination']}")

for f in plan["files_to_create"]:
    print(f"{f['kind']}: {f['destination']}")
```

### Reading the exit code before the payload

In both examples above, the exit code is checked *before* the
payload is parsed. This is deliberate:

- The exit code is the *primary* signal — it tells you whether to
  trust the payload at all.
- A `--json` output with exit 1 or 2 may not be valid JSON (a
  `MissingRequiredFlagsError` renders as a message, not JSON). Only
  exit 0 and 7 produce valid JSON plan output.

A future version of the CLI may emit JSON error envelopes on exit
1 and 2 as well — that would be an additive change (new envelope,
same exit codes) and would not require a `schema_version` bump for
the plan itself. But it is not the current contract, so scripts
should not rely on it.

---

## See also

- [`docs/adr/0018-dry-run-is-a-pure-plan-builder.md`](../../adr/0018-dry-run-is-a-pure-plan-builder.md) — the design rationale for the dry-run being a pure plan builder, and the "no filesystem mutations" guarantee.
- [`docs/adr/0012-cli-output-contracts-and-renderers.md`](../../adr/0012-cli-output-contracts-and-renderers.md) — the general policy for versioned CLI output and pure renderers.
- [`docs/adr/0013-exit-codes-as-public-contract.md`](../../adr/0013-exit-codes-as-public-contract.md) — the exit-code contract that `--dry-run` participates in.
- [`src/forge/dryrun/plan.py`](../../../src/forge/dryrun/plan.py) — the DTO that defines the envelope.
- [`tests/fixtures/dryrun/golden-plan.json`](../../../tests/fixtures/dryrun/golden-plan.json) — the byte-stable golden fixture.
