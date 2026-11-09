# `forge validate --json` — Output Contract (v1.0.0)

**Status:** stable. This document is a *contract*: the JSON emitted by
`forge validate --json` is consumed by automation, and the shape it
describes is guaranteed until a major version bump.

**Audience:** anyone writing a script, CI job, editor integration, or
pipeline step that parses `forge validate --json`.

**Companion artefacts:**

| Artefact | Purpose |
|----------|---------|
| `src/forge/cli/schemas/validate_output.py` | The DTO that defines the shape at construction time |
| `src/forge/cli/renderers/validate_json.py` | The pure serialiser that turns the DTO into bytes |
| `tests/unit/cli/test_validate_json.py` | Behavioural tests: the envelope satisfies its promises |
| `tests/contract/test_validate_output_contract.py` | Snapshot tests: the envelope's shape is frozen |
| `tests/fixtures/validate/valid.json` | Golden fixture: the exact bytes of a success envelope |
| `tests/fixtures/validate/invalid.json` | Golden fixture: the exact bytes of a failure envelope |

---

## Table of contents

1. [Invocation](#invocation)
2. [Exit codes](#exit-codes)
3. [Streams](#streams)
4. [Envelope](#envelope)
5. [Error entries](#error-entries)
6. [Guarantees](#guarantees)
7. [Non-guarantees](#non-guarantees)
8. [Versioning](#versioning)
9. [Examples](#examples)
10. [Consumer recipes](#consumer-recipes)

---

## Invocation

```sh
forge validate <PATH> --json
```

`PATH` is either:

- a **directory** containing a `blueprint.yaml` file, or
- the **file** `blueprint.yaml` itself.

Both forms produce the same envelope. The `source` field echoes the
path the caller supplied (resolved to an absolute path by Typer; see
[The `source` field](#the-source-field)).

The `--json` flag has no short alias, on purpose: `-j` is reserved for
a future `--jobs` option (parallel validation), and giving `--json` a
short flag now would force a rename later. Spell it out.

---

## Exit codes

Exit codes are the **first** signal a consumer reads. They are stable
across modes and independent of the envelope's contents.

| Code | Name | Meaning |
|------|------|---------|
| `0`  | `SUCCESS` | The blueprint validated cleanly. `valid: true`. |
| `1`  | `USER_ERROR` | The blueprint is invalid. `valid: false`. |
| `2`  | `BLUEPRINT_ERROR` | The **invocation** is wrong (directory has no `blueprint.yaml`). Not a validation result — see [Usage errors](#usage-errors-are-not-in-the-envelope). |
| `5`  | `INTERNAL_ERROR` | The **tool** crashed. Not a validation result — see [Internal errors](#internal-errors-are-not-in-the-envelope). |

Exit `3` and `4` (`FILESYSTEM_CONFLICT`, `VERIFICATION_FAILED`) are
defined in `forge.core.exceptions.ExitCode` but **are not emitted by
`forge validate`**: validation does not touch the filesystem outside
of reads, and it does not run a blueprint's verification contract.
A caller that branches on `$?` can treat `0/1` as "the answer" and
`2/5` as "the answer is not a validation result".

**The exit code is authoritative.** A consumer that reads `payload["valid"]`
without also checking the exit code will be right in the `0/1` cases
and wrong in the `2/5` cases (where no envelope is emitted at all —
see below).

---

## Streams

`forge validate --json` writes to **stdout only**. Nothing writes to
stderr in JSON mode.

| Case | stdout | stderr |
|------|--------|--------|
| Valid blueprint | Full envelope | *(empty)* |
| Invalid blueprint | Full envelope | *(empty)* |
| Usage error (no manifest) | *(empty)* | Human-readable prose |
| Internal error | *(empty)* | Human-readable prose |

This stream discipline exists so that:

```sh
forge validate --json ./my-blueprint 2>/dev/null | jq .
```

is a valid pipeline: nothing the consumer cares about ever lands on
stderr, so a `2>/dev/null` does not lose diagnostics.

A test that asserts `stderr == ""` in JSON mode is proving this
invariant. It is not a convenience check.

---

## Envelope

Every successful invocation — valid or invalid blueprint — emits the
same top-level object. **The key set is fixed.** It does not vary
with validity; nullability is expressed with `null`, not by omitting
keys.

### Top-level keys

| Key | Type | Description |
|-----|------|-------------|
| `schema_version` | `string` | Contract version. Pinned to `"1.0.0"` for this document. |
| `valid` | `boolean` | `true` iff `errors` is empty. Emitted explicitly, not derived. |
| `source` | `string` | The path the caller supplied (resolved to absolute by Typer). |
| `blueprint_id` | `string \| null` | The blueprint's `id`, or `null` when the loader could not extract one (i.e. `valid: false`). |
| `version` | `string \| null` | The blueprint's `version`, or `null` (same condition). |
| `errors` | `array` | Zero entries on success, one or more on failure. Never `null`. |

### The `valid` field

`valid` is the boolean shadow of `len(errors) == 0`. It exists so
consumers never have to compute it, and so an accidental invariant
break is visible in diffs. The two representations must always agree:

```
payload["valid"] === (payload["errors"] === [])
```

A consumer that reads only `valid` and one that reads only `errors`
must reach the same verdict. If they ever disagree, that is a bug in
the tool, not an ambiguity in the contract.

### The `source` field

`source` echoes the path the caller supplied, **resolved to an
absolute path** by Typer's `resolve_path=True` on the `path`
argument. This means:

- A caller passing `./my-blueprint` sees `/abs/path/to/my-blueprint`.
- A caller passing `./my-blueprint/blueprint.yaml` sees
  `/abs/path/to/my-blueprint/blueprint.yaml`.

The resolution is intentional: it makes the field useful for
correlating a payload with the file it came from, regardless of the
consumer's working directory. A caller that needs the raw input
already has it — it is the argument the caller passed.

### The `blueprint_id` and `version` fields

Both are present in **every** envelope; both are `null` on failure.
The reason they are nullable rather than optional is structural:

- Omitting a key forces consumers to write `payload.get("blueprint_id")`
  instead of `payload["blueprint_id"]` — the small difference between
  an *optional* contract and a *nullable* one.
- The key set is a frozen snapshot; a shape that varies by case would
  make every shape-diff a two-case decision ("does this fail on valid
  input, on invalid input, or both?"). A fixed key set is a one-line
  answer.

---

## Error entries

Each entry in `errors[]` is an object with exactly six keys. **All
six keys are always present.** Type and nullability are fixed; the
field set does not vary by error kind.

| Key | Type | Description |
|-----|------|-------------|
| `path` | `string` | Dotted path within the blueprint (e.g. `"variables.0.type"`). Empty string when the error applies to the document as a whole. |
| `message` | `string` | Non-empty, one-sentence description of the problem. |
| `hint` | `string` | Non-empty, one-sentence suggested fix. See [Hints](#hints). |
| `code` | `string` | Machine-readable error class (e.g. `"required.missing"`). Empty string when no class is known. Never `null`. |
| `line` | `integer \| null` | 1-based line number, when the parser could locate the failure (YAML parse errors). `null` otherwise. |
| `column` | `integer \| null` | 1-based column number, when available. `null` otherwise. |

### `path`

Dotted, matching the YAML document's structure:

- `"id"` — top-level field `id`.
- `"variables.0.type"` — the `type` field of the first `variables[]` entry.
- `""` (empty string) — the document as a whole (e.g. YAML that is not a mapping).

The empty-string convention (rather than `null` or a sentinel) is
deliberate: consumers can render `path` without a null check, and a
future *name* for the root path (e.g. `"$"`) would be a breaking
change to a nullable contract, whereas an empty string is simply a
naming convention that can be extended later.

### `message`

Non-empty. This is the "what went wrong" of a report; it never
duplicates `hint` (which is "what to do"), and it never contains
stack-trace information (that is stderr's job, in `--debug` mode).

Message wording **is not part of the contract**. A consumer that
string-matches on `message` is relying on an implementation detail,
and a future release will change the wording for readability without
warning. Branch on `code` instead (see below).

### `hint`

Non-empty, and the KPI this contract exists to enforce: **100% of
validation errors are actionable**. A missing hint is a defect in the
loader (see `forge.loading.errors.ValidationError`'s docstring), and
the contract requires the JSON renderer to surface it. Do not treat an
empty `hint` as a legal value — treat it as a bug to report.

### `code`

A machine-readable class for the error. Conventional values include:

| `code` | Meaning |
|--------|---------|
| `"required.missing"` | A required field is absent. |
| `"type.mismatch"` | A value has the wrong type. |
| `"enum.not_allowed"` | An enum value is not one of the allowed choices. |
| `"pattern.invalid"` | A string does not match its required pattern. |
| `"name.invalid"` | A named validator (project name, Python package, etc.) rejected the value. |
| `"input.not_found"` | The loader could not read a file. |
| `"io.error"` | A generic I/O error. |
| `""` | No class known. |

`code` is **stable** across releases: renaming one is a breaking
change. The set above is what the shipped loader produces today; new
codes may be added (that is non-breaking), but existing codes will
not be renamed or removed without a version bump.

### `line` and `column`

Only YAML-parse errors carry them: schema-phase and model-phase
errors do not have a location because the JSON Schema validator and
Pydantic do not track source positions. When both are present, they
are 1-based; when either is `null`, both are `null`.

The convention is `null`, not `0`, so that
`if entry["line"] is not None:` is the correct branch. `0` is not a
valid line number and would be a worse sentinel.

---

## Guarantees

This document guarantees the following, for every invocation of
`forge validate --json` that reaches the loader:

1. **Key set stability.** The top-level keys are exactly the six
   listed above; the per-error keys are exactly the six listed above.
   No key is added, removed, renamed, or made optional without a
   version bump.
2. **Nullability.** `blueprint_id`, `version`, `line`, and `column`
   may be `null`. No other field may be `null`.
3. **Validity invariant.** `payload["valid"] == (payload["errors"] == [])`.
4. **Actionability.** Every error entry has a non-empty `hint`.
5. **Determinism.** The same input produces byte-identical output
   across runs (`sort_keys=True`, `separators=(",", ":")`, no
   whitespace variance).
6. **ANSI-free.** The emitted string contains no terminal escape
   codes. Colour, if any, is applied by the caller's shell, not the
   tool.
7. **Stream discipline.** In JSON mode, all output is on stdout,
   nothing on stderr.
8. **Exit-code coherence.** A payload with `valid: true` corresponds
   to exit `0`; `valid: false` corresponds to exit `1`. Exits `2`
   and `5` do not produce an envelope.

---

## Non-guarantees

The following are **implementation details** and may change in any
release. Do not rely on them.

- **Key order in the emitted JSON.** The renderer sorts keys
  alphabetically (`sort_keys=True`), which is what makes the output
  deterministic — but that order is a property of the serialiser,
  not the contract. Parse the JSON; do not regex it.
- **Whitespace.** The renderer uses compact separators
  (`separators=(",", ":")`), so there is no whitespace between tokens
  and no trailing newline *inside the string*. The CLI's output
  machinery adds exactly one trailing newline on write (as POSIX
  expects); do not depend on the *presence* or *absence* of that
  newline for parsing (use `json.loads` or `jq`, both of which ignore
  it).
- **Message wording.** `message` and `hint` texts change for
  readability without warning. Branch on `code` for machine logic.
- **The set of codes.** New codes may be introduced in any release.
  A consumer that must recognise a specific code (e.g. `"required.missing"`)
  should handle unknown codes gracefully.
- **Error ordering within `errors[]`.** Errors appear in the loader's
  deterministic order, which is *path-sorted* today. That ordering is
  a property of the loader's aggregation, not of this contract;
  consumers that need a specific order should sort by `path`
  themselves.

---

## Versioning

The envelope advertises its contract version in `schema_version`.
For this document that value is **`"1.0.0"`** (a JSON string, not a
number, and not a SemVer object — parse it as a string and compare
against the literal).

### When `schema_version` is bumped

- **Patch (1.0.x).** Wording changes in `message` or `hint`. No
  shape change.
- **Minor (1.x.0).** A new optional field is added to the envelope
  or to error entries (with a default that preserves existing
  behaviour). Existing consumers are unaffected.
- **Major (x.0.0).** A key is renamed, removed, or made optional; a
  type changes; nullability changes. Consumers must update.

This document is the **single source of truth** for the current
version. When the version changes, this file, the DTO's
`Literal["1.0.0"]` annotation, and the golden fixtures in
`tests/fixtures/validate/` all change together in one commit. Three
independent tripwires (the doc, the type checker, the snapshot)
guard the same event — a consumer's bug costs more than three tests.

### How a consumer should branch on version

```python
payload = json.loads(stdout)
version = payload["schema_version"]

if version == "1.0.0":
    handle_v1(payload)
elif version.startswith("1."):
    handle_v1(payload)  # forward-compatible within a major
else:
    raise RuntimeError(f"Unsupported forge validate schema_version: {version}")
```

Or, in shell:

```sh
payload=$(forge validate --json ./my-blueprint)
version=$(echo "$payload" | jq -r .schema_version)

case "$version" in
  1.*) echo "$payload" | jq . ;;
  *) echo "Unsupported schema_version: $version" >&2; exit 1 ;;
esac
```

---

## Examples

### Success

Input: a valid blueprint.

```json
{
  "blueprint_id": "python.fastapi.api",
  "errors": [],
  "schema_version": "1.0.0",
  "source": "/abs/path/to/my-blueprint",
  "valid": true,
  "version": "1.0.0"
}
```

Exit code: `0`.

### Failure with two schema violations

Input: a blueprint missing `id` and with `version: "1.0"`.

```json
{
  "blueprint_id": null,
  "errors": [
    {
      "code": "required.missing",
      "column": null,
      "hint": "Provide a dotted lowercase identifier such as 'python.fastapi.api'.",
      "line": null,
      "message": "'id' is a required property",
      "path": "id"
    },
    {
      "code": "pattern.invalid",
      "column": null,
      "hint": "Use full SemVer: MAJOR.MINOR.PATCH, e.g. '1.0.0'.",
      "line": null,
      "message": "'1.0' does not match the SemVer pattern",
      "path": "version"
    }
  ],
  "schema_version": "1.0.0",
  "source": "/abs/path/to/my-blueprint",
  "valid": false,
  "version": null
}
```

Exit code: `1`.

### Failure with a YAML parse error (has `line` and `column`)

Input: a file with a YAML syntax error on line 3, column 8.

```json
{
  "blueprint_id": null,
  "errors": [
    {
      "code": "",
      "column": 8,
      "hint": "See docs/reference/SCHEMA_REFERENCE.md for the field's allowed values.",
      "line": 3,
      "message": "invalid YAML: expected <block end>, but found ':'",
      "path": ""
    }
  ],
  "schema_version": "1.0.0",
  "source": "/abs/path/to/my-blueprint",
  "valid": false,
  "version": null
}
```

Exit code: `1`.

### Usage error — no envelope

Input: a directory without `blueprint.yaml`.

**stdout:** *(empty)*
**stderr:** `✗ No blueprint.yaml found in directory: /abs/path/to/dir\n  hint: A blueprint directory must contain a 'blueprint.yaml' at its root.`
**Exit code:** `2`.

A consumer that expects JSON must branch on the exit code **first**.
A payload-parsing consumer that runs `json.loads(stdout)` will raise
`JSONDecodeError` on empty stdout; that is the correct signal that
"there is no envelope here."

### Internal error — no envelope

Input: any path, when the loader raises unexpectedly.

**stdout:** *(empty)*
**stderr:** `✗ Could not validate <path>: <ExceptionClass>: <message>\n  hint: This is an internal Forge error, not a problem with your blueprint. Run with --debug for a full traceback, or report this at ...`
**Exit code:** `5`.

The reason there is no envelope here: a Forge bug is not a *blueprint
validation result*. Emitting `{"valid": false, "errors": [...]}` for
"the loader crashed" would force every consumer to distinguish "the
blueprint was rejected" from "the tool blew up" by inspecting
`errors[0].code`. That is the mixed-message stream the renderer split
(ADR-0012) exists to avoid. Exit code 5 plus prose on stderr is the
unambiguous signal.

### Usage errors are not in the envelope

A usage error (exit `2`) and an internal error (exit `5`) both
produce no JSON. This is deliberate, and it is the reason the exit
code is authoritative:

| The consumer sees | Its correct response |
|-------------------|-----------------------|
| exit `0` + JSON   | Parse, read `valid: true` |
| exit `1` + JSON   | Parse, read `valid: false` and render `errors[]` |
| exit `2` + prose  | Report "usage error" (a caller mistake) |
| exit `5` + prose  | Report "tool bug" (report it upstream) |

A consumer that tries to distinguish these by parsing stderr is
doing the tool's job poorly; the exit code already names the category.

### Internal errors are not in the envelope

Same reasoning: `errors[]` describes violations **the loader
reported**. A loader crash is not a violation — it is the tool
failing to produce a verdict at all. Pretending it is a verdict
("there is one error, and it says 'the tool crashed'") would make
the envelope a mixed vocabulary (blueprint violations plus tool
failures). The exit code is where the category lives.

---

## Consumer recipes

### POSIX shell: fail the script on any error, show messages

```sh
if ! payload=$(forge validate --json ./my-blueprint); then
    case $? in
        1) echo "$payload" | jq -r '.errors[] | "\(.path): \(.message)"' >&2 ;;
        2) echo "Usage error; see stderr" >&2 ;;
        5) echo "Internal Forge error; re-run with --debug" >&2 ;;
    esac
    exit 1
fi
```

### Python: iterate errors, tolerate new codes

```python
import json
import subprocess

proc = subprocess.run(
    ["forge", "validate", "--json", "./my-blueprint"],
    capture_output=True, text=True,
)

if proc.returncode not in (0, 1):
    raise RuntimeError(f"forge validate failed ({proc.returncode}): {proc.stderr}")

payload = json.loads(proc.stdout)
for err in payload["errors"]:
    if err["code"] == "required.missing":
        ...
    else:
        # Unknown or irrelevant code — still render it.
        print(f"{err['path']}: {err['message']} — {err['hint']}")
```

### GitHub Actions: fail the job on invalid, annotate

```yaml
- name: Validate blueprint
  run: |
    set +e
    payload=$(forge validate --json ./my-blueprint)
    code=$?
    set -e
    if [ "$code" = "0" ]; then
      echo "Blueprint is valid."
    elif [ "$code" = "1" ]; then
      echo "$payload" | jq -r '.errors[] | "::error file=\(.path)::\(.message) — \(.hint)"'
      exit 1
    else
      echo "::error::forge validate failed with exit $code"
      exit 1
    fi
```

### Pre-commit hook: no output on success, actionable on failure

```bash
#!/usr/bin/env bash
# .git/hooks/pre-commit
files=$(git diff --cached --name-only --diff-filter=ACM -- '*blueprint.yaml')
[ -z "$files" ] && exit 0

for f in $files; do
    if ! out=$(forge validate --json "$f" 2>/dev/null); then
        echo "✗ $f"
        echo "$out" | jq -r '.errors[] | "  \(.path): \(.message)\n    hint: \(.hint)"'
        exit 1
    fi
done
```

Note the `2>/dev/null` on the invocation: in the failure path
the envelope is still on stdout, so suppressing stderr loses nothing.
In the usage-error path (`exit 2`) the diagnostics *would* be lost —
which is why the hook above exits on `! out=...` without echoing
stderr; if a user hits that path, they will see it when they
re-run `forge validate` manually.

---

## Appendix A: implementation notes for maintainers

These notes are for people changing `forge validate --json`, not for
people consuming it. They describe *why* the shape is what it is, so
a future change is made with the rationale in hand rather than in
spite of it.

### Why a versioned DTO and not a plain dict

`ValidateOutput` and `ValidateError` (in
`src/forge/cli/schemas/validate_output.py`) are Pydantic models, not
hand-built dicts. That is what makes "the contract" enforceable:

- The key set is a class definition, not a scattered set of dict
  literals.
- Nullability is a type (`str | None`), not a runtime convention.
- `schema_version` is a `Literal["1.0.0"]`, so a bump forces a
  type-check failure at every construction site until each is
  updated.
- `extra="forbid"` means a typo (`blueprint_id` misspelled) raises at
  construction, not on the consumer's side.

A dict-literal envelope would have none of these; the shape would
drift the first time someone added a field in one place and forgot
another.

### Why field-by-field construction in `_render_json`

The renderer builds each `ValidateError` field-by-field rather than
splatting `**err.__dict__` (or the Pydantic equivalent). The reason:

- A field added to the loader's `ValidationError` (say, `severity`)
  does **not** silently leak into the JSON contract. It becomes a
  code change in exactly one place — the renderer — and a reviewer
  sees it in the same diff as the contract doc if it belongs there,
  or in a separate diff if it does not.
- The DTO's `extra="forbid"` catches typos; the field-by-field
  construction catches *unintentional additions*, which are a
  different class of bug (the "I added a field to the internal type
  and forgot it would leak" bug).

### Why `schema_version` is a `Literal`

`ValidateOutput.schema_version: Literal["1.0.0"] = "1.0.0"`. The
`Literal` type means:

- A constructor call with a different value fails type-checking
  (mypy/pyright) and runtime validation (Pydantic).
- Bumping the contract version is a **search-and-replace in the type
  system**, not a hope that every construction site is consistent.
- The `= "1.0.0"` default means the common path does not spell it out,
  so the *bump* (changing the literal) touches only the type
  definition and the tests that assert the value — which is the
  correct unit of change for a version bump.

### Why the renderer is pure and separate from the command

`render_validate_json` (in `src/forge/cli/renderers/validate_json.py`)
takes a `ValidateOutput` and returns a `str`. It does not call the
loader, does not touch stdout, does not know the command exists. The
command (`_render_json` in `src/forge/cli/commands/validate.py`) is
what constructs the DTO and calls `ui.emit`.

This split makes:

- The DTO testable in isolation (construct it, assert its fields).
- The renderer testable in isolation (pass a DTO, assert the string).
- The command testable end-to-end (invoke it, capture stdout, parse).
- A future "format=yaml" or "format=jsonl" a new renderer, not a
  branch inside the command.

ADR-0012 explains the reasoning in full.

### Why `stderr` is empty in JSON mode

A subprocess's stderr is a separate stream in every POSIX-compatible
shell. Sending JSON to stdout and diagnostics to stderr means a
consumer can do:

```sh
forge validate --json ./my-blueprint 2>/dev/null | jq .errors
```

without losing anything it needs. If diagnostics went to stdout
(interleaved with JSON), or JSON went to stderr (where most logging
captures go), every consumer would need to filter the stream. The
tool already knows what the consumer wants; it should put it where
the consumer expects it.

The two cases where stderr is non-empty (usage error, internal error)
are cases where **there is no envelope**, so there is nothing for the
consumer to lose by ignoring stderr. That is why "stderr is empty in
JSON mode" is a precise statement: it is empty *when there is an
envelope*.

---

## Appendix B: changelog

| Version | Date | Change |
|---------|------|--------|
| `1.0.0` | — | Initial contract. Covers exits 0/1/2/5, envelope shape, error entry shape. |
