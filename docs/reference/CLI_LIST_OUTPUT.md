# `forge list` Output Contract (v1.0.0)

> **Status:** stable. The JSON shape described here is a **public
> contract** for `schema_version` `1.0.0`. It is consumed by scripts,
> CI jobs, and downstream tooling, and changes to it are governed by
> the version-bump rules in [Versioning](#versioning) below.
>
> **Scope:** this document defines the shape of the output produced by
> `forge list`. It does *not* define which blueprints are discovered —
> that is a discovery concern (see ADR-0009). It does not define the
> human-readable table layout — that is cosmetic (see
> [Table layout](#table-layout) below) and is deliberately not
> contractual.
>
> **See also:** ADR-0012 (why output is a versioned DTO consumed by
> pluggable renderers), `docs/reference/SCHEMA_REFERENCE.md` (the
> blueprint schema itself), and `docs/architecture/discovery.md`
> (the discovery rules).

---

## Overview

`forge list` answers one question: *which blueprints are available on
this machine?* It answers it in two formats:

| Format | Trigger | Audience | Stream |
|--------|---------|----------|--------|
| Table  | default | humans   | stdout |
| JSON   | `--json` | automation | stdout |

Both formats are produced from the **same** `ListOutput` DTO (a
Pydantic model in `forge/cli/schemas/list_output.py`). A change to the
DTO therefore changes both formats — but only the JSON format is a
*contract*; the table is cosmetic (see [Table
layout](#table-layout)).

Two additional invariants apply to both formats and are covered in
[Cross-cutting invariants](#cross-cutting-invariants):

* **Exit codes** are part of the contract.
* **Stream selection** (stdout vs. stderr) is part of the contract.

---

## JSON format (`--json`)

### Envelope

`forge list --json` emits a single JSON object with exactly two
top-level keys:

```json
{
  "schema_version": "1.0.0",
  "blueprints": [ /* ... */ ]
}
```

| Key | Type | Required | Notes |
|-----|------|----------|-------|
| `schema_version` | string (SemVer) | yes | Version of *this* output contract. See [Versioning](#versioning). |
| `blueprints` | array of objects | yes | Ordered catalogue. See [Item shape](#item-shape). |

The envelope is an **object**, not a bare array. That is a deliberate
choice with three consequences consumers should rely on:

1. **Extensibility.** Adding top-level metadata (e.g. `generated_at`,
   `forge_version`, `total`) is *additive* — consumers that read
   `blueprints` are unaffected.
2. **Versioning.** The envelope carries its own contract version;
   consumers can pin behaviour without inferring it from field
   presence.
3. **Error-shaped envelopes.** A future version could emit
   `{"schema_version": "...", "blueprints": [], "warning": "..."}`
   for a warning case without introducing a second document format.

Consumers **must** read `blueprints` and ignore unknown top-level
keys (forward compatibility for minor version bumps).

### Item shape

Each element of `blueprints` is an object with exactly five keys:

```json
{
  "id": "python.fastapi.api",
  "name": "Python FastAPI API",
  "version": "1.0.0",
  "category": "api",
  "status": "ok"
}
```

| Field | Type | Constraints | Meaning |
|-------|------|-------------|---------|
| `id` | string | Dotted lowercase identifier (see [Identifier rules](#identifier-rules)) | Stable blueprint identifier used by `forge create <id>` |
| `name` | string | 1–120 chars | Human-readable name, suitable for display |
| `version` | string (SemVer) | Full SemVer, e.g. `1.0.0` | Blueprint version, independent of `schema_version` |
| `category` | string (enum) | `api` · `automation` · `frontend` · `library` · `cli` · `other` | Coarse classifier for grouping |
| `status` | string (enum) | `ok` · `stale` · `error` | Availability signal, see [Status](#status) |

Item objects have **no additional properties**. A key not listed
above will not appear; consumers that see one should treat the
document as belonging to a newer minor version and ignore it.

### Status

The `status` field describes the health of the underlying blueprint
file at the moment `forge list` ran. It is derived from a filesystem
check, not from the blueprint's own declaration.

| Value | Meaning |
|-------|---------|
| `ok` | The blueprint parses, validates against schema and model, and every referenced template file exists. Usable as-is. |
| `stale` | The blueprint parses and validates, but at least one referenced template file is missing. Metadata is trustworthy; the *content* is out of date. `forge create <id>` from a `stale` blueprint will fail at render time until the missing files are restored. |
| `error` | **Reserved.** Currently suppressed by the default catalogue (discovery skips malformed blueprints — see ADR-0009). Exists in the enum so a future `--include-broken` mode can surface it under the *same* envelope without a schema-version bump for consumers that already handle the full value set. |

Consumers should treat unknown `status` values as "not ok" — the safe
default when the tool grows a new state.

### Status and the item set

`forge list --json` **excludes** blueprints whose file is malformed
(schema-invalid, unparseable, etc.). Those appear only if a future
`--include-broken` flag is added, at which point they will carry
`"status": "error"` under the same envelope.

This is why the default catalogue never contains `"status":
"error"` today: discovery skips broken blueprints entirely, per
ADR-0009.

### Ordering

`blueprints` is sorted by `id` **ascending**, lexicographically (by
Unicode codepoint, which for the ASCII-only blueprint identifier
schema is the same as byte ordering). Consumers **may** rely on this
for `schema_version` `1.0.0`.

The sort key is the `id` field, not the `name`, `version`, or
`status`. See [Why `id`](#why-id) below for the rationale.

### Example

A complete response for a minimal install with the three official
blueprints:

```json
{"blueprints":[{"category":"api","id":"python.automation.cli","name":"Python Automation CLI","status":"ok","version":"1.0.0"},{"category":"api","id":"python.fastapi.api","name":"Python FastAPI API","status":"ok","version":"1.0.0"},{"category":"frontend","id":"typescript.frontend.react","name":"TypeScript React Frontend","status":"ok","version":"1.0.0"}],"schema_version":"1.0.0"}
```

(The example is one line because `render_json` uses
`separators=(",", ":")`; see [Byte-level
properties](#byte-level-properties) below.)

An empty catalogue in a checkout with no blueprints installed:

```json
{"blueprints":[],"schema_version":"1.0.0"}
```

Exit code `0` in both cases (see [Exit codes](#exit-codes)).

---

## Byte-level properties

The JSON output is intentionally deterministic at the byte level.
That makes it diff-able, snapshot-testable, and safe to compare
across runs. The properties below are contractual for
`schema_version` `1.0.0`:

| Property | Value | Why |
|----------|-------|-----|
| Key order | Alphabetic | `sort_keys=True` in the renderer. Not meaningful to JSON, but meaningful to `diff`, review, and snapshot fixtures. |
| Whitespace | None beyond `,` and `:` | `separators=(",", ":")`. Keeps pipelines into `jq` cheap; produces compact, diffable output. |
| Trailing newline | Exactly one | `ui.emit` appends it. A POSIX text file ends in a newline; a missing one glues the shell prompt to the payload. A double one breaks `wc -l`. |
| ANSI escapes | None | The renderer is documented as ANSI-free. Colour, if any, is applied by `ui.emit` and *never* enters a JSON payload. |
| Encoding | UTF-8 | `ensure_ascii=False`. Non-ASCII names round-trip faithfully. |

The renderer's source
(`forge/cli/renderers/json.py`) documents the same properties from
the *producer* side; this document states them from the *consumer*
side, and the two are tested against each other
(`tests/contract/test_list_output_contract.py::TestPurity`).

---

## Exit codes

The exit code is part of the contract. It is documented here in one
place so consumers can rely on it without reading the source.

| Situation | Exit code | Message | What to do |
|-----------|-----------|---------|------------|
| Success, at least one blueprint | `0` | none | — |
| Success, empty catalogue | `0` | none in JSON mode; warning on stderr in table mode | Not an error; the query was valid and the answer was "none". |
| Success, filters matched nothing | `0` | warning on stderr in table mode | The query was valid; the filter was too narrow. |
| Unknown `--category` or `--source` value | `2` | usage error on stderr | Fix the flag; see `forge list --help`. |
| Discovery error (e.g. broken override) | `1` | error message on stderr | Inspect the message; it names the recovery. |
| Unexpected internal error | `5` | "unexpected internal error" on stderr | Report a bug. |

Two conventions are worth stating explicitly:

* **Empty is success.** `forge list --json | jq '.blueprints | length'`
  returns `0`, not a parse error. A shell script running `forge list`
  under `set -e` does not fail because there are no blueprints
  installed.
* **`2` is reserved for usage errors.** POSIX convention; also what
  `typer.BadParameter` produces when raised during argument parsing.
  A usage error is a bug in the *invocation*, not in the environment.
  The CLI produces exit 2 for these by rendering the message
  explicitly and raising `typer.Exit(code=2)`, so the contract does
  not depend on which of Typer's two error paths happens to catch
  the raise.

---

## Stream selection

`stdout` is the *product* of the command. `stderr` is *commentary*.
This is the Unix contract, and it is load-bearing for pipelines.

| Stream | Contains | Never contains |
|--------|----------|----------------|
| stdout | the payload (table or JSON) | errors, warnings, progress, diagnostics |
| stderr | errors, warnings, progress, diagnostics | the payload |

Consequences:

* `forge list --json | jq` always receives valid JSON on the success
  path.
* `forge list --json 2>/dev/null` suppresses warnings without
  suppressing the payload.
* `forge list --json 1>/dev/null` suppresses the payload without
  suppressing warnings — useful when checking for warnings as a
  pre-flight.
* `forge list --json 2>&1 | ...` mixes them (as it should), and the
  consumer takes responsibility for distinguishing.

Exceptions to "stdout contains only the payload":

* **`--help` output** goes to stdout because it *is* the product of
  `forge list --help`.
* **`--version` output** goes to stdout because it *is* the product
  of `forge --version`.

Both are consistent with "stdout is the product" — they just
represent a different product than a catalogue listing.

---

## Table layout

The table is a **cosmetic** format. Its layout (column widths, word
truncation, colour, separators) may change between minor releases
without a version bump. Automation **must** use `--json`.

The table currently renders five columns:

| Column | Source | Notes |
|--------|--------|-------|
| `ID` | `item.id` | Truncated to 32 codepoints with a `…` glyph |
| `NAME` | `item.name` | Truncated to 32 codepoints with a `…` glyph |
| `VERSION` | `item.version` | Truncated to 10 codepoints |
| `CATEGORY` | `item.category` | Truncated to 12 codepoints |
| `STATUS` | `item.status` | Truncated to 8 codepoints |

Truncation caps are an implementation detail and are **not**
contractual. They exist to keep the table readable on a standard
80-column terminal even when a user supplies a long name. Wrapping is
never used, because a wrapped ID cannot be copy-pasted into
`forge create <id>`, which is the primary use of that column.

---

## Identifier rules

The `id` field is constrained by the blueprint schema
(`^[a-z][a-z0-9]*(\.[a-z][a-z0-9]*)+$`). Consequences:

* **ASCII lowercase only.** No uppercase, no non-ASCII.
* **Dotted.** At least one dot separating two or more segments.
* **Each segment starts with a letter.** `a.a` is valid; `1.a` is
  not.
* **Subsequent characters are lowercase letters or digits.**

This is why the contract can promise lexicographic ordering without
qualification: all valid ids are ASCII, so codepoint order is
unambiguous and does not vary with locale. A future schema revision
that allowed non-ASCII ids would need to revisit this document.

---

## Versioning

`schema_version` is a **SemVer string** (`MAJOR.MINOR.PATCH`). It
identifies the shape of the JSON envelope, not the shape of the
blueprint schema (which has its own versioning — see
`docs/reference/SCHEMA_REFERENCE.md`) and not the shape of any
individual blueprint (which has its own `version` field).

The bump rules:

### Major

* A required field is **removed** or **renamed**.
* An existing field changes **type** in a non-compatible way
  (e.g. `version` becomes an object `{"major": 1, ...}`).
* The envelope changes from an object to an array, or vice versa.
* The `status` enum gains a member **and** consumers cannot
  safely ignore unknown values. (Since consumers *must* ignore
  unknown enum values, adding a status is normally a **minor**
  change. It becomes major only if the new value changes the
  meaning of existing code paths in a way that cannot be tolerated.)

When a major bump happens, the new version **may** be emitted
alongside the old for one release cycle (e.g. `--json` emits v2 while
`--json-legacy` emits v1), after which the old version is removed.

### Minor

* A field is **added**. Consumers that ignore unknown fields are
  unaffected.
* A new `status` or `category` value is **added**. Consumers that
  handle unknown values are unaffected. (See the caveat in the major
  section.)
* A **cosmetic** property of the JSON changes (key order, whitespace
  — though in practice these are considered part of the contract and
  changing them is a compatibility concern; see
  [Byte-level properties](#byte-level-properties)).

### Patch

* A **documentation** or **wording** change with no shape impact.
* An **edge case** that was previously undefined becomes defined,
  without changing the shape of any output that existing consumers
  might see.

### The coupling with this document

Every change to the shape must be accompanied by:

1. An update to this document.
2. A bump of `schema_version` in `forge/cli/schemas/list_output.py`.
3. A change to the assertions in
   `tests/contract/test_list_output_contract.py` (which is
   written so a shape change fails CI until the version and the
   assertions are updated together).

That coupling is deliberate: it makes the shape change a *single,
reviewable diff* rather than a silent drift.

---

## Why `id`

Consumers sometimes ask why the ordering is by `id` rather than by
`name` (more human-friendly) or by `status` (more useful for
diagnostics). The answer:

* **`id` is stable.** Blueprints rename (`name`), and their status
  changes between runs (a template file is deleted and restored).
  `id` is the one field that both identifies the blueprint and is
  guaranteed not to change for that identity.
* **`id` is unique.** Two blueprints can share a name; the schema
  requires unique ids.
* **`id` is what consumers key on.** `forge create <id>` is the
  command that uses a blueprint; it takes an id, not a name or a
  status.
* **`id` sorting is locale-independent.** The schema restricts ids
  to ASCII lowercase (see [Identifier rules](#identifier-rules)), so
  the ordering does not vary with `LC_ALL` or terminal encoding — a
  requirement for byte-identical output across machines.

The ordering is a *contract*: a consumer relying on it is correct to
do so, and a change is a breaking change subject to the version-bump
rules above.

---

## Empty is not an error

An empty catalogue is a **valid answer**, not a failure. This is
stated three separate times in the documentation (ADR-0012, this
file, and the command's docstrings) because it is the most commonly
broken convention in CLIs:

* **`set -e` scripts.** A `forge list` invocation under
  `set -euo pipefail` must not fail because the environment is
  minimal.
* **Pipelines.** `forge list --json | jq '.blueprints | length'` must
  return `0`, not a parse error and not an empty string.
* **Semantic correctness.** "How many blueprints are installed?" has
  a well-defined answer ("zero") when none are. Returning an error
  would be answering a different question ("can I list blueprints?")
  with the wrong answer ("no").

Concretely:

| Mode | Empty catalogue | Exit |
|------|-----------------|------|
| `--json` | `{"blueprints":[],"schema_version":"1.0.0"}` on stdout, no stderr | `0` |
| table | warning on stderr, no stdout | `0` |

The table mode distinguishes "nothing installed" (warning mentions
installing blueprints / `forge doctor`) from "your filter matched
nothing" (warning mentions filters), because the recovery differs.
The JSON mode does not need to distinguish them — a machine consumer
acting on the payload sees an empty array either way.

---

## Cross-cutting invariants

The properties below apply regardless of format, and are the load-
bearing rules the contract is built on:

1. **stdout is the payload, stderr is commentary.** Every test and
   every pipeline depends on this.

2. **Exit 0 means success, including empty results.** Exit non-zero
   only for actual failures (usage errors, discovery errors, internal
   errors).

3. **The output is deterministic.** Two invocations with the same
   environment produce byte-identical output.

4. **Empty is a valid answer.** (Repeated because it is the rule
   most often broken.)

5. **The JSON shape is versioned and stable.** Automation can rely
   on the shape for `schema_version` `1.0.0`.

6. **The table layout is not a contract.** Automation must use
   `--json`.

If any of these is broken, it is a bug — open an issue with the
`cli-contract` label and cite this document.

---

## Quick reference for scripting

A few common patterns and their documented behaviour:

```bash
# Count blueprints.
forge list --json | jq '.blueprints | length'

# All ids, one per line.
forge list --json | jq -r '.blueprints[].id'

# Only frontend blueprints.
forge list --json --category=frontend | jq '.blueprints[].id'

# Only user-installed overrides.
forge list --json --source=local | jq '.blueprints[].id'

# Fail if any blueprint is stale.
forge list --json | jq -e 'all(.blueprints[]; .status == "ok")'

# Fail if the catalogue is empty (the one case where exit 0 is not
# what you want — check the length explicitly).
test "$(forge list --json | jq '.blueprints | length')" -gt 0

# Deterministic output, safe for hashing and comparison.
forge list --json | sha256sum
```

Every snippet above relies on a property documented in this file.
If one breaks, the fix is in the CLI, not in the snippet.
