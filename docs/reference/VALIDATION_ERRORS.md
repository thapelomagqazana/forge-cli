# Validation Errors Reference

Every error `forge` reports when a blueprint fails validation shares a
single shape, a single vocabulary, and a single set of exit codes.
This document is the reference for that shape.

Two audiences:

- **Users** reading `forge validate <path>` output and trying to fix
  their blueprint.
- **Contributors** adding new validation rules, new error categories,
  or new commands that produce validation errors.

Both need the same contract, described once, in one place. That is
this file.

---

## Contents

1. [The error shape](#the-error-shape)
2. [The exit codes](#the exit-codes)
3. [Stream discipline](#stream-discipline)
4. [Where errors come from](#where-errors-come-from)
5. [The hint catalogue](#the-hint-catalogue)
6. [The four phases of validation](#the-four-phases-of-validation)
7. [Reading a validation report](#reading-a-validation-report)
8. [Adding a new rule](#adding-a-new-rule)
9. [Adding a new error category](#adding-a-new-error-category)
10. [Stability](#stability)

---

## The error shape

Every violation is a `forge.loading.errors.ValidationError`, a frozen
dataclass with six fields:

| Field | Type | Meaning |
|-------|------|---------|
| `path` | `str` | Dotted field path, e.g. `variables.0.type`. Empty string means "the document root". |
| `message` | `str` | One-sentence description of what is wrong. Starts lowercase. No hint text. |
| `hint` | `str` | One-sentence suggestion for fixing it. **Always non-empty.** |
| `source` | `str` | Label of the input this error came from (usually a file path). Defaults to `"<memory>"`. |
| `line` | `int \| None` | 1-based line number, when the phase could locate it. Only YAML parse errors populate this. |
| `column` | `int \| None` | 1-based column number, paired with `line`. |

Nothing else. No severity, no code, no category, no reference. If you
need those, add them to a *new* error type with its own tests and
documentation — do not widen this one. The narrow shape is what makes
it renderable uniformly across the CLI, future JSON output, and any
downstream consumer.

### The `path` field

Dotted, not JSON Pointer (though the loader normalises between the two
internally — see `forge.loading.aggregator.from_phase`).

| Scenario | Path |
|----------|------|
| Missing required field at document root | `id` |
| Invalid value for a nested field | `variables.0.type` |
| Missing template file referenced by the fourth file entry | `files.3.source` |
| Missing template file in the second entry of the first conditional block | `conditional_files.0.files.1.source` |
| YAML parse error (no field to point at) | `""` |

The path is *phase-prefixed* when the loader aggregates multiple
phases. A `ValidationError` produced by the loader always begins with
either `schema.` or `model.` (or a future phase name) followed by the
rest of the path. This is deliberate:

- The **schema** is the referee (`ADR-0007`). A document the schema
  rejects is a *user* problem — the user's blueprint is malformed.
- The **model** is a projection of the schema (`ADR-0008`). A
  document the schema accepts but the model rejects is a *Forge*
  problem — the two engines have drifted.

The prefix is the piece of information that lets a maintainer
distinguish the two at a glance:

```
schema.version: '1.0' does not match '...'    ← user's blueprint is wrong
model.version: does not match expected type  ← Forge's models drifted
```

### The `hint` field

Mandatory. Empty hints are a bug, enforced by tests in
`tests/unit/loading/test_hints.py`. Every hint names a *fix*, not a
*restatement* of the message.

Good:

- `message`: `'1.0' does not match the SemVer pattern`
- `hint`: `Use full SemVer: MAJOR.MINOR.PATCH, e.g. '1.0.0' — not '1.0' and not 'v1.0.0'.`

Bad (hint restates the message):

- `message`: `'1.0' is not valid SemVer`
- `hint`: `The version field must be valid SemVer.`

The catalogue enforces the "good" style. See [the hint catalogue
section](#the-hint-catalogue) below.

### The `source` field

A label identifying where the error came from. For a file load, it is
the path the user passed (not resolved — the resolved path is on the
`source` field of the loader's returned error, which `_render`
stamps). For in-memory validation, callers pass a label that makes
sense in context (`<test:multi-error>`, `<fixture>`, etc.); the
default `<memory>` covers the anonymous case.

The label is rendered verbatim. Downstream tools that batch-validate
many files (CI, `forge validate */*.yaml`, future `--json` output) key
on `source` to group errors per file.

---

## The exit codes

`forge` uses four exit codes for `validate`. They are documented in
full in `docs/reference/exit-codes.md` and in the source of truth,
`src/forge/core/exceptions.py::ExitCode`.

| Code | Name | When `validate` returns it |
|------|------|---------------------------|
| `0` | `SUCCESS` | The blueprint loaded successfully. |
| `1` | `USER_ERROR` | The blueprint is invalid, *or* the invocation is malformed (missing argument, nonexistent path, directory without a `blueprint.yaml`). |
| `2` | `BLUEPRINT_ERROR` | **Never returned by `validate`.** Reserved for commands that take a blueprint ID (`forge create`) and find it missing from the catalogue. `validate` takes a path, not an ID. |
| `5` | `INTERNAL_ERROR` | A bug in Forge. Report at the issue tracker. |

The distinction between 1 and 2 is subtle but load-bearing:

- **1 means "the user can fix this"** — either by fixing their file,
  their arguments, or their path.
- **2 means "the blueprint the user asked for is not available"** —
  and the only commands that can produce this are ones that take a
  blueprint *ID*.

`forge validate` takes a *path*. A path that does not resolve to a
blueprint is user input that the command cannot accept — exit 1. A
path that resolves to a malformed blueprint is user input that
produced an invalid document — exit 1. There is no scenario in which
`validate` should return 2.

If a future refactor of `validate` ever produces exit 2, that is a
bug. The exit-code table in `core/exceptions.py` is the source of
truth; the docstrings and tests must agree with it, not the reverse.

### Exit codes and scripts

A shell script that wants to distinguish "retry with different
arguments" from "the file needs fixing" from "report a bug":

```sh
forge validate "$file"
case $? in
    0)  echo "OK: $file" ;;
    1)  echo "FIX: $file has a problem the user can fix" ;;
    5)  echo "REPORT: Forge has a bug affecting $file" ;;
    *)  echo "UNEXPECTED: exit $? on $file" ;;
esac
```

The contract guarantees that `1` and `5` mean what the script says.
The absence of a `2` case is deliberate: `forge validate` cannot
return it.

---

## Stream discipline

Validation errors go to both stdout and stderr, split by role:

| Stream | Content | Why |
|--------|---------|-----|
| **stdout** | One line per `ValidationError`, rendered via `.format()` | The user asked to validate a file; the errors are the product. |
| **stderr** | The summary line (`N validation error(s) in FILE:`) and the success message (`✓ OK: ...`) | Metadata about the product — how many, from where, whether it succeeded. |

Consequences:

- `forge validate foo.yaml | grep schema.` matches the per-field
  error lines but not the summary.
- `forge validate foo.yaml 2>/dev/null` suppresses the summary but
  not the errors.
- `forge validate foo.yaml >/dev/null` suppresses the errors but not
  the summary — useful for a "did it pass?" check that only cares
  about the count.

There is no JSON mode yet. When `--json` is added, its output goes to
stdout (as the product), and the human-readable summary continues on
stderr for users who inspect both.

---

## Where errors come from

Every `ValidationError` originates in exactly one of four loader
phases, executed in this order:

```
raw bytes
    │
    ▼
┌────────────────┐
│ 1. parse_yaml  │   bytes → mapping | ValidationError
└────────────────┘
    │ (returns ValidationError → loader returns Err([...]))
    ▼
┌────────────────┐
│ 2. schema      │   mapping → mapping | list[ValidationError]
└────────────────┘
    │ (returns non-empty → loader returns Err([...]))
    ▼
┌────────────────┐
│ 3. model       │   mapping → Blueprint | list[ValidationError]
└────────────────┘
    │ (returns non-empty → loader returns Err([...]))
    ▼
┌────────────────┐
│ 4. filesystem  │   Blueprint → Blueprint | list[ValidationError]
└────────────────┘
    │
    ▼
Ok(Blueprint) | Err([...])
```

The pipeline **short-circuits** between phases:
- A parse failure stops everything — there is no document to validate.
- A schema failure stops the model and filesystem phases — the model
  errors would just echo schema errors with less precision, and
  filesystem refs on a schema-invalid document are ambiguous.
- A model failure stops the filesystem phase — the file references
  are on a Blueprint that Pydantic rejected, so they cannot be
  trusted.

The aggregate report therefore shows:
- Errors from **exactly one phase** when a phase fails.
- All errors from that phase — never just the first.

A user fixing an invalid blueprint sees one report, in one phase, in
one pass. They fix everything, re-run, and see either `Ok` or a
different phase's errors (which means they fixed the last round but
revealed the next).

The alternative — running all four phases unconditionally and
merging their errors — would produce a report where the schema errors
obscure the parse errors that caused them. That is worse UX, not
better coverage.

---

## The hint catalogue

Hints are generated by `forge.loading.hints.hint_for(path)`, a pure
function of the path string. The function is deterministic, total (no
path returns empty or raises), and fast (a short ordered list of
predicates).

### How hints are matched

Each entry in the catalogue is `(predicate, hint_text)`. The
predicates are tried in order; the first one that matches the
normalised path (dots only, no leading slash) supplies the hint. If
no predicate matches, the default is returned.

The order is load-bearing. A rule for `verification.0.command` must
come before a hypothetical generic `verification` rule, or the
specific hint never fires. The catalogue's tests pin the most
order-sensitive pairs.

### Phase-prefix tolerance

The loader aggregates errors with a `schema.` or `model.` prefix on
the path. The catalogue's predicates are written to match on path
*shape*, not on the prefix:

- Top-level fields use `p == "<field>" or p.endswith(".<field>")`,
  which matches both `version` and `schema.version`.
- Nested fields use substring matching (`"variables" in p`) plus a
  suffix check (`p.endswith("type")`), which is naturally
  prefix-tolerant.

A predicate that used only `p == "version"` would fire only for
unprefixed paths — which the aggregator never produces — and every
production error would fall through to the default. That class of
bug is tested explicitly in `TestPhaseTolerance`.

### The default

When no rule matches, `hint_for` returns:

> See docs/reference/SCHEMA_REFERENCE.md for this field's allowed
> values and examples.

The default:
- Points at a **public document** (not a class name, not a WBS ID,
  not an internal symbol).
- Is **non-empty** — the KPI "100% of errors carry a hint" is a
  structural property, not an aspiration.
- Is **short** — it reads as a pointer, not as advice.

A maintainer adding a new schema field should add a hint for it in
the same commit as the field. The default exists to keep production
usable during the window between a schema change and its hint being
added; it is not the intended outcome.

### Adding a hint

To add a hint for a new path:

1. Open `src/forge/loading/hints.py`.
2. Find the appropriate section (root fields, verification,
   variables, files, or a new section).
3. Add the rule **above** any existing rule that would otherwise
   match your path. The predicates are tried in order.
4. Add a test entry in `tests/unit/loading/test_hints.py`'s
   `TestSpecificity.known_path_has_specific_hint` table.
5. Run `pytest tests/unit/loading/test_hints.py` — the existing
   `TestRuleOrdering` tests will catch any regressions in the
   ordering of the surrounding rules.

Hints are user-facing text. Follow the style guide at the top of
`hints.py`'s `_RULES` list (imperative, concrete, short, no
back-references to the message, no internal jargon).

---

## The four phases of validation

### 1. YAML parse (`loading/yaml_parser.py`)

**What it checks:** that the raw bytes are valid UTF-8 and parse as a
YAML mapping.

**Errors it can produce:**

| Message (abbreviated) | Path | Hint |
|-----------------------|------|------|
| `file is not valid UTF-8: ...` | `""` | Re-save the file as UTF-8. |
| `invalid YAML: ...` | `""` | (default) |
| `blueprint is empty or contains only comments` | `""` | Start with the required fields. |
| `blueprint root must be a mapping, got <type>` | `""` | Wrap top-level lists or scalars under their intended key. |

YAML syntax errors carry `line` and `column` from PyYAML's parser
mark. Non-mapping errors carry `path=""` — there is no field to name
because the document's structure itself is wrong.

**What it does not check:** whether the document is a *valid*
blueprint (that is the schema phase's job), or whether any
referenced files exist (filesystem phase).

### 2. Schema (`verification/handlers/__init__.py::schema_errors`)

**What it checks:** that the parsed document conforms to the bundled
JSON Schema (`src/forge/schemas/blueprint.schema.json`, Draft
2020-12). The schema is the canonical definition of "a Forge
blueprint" (ADR-0007).

**Errors it can produce:** every violation the JSON Schema declares.
Examples:

| Message (abbreviated) | Path |
|-----------------------|------|
| `'nope' is not one of ['api', ...]` | `schema.category` |
| `'id' is a required property` | `schema.id` |
| `'1.0' does not match '...'` | `schema.version` |
| `Additional properties are not allowed ('unknown_field' was unexpected)` | `schema.unknown_field` |

Every error from the schema phase is prefixed with `schema.` in the
aggregated report.

**What it does not check:** semantic rules that require more than the
JSON Schema can express (e.g. "an `enum` variable must have at least
one allowed value"). Those are the model phase's job, and they use
Pydantic validators to express the invariant.

**The phantom-error filter.** When `jsonschema` evaluates an `allOf`/
`if-then` construct, it reports the failures of the `if` branch as
well as the failures of the `then` branch — the `if` failures are
"the rule did not apply", which is normal, not an error. The loader's
`_flatten_errors` helper filters these phantoms and deduplicates the
`then`-branch errors before rendering. The result: a report that
contains only actionable violations, not the internal false-positives
of the schema engine.

### 3. Model (`verification/handlers/__init__.py::model_errors`)

**What it checks:** that the parsed document, once schema-valid,
also validates against the generated Pydantic models
(`src/forge/models/blueprint.py`).

**Why this phase exists:** The Pydantic models are generated from the
schema (ADR-0008), but the generator cannot express every invariant
the schema declares. Where the two disagree — either the schema has a
rule the model cannot state, or the model has a stricter check than
the schema — the model phase catches it. The two engines are required
to agree on every document, enforced by a Hypothesis-based parity
test (`tests/unit/models/test_schema_model_parity.py`).

**Errors it can produce:** any `ValidationError` Pydantic raises.
Examples:

| Message (abbreviated) | Path | When |
|-----------------------|------|------|
| `type 'enum' requires validation.enum with at least one value` | `model.variables.0` | Enum variable declared without choices |
| `type 'command' requires the 'command' field` | `model.verification.0` | Verification check declared without its payload |

Every error from the model phase is prefixed with `model.` in the
aggregated report. **A `model.` error is always a Forge bug, not a
user mistake.** The user's file passed the schema; the model should
have accepted it. The parity test is meant to catch this before any
user sees it. When it does surface, the error's hint says "run `make
models-check` and report the diff".

### 4. Filesystem (`loading/fs_checks.py`)

**What it checks:** that every template file the blueprint references
exists, is readable, and stays inside the blueprint's directory.

**Errors it can produce:**

| Message (abbreviated) | Path | Hint |
|-----------------------|------|------|
| `template path escapes the blueprint directory: <path>` | `files.<i>.source` | Reference only files inside the blueprint's own folder. |
| `missing template file: <path>` | `files.<i>.source` | Copy or create the template file, or remove the reference. |
| `template file is not readable: <path>` | `files.<i>.source` | Check the file's permissions. |

The path for a static file reference is `files.<i>.source`; for a
conditional file, it is `conditional_files.<c>.files.<i>.source`.
The nesting mirrors the YAML structure, so the user can find the
offending line by searching for the path.

Every error from the filesystem phase is *not* prefixed — it is
already the loader's own phase, and its paths are unambiguous
(`files.N.source` only occurs here). If a future prefix is added, it
will be `fs.` or `filesystem.`, and the parity with schema/model will
be preserved.

---

## Reading a validation report

A typical `forge validate` invocation on an invalid blueprint produces:

```
$ forge validate examples/broken.yaml
✗ 3 validation error(s) in examples/broken.yaml:
  • examples/broken.yaml: schema.category: 'nope' is not one of [...]
    — hint: Pick one of: api, automation, frontend, library, cli, other.
  • examples/broken.yaml: schema.id: 'id' is a required property
    — hint: Provide a dotted lowercase identifier such as 'python.fastapi.api'.
  • examples/broken.yaml: schema.version: '1.0' does not match '...'
    — hint: Use full SemVer: MAJOR.MINOR.PATCH, e.g. '1.0.0'.
```

Reading it, one error at a time:

1. **The first line** (`✗ 3 validation error(s) in examples/broken.yaml:`) is on stderr. It tells you how many errors and which file. If you redirected `2>/dev/null`, you lost this line but not the errors below.

2. **Each `  •` line** is on stdout. It has the shape:

   ```
   <source>: <path>: <message> — hint: <hint>
   ```

   The `source:` prefix on every line is intentional. It makes each
   line independently greppable and copy-pasteable, and it means a
   multi-file batch (`forge validate */*.yaml`) produces output where
   every line names the file it belongs to. You do not have to
   scroll up to find which file the "3 errors" summary referred to.

3. **The `path`** identifies the offending field. For a top-level
   violation it is the field name; for a nested violation it is the
   dotted path (`variables.0.type`). For a missing required field,
   `jsonschema` reports the *parent* object with the field name in
   the message — the loader's aggregator promotes the field name into
   the path, so you see `schema.id` rather than `schema.<root>`.

4. **The `message`** says what is wrong.

5. **The `hint`** says how to fix it. If you skip every other line
   and read only the hints, you will know what to do.

For a *successful* validation:

```
$ forge validate examples/valid.yaml
✓ OK: python.fastapi.api v1.0.0
```

The success line goes to stderr (it is commentary, not the product —
the product is "no errors", which is the absence of stdout lines).
`2>/dev/null` on a successful run produces no output at all.

### Multiple files

```sh
$ forge validate */*.yaml
✗ 3 validation error(s) in examples/a.yaml:
  • examples/a.yaml: schema.id: 'id' is a required property — hint: ...
  • examples/a.yaml: schema.version: '1.0' does not match '...' — hint: ...
  • examples/a.yaml: schema.category: 'nope' is not one of [...] — hint: ...
✗ 2 validation error(s) in examples/b.yaml:
  • examples/b.yaml: schema.variables.0.type: 'integer' is not one of
    ['string', 'boolean', 'enum'] — hint: Variable type must be one of: ...
  • examples/b.yaml: schema.variables.0.validation.enum: 'enum' is a
    required property — hint: Enum variables need at least one allowed value.
✓ OK: python.fastapi.api v1.0.0
```

Each `forge validate` invocation handles one file — the shell's glob
expands to multiple invocations. Every line names its source, so the
report is navigable.

---

## Adding a new rule

Two places can enforce new rules, and the choice is not arbitrary:

### Add to the schema, not to code

If the rule can be expressed in JSON Schema — a required field, a
type constraint, a pattern, an enum, an `allOf`/`if-then` invariant —
add it to `src/forge/schemas/blueprint.schema.json`. **Do not** add a
Python check to `validate.py`, `loader.py`, or any other module.

The schema is the canonical contract (ADR-0007). Adding a Python
check that the schema does not express splits the contract in two.
A reviewer should reject any PR that adds a shape rule outside the
schema.

After changing the schema:

1. Regenerate the Pydantic models: `make models`.
2. Run `make models-check` to confirm the generated models match the
   schema (0 diff).
3. Run the parity test: `pytest tests/unit/models/test_schema_model_parity.py`.
4. Add a hint for the new field (see [the hint catalogue
   section](#adding-a-hint)).
5. Add fixtures and tests for the new rule:
   - A *valid* fixture that satisfies the rule (put it under
     `tests/fixtures/blueprints/valid/`).
   - An *invalid* fixture that violates it (put it under
     `tests/fixtures/loader/` or wherever the fixture category fits).
   - Tests in `test_schema_rejects.py` (schema-level) and
     `test_loader_multi_error.py` (loader-level, if the rule is
     expected to appear in aggregated reports).

### Add to the model only if the schema cannot express it

Some invariants cannot be expressed in JSON Schema:

- "If `type` is `enum`, `validation.enum` must be non-empty." This is
  an `allOf`/`if-then` in the schema, but Pydantic cannot express the
  implication directly, so it is re-enforced by a
  `@model_validator` on `VariableDefinition`. The two must agree, and
  the parity test verifies that they do.

The rule for adding model-only invariants:

1. Try to express it in the schema first.
2. If you can, do — schema-only rules do not require a model change.
3. If you cannot, add a `@model_validator` in
   `src/forge/models/blueprint.py`'s "hand-tuned region", and a
   matching `if`/`then` in the schema. The schema must still accept
   or reject the same documents.
4. Add a parity test fixture (valid and invalid) that exercises the
   rule from both engines.
5. `make models` and `make models-check` still pass.

### Add to the filesystem phase only for filesystem concerns

Only *filesystem* checks belong in `loading/fs_checks.py`. If the rule
is "this field must be a specific shape", it belongs in the schema.
If the rule is "this file must exist", it belongs here.

The `check_files=False` option is available for callers (discovery)
that cannot satisfy the filesystem contract but still want schema and
model validation. If a new filesystem rule is added, this option must
respect it — either by skipping the check (like the current one does)
or by raising a clear error if it cannot be skipped.

---

## Adding a new error category

If you need an error that the four phases cannot express — for
example, a compatibility check ("this blueprint requires Forge
X.Y.Z, you have A.B.C") — the pattern is:

1. **Add the phase to `loader.py`.** Insert it between the existing
   phases where its check makes sense. A compatibility check runs
   after the model phase (it needs the parsed `Blueprint` object) and
   before the filesystem phase (a compatibility failure makes
   filesystem checks pointless).

2. **Have the phase return `list[ValidationError]`.** Not a raise,
   not a `Result`, not a custom error type. The loader's aggregation
   is uniform across phases; a phase that raises breaks the shape.

3. **Prefix the paths with the phase name** in the aggregator, via
   `from_phase("compat", source, errors)`. The convention is that
   every phase-prefixed error can be unambiguously attributed to one
   phase.

4. **Add hints for the new field names** in
   `loading/hints.py`. If a new phase introduces fields like
   `min_forge_version`, the hints for those fields belong in the
   catalogue.

5. **Update the docstring in `loader.py`** to describe the new phase
   and its position in the pipeline.

6. **Add fixtures and tests:**
   - A valid fixture that satisfies the new check.
   - An invalid fixture that violates it.
   - Tests at the loader level (`test_loader_multi_error.py` or a new
     module).
   - Tests at the CLI level (`test_validate_command.py`) that the
     exit code and output are correct.
   - Parity tests, if the check has a corresponding schema rule.

7. **Update this document.** Add the phase to the "four phases of
   validation" section (which will become five), and describe its
   errors in the table.

The alternative — writing a one-off check that raises a custom
exception, or that produces a non-`ValidationError` object — is
exactly the kind of drift this document exists to prevent. The
loader's uniformity is what lets the CLI render every error the same
way, and that uniformity is what makes this document's contract
meaningful.

---

## Stability

The following are **public API** and will not change without a major
version bump:

- The six fields of `ValidationError`: `path`, `message`, `hint`,
  `source`, `line`, `column`.
- The four exit codes for `validate`: `0` (success), `1` (user
  error), `5` (internal error). (Exit code `2` is reserved and never
  returned by `validate`; if a future command produces it, this
  document will be updated.)
- The stream discipline: errors on stdout, summary on stderr.
- The `hint` field's contract: non-empty, actionable, names a fix.
- The `path` field's format: dotted, phase-prefixed when aggregated.

The following are **implementation details** and may change:

- Which phase produces which error. If a schema rule is moved to the
  model, or a filesystem check moves to a new phase, the path's
  *prefix* will change (`schema.foo` → `model.foo`) but the field
  portion (`foo`) will not. Consumers should treat the prefix as
  informative metadata, not as an identifier they key on.
- The default hint text. The wording is UX, not contract. The
  *presence* of a non-empty default is contract; the *contents* are
  not.
- The exact message text of any error. The `message` field is
  user-facing text; the contract is that it is a non-empty
  human-readable description of the problem. Consumers that need to
  distinguish specific errors should key on `path`, not on `message`
  substrings.

Adding a field to `ValidationError` is non-breaking (existing
consumers ignore fields they do not read). Removing one, or changing
one's type, is breaking and requires a major version bump.

Adding a new phase is non-breaking (existing valid documents still
validate; only documents with new kinds of problems get new errors).
Removing a phase is breaking if it changes the set of documents the
loader accepts.

Adding a new hint rule is non-breaking. Changing an existing rule's
*wording* is non-breaking; changing which *path* it matches is a
contract change for anyone who has written a test asserting on the
old wording — but that is a test concern, not a user concern.

---

## See also

- `docs/reference/exit-codes.md` — the full exit-code contract.
- `docs/reference/SCHEMA_REFERENCE.md` — every field of a blueprint,
  with constraints.
- `docs/architecture/loader.md` — the loader's design in depth.
- `docs/adr/0007` — the schema is the canonical contract.
- `docs/adr/0008` — the Pydantic models are a projection of the
  schema.
- `docs/adr/0010` — the loader returns `Result` and aggregates
  every error.
- `src/forge/core/exceptions.py` — the exit-code source of truth.
- `src/forge/loading/errors.py` — the `ValidationError` definition.
- `src/forge/loading/hints.py` — the hint catalogue.
- `src/forge/loading/loader.py` — the phase pipeline.
