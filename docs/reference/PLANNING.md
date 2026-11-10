# Planning — Files, Destinations, and Kinds

> **Reference for blueprint authors and for contributors to the
> generation pipeline.** This document describes the *planning phase*:
> the step between a validated blueprint and a set of files on disk.
> Planning is what turns a declarative blueprint plus a resolved set of
> variables into a concrete, ordered list of "write this here, that
> way". Reading this document should be enough to (a) understand what
> the planner will do with a blueprint you wrote, and (b) know exactly
> which rule to consult if the planner rejects something.
>
> **Status of the pipeline.** The planner itself is complete and
> tested (see the module docstrings for `forge.planning`). The writer
> — the phase after the planner, which actually creates files — is a
> later task. This document specifies the *output* of the planner
> precisely, so the writer has a contract to build against, and so
> blueprint authors can reason about what will be written before the
> writer lands.

The planning phase
------------------
Planning sits between three other phases:

    ┌──────────┐    ┌───────────┐    ┌────────────────┐    ┌────────┐
    │  loader  │ →  │  resolver │ →  │    planner     │ →  │ writer │
    └──────────┘    └───────────┘    └────────────────┘    └────────┘
    YAML → model    vars → values     blueprint + values   plan → disk
                                      → list[PlannedFile]

Each arrow is a **pure function** with a `Result` return type. The
planner's contract is described below; the loader and resolver
contracts are in their respective references (`LOADER.md`,
`VARIABLE_RESOLUTION.md`).

Why the planner is separate from the writer
-------------------------------------------
The planner produces *data* — a list of `PlannedFile` objects. It does
not touch the filesystem for writes, does not read template bodies,
and does not perform any operation that could fail for reasons
unrelated to the blueprint's content. Two concrete consequences:

1. **Dry runs are free.** Rendering the plan to a human-readable
   listing (`forge create --dry-run`) is a pure formatting operation
   over the plan. No files are written, no template bodies are
   evaluated. The dry-run output is guaranteed to match what the
   writer would do, because it is a description of the same object.

2. **Batch validation is possible.** A tool that wants to check "would
   these 50 blueprints plan cleanly?" can run the planner without
   creating a single file. That is what `forge validate` uses today
   (in combination with the loader), and it is what a future "plan
   linter" would build on.

The two entry points
--------------------
```python
from forge.planning import FileMapper

mapper = FileMapper()
result = mapper.map(blueprint, variables)
```

* `blueprint` is a **loaded, validated `Blueprint`**. Loading a
  blueprint from disk, parsing its YAML, running it through the JSON
  Schema, and running it through Pydantic are the *loader's*
  responsibilities (see `LOADER.md`). The planner trusts those
  results and does not re-validate them.

* `variables` is a **plain `Mapping[str, object]`** — typically
  `ResolvedValues.as_dict()` from the resolver (see
  `VARIABLE_RESOLUTION.md`). The planner reads it only to substitute
  into `destination` templates; it does not re-validate variable
  values against the blueprint's declarations.

`mapper.map(...)` returns `Result[list[PlannedFile], MappingError]`:

* On success, `Ok(plan)` — a **sorted, collision-free list**.
* On failure, `Err(MappingError)` — an *aggregated* error carrying
  one `Finding` per problem found. The planner never fails on the
  first problem; it collects every problem it can find in one pass.

This `Result`-typed contract is the same discipline the loader and
resolver use (ADR-0010 and ADR-0011 respectively). A caller that
ignores the `Err` branch is a caller that ignores user-facing errors,
and the type checker will flag it.

`PlannedFile` — the output shape
--------------------------------
Each element of the plan is a `PlannedFile` — an immutable Pydantic
model with exactly three fields:

| Field | Type | Meaning |
|-------|------|---------|
| `source` | `str` | Path to the template or literal file, relative to the blueprint directory. |
| `destination` | `str` | Resolved, safe, relative path inside the target project. |
| `kind` | `PlannedFileKind` | `render` or `copy` — how the writer produces the file. |

Two convenience properties are part of the contract:

* `planned.is_render` — `True` iff `kind is PlannedFileKind.RENDER`.
* `planned.is_copy` — `True` iff `kind is PlannedFileKind.COPY`.

For every valid `kind`, exactly one of these is `True`. There is no
third kind today, and adding one requires updating these properties —
a test (`TestConvenienceProperties::test_properties_are_total`) pins
the invariant.

**Field names and types are a public contract.** Consumers read
`planned.destination`, branch on `planned.is_render`, and serialise
via `model_dump(mode="json")`. Renaming a field, changing a type, or
re-adding a previously-removed field is a breaking change and is
treated as such (see `tests/contract/test_planned_file_contract.py`).

Destination templates
---------------------
A destination in a blueprint is a **template**, not a literal path.
Any `{{ … }}` placeholders are substituted at planning time using the
*same sandboxed renderer* the writer will use for file bodies:

```
destination: src/{{ project_name }}/main.py
                           ↓
                    (substitute project_name = "payments-api")
                           ↓
                src/payments-api/main.py
```

Reusing the renderer is deliberate: it means a destination template
cannot use any capability (a filter, a global, an escape route) that
the sandbox denies for file bodies. If a filter is not on the
allowlist in `TEMPLATE_FILTERS.md`, it is not available for
destination substitution either.

### Destination safety rules

Every rendered destination is validated against the same rule the
loader applies to `source` paths. A destination is rejected — with a
`Finding` whose `code` begins `destination.` — if it:

| Rule | Rejection code | Why it matters |
|------|----------------|----------------|
| Is an empty string | `destination.empty` | A variable expanded to nothing; the resulting path names a file the user did not intend. |
| Contains NUL, LF, or CR | `destination.illegal_chars` | These break filesystem I/O at a level no hint can recover from. |
| Is longer than 400 chars | `destination.too_long` | Almost certainly a template bug (a variable that duplicated a directory). |
| Starts with `/`, `\`, or a drive letter | `destination.absolute` | Destinations are always relative to the target project. |
| Has an empty segment (`//`) | `destination.empty_segment` | Same cause as `destination.empty` — a variable that expanded to nothing. |
| Has a `..` or `.` segment | `destination.traversal` | Traversal segments indicate a template mistake or an escape attempt. |

The rule is **platform-independent**: `C:\Users\x` is rejected on
POSIX *and* Windows, because a blueprint must behave identically on
every host. The same reasoning applies to `\\Windows\x` and to
POSIX-style `/etc/evil`.

### Where destination validation happens

Destination validation has two layers:

1. **`FileEntry` (at parse time).** The `FileEntry` model — the
   `files[]` entry in the blueprint — has a Pydantic pattern on both
   `source` and `destination` that rejects **raw traversal** (literal
   `..` segments, leading `/` or `\`, drive letters). This is the
   *earliest* checkpoint. A blueprint whose `files[]` entries
   literally contain `../escape.py` cannot even be constructed as a
   model.

2. **`destinations.resolve` (at planning time).** After substitution,
   the *rendered* destination is validated against the same rules.
   This is what catches **substituted traversal** — a template like
   `out/{{ name }}/x.py` whose `name` value is `..`. The model pattern
   cannot catch this, because at construction time the value is the
   template string, not the rendered path.

The distinction matters when writing tests or debugging a rejection.
If `FileEntry(source="../x")` raises at *model construction*, the
bug is in the blueprint's *literal* content. If the plan rejects a
destination the model accepted, the bug is in a *variable value* that
was substituted into the template.

### Source paths

Sources are validated against the same rules as destinations, minus
substitution (a source is not templated — it names a real file in
the blueprint). The `source.*` rejection codes mirror the
`destination.*` ones: `source.empty`, `source.illegal_chars`,
`source.absolute`, `source.empty_segment`, `source.traversal`.

The loader's `check_templates` phase additionally verifies that each
source file **exists and is readable**, and that its *resolved* path
stays inside the blueprint directory (which catches symlink escapes
the lexical checks cannot see). Those checks are the loader's, not
the planner's — the planner trusts that a loaded blueprint's sources
exist.

File kind — render or copy
--------------------------
Every `PlannedFile` has a `kind`. The classifier that decides it is
deliberately conservative, in this order:

1. **If either the source or destination has a binary extension, copy.**
   Binary extensions include `.png`, `.jpg`, `.gif`, `.webp`, `.ico`,
   `.bmp`, `.pdf`, `.zip`, `.tar`, `.gz`, `.tgz`, `.bz2`, `.xz`,
   `.whl`, `.so`, `.dll`, `.dylib`, `.exe`, `.woff`, `.woff2`,
   `.ttf`, `.otf`, `.eot`, `.mp3`, `.mp4`, `.webm`, `.wav`,
   `.sqlite`, `.sqlite3`, `.db`. The check is case-insensitive.

2. **If the source does not end in a template extension, copy.**
   Template extensions are `.j2`, `.jinja`, `.jinja2`. A source
   without one of these cannot be rendered — it is not a template.

3. **Otherwise, render.**

The "either side has a binary extension" rule is the sharp edge.
Consider `source: logo.png` and `destination: logo.png.j2` — the
source is unambiguously binary (it ends in `.png`), but the
destination *ends in* a template extension. Copying preserves the
bytes; rendering would corrupt them. The planner copies, on the
principle *"when in doubt, do not corrupt"*: copying a template-shaped
file produces literal `{{ }}` markers (ugly but recoverable);
rendering a binary-shaped file produces garbage (unrecoverable
without the original).

The conservative direction also protects a specific case: a template
source whose destination happens to end in a binary extension
(`source: pyproject.toml.j2`, `destination: pyproject.zip` — unusual,
but possible). The planner copies rather than renders, and the user
gets a `.zip` containing literal `{{ }}`. That is the correct
trade-off: the failure is visible and recoverable.

### Adding a new extension

Adding a binary or template extension is a one-line change to
`src/forge/planning/mapper.py` (the `_BINARY_EXTENSIONS` or
`_TEMPLATE_EXTENSIONS` constant), plus one parameter to the relevant
test in `tests/unit/planning/test_literal_copy.py`. The test file is
the executable list of extensions the planner currently handles; if
you add an extension to the constant, add it to the test. If you do
not, the extension will work but will not be *pinned* — and a future
contributor removing it from the constant will not fail any test.

Collision detection
-------------------
Two sources with the same destination collide. The planner detects
this **before returning the plan**, and rejects the whole plan rather
than letting one file silently overwrite another.

Detection rules:

* **Normalised comparison.** The comparison is on the *normalised*
  destination, not the raw string. `src/a.py`, `./src/a.py`, and
  `src//a.py` are the same file and bucket together. Normalisation
  rules are in `planning/destinations.py::normalise` and are the
  same rules `destinations.resolve` uses.

* **Case-sensitive comparison.** `README.md` and `readme.md` are
  treated as *different* destinations. Case-folding is a filesystem
  concern (some filesystems are case-insensitive, others are not),
  and the planner cannot portably decide which is which. A
  case-only collision on a case-insensitive filesystem is caught at
  *write* time by the writer, with a clear error.

* **All collisions reported.** A blueprint with three independent
  collisions produces three `Finding` objects, not one. The
  `MappingError` aggregates every problem the planner can find in
  one pass, so the user fixes them all in one editing session.

A collision `Finding` carries:

| Field | Contents |
|-------|----------|
| `code` | `"destination.collision"` |
| `destination` | The normalised path shared by the colliding sources |
| `message` | `"<N> sources map to the same destination"` |
| `hint` | `"Each destination must be produced by exactly one source…"` |
| `sources` | The source paths of every colliding file, in planner order (blueprint order — see below) |

The `sources` tuple is **not** alphabetically sorted. It preserves
planner order, which is blueprint order (the order the author wrote
the entries in `files[]`). This is what makes the finding read in the
same order the author sees the entries in their YAML — a small but
real translation cost the planner avoids paying.

Output ordering
---------------
The plan is **sorted ascending by `destination`**. This is part of
the contract, not a detail:

* Downstream consumers (dry-run renderers, future `--json` output,
  the writer) receive the plan in a stable, predictable order.
* Two runs of the same blueprint produce identical output, which
  makes plan diffing meaningful.
* Two blueprints with the same set of files in different order
  produce the *same* plan, because the plan's order is a function of
  destinations, not of input order.

The sort is a plain string sort (`sorted(plan, key=lambda p:
p.destination)`). It is *not* path-aware — `out/a.py` sorts before
`out/a/b.py` because `.` sorts before `/` in ASCII. That is a
deliberate choice: a path-aware sort would put files before their
subdirectory contents, which is arguably "cleaner" but changes the
output order of every existing plan. Determinism is more valuable
than aesthetic preference here.

Ordering is pinned by `tests/unit/planning/test_stable_order.py`.
Tests for cross-phase ordering (`test_planning_end_to_end.py`) verify
that the whole pipeline hands the planner the full set of files —
a loader that reordered `files[]` or a resolver that dropped one
would produce a different plan, and those tests catch it.

Conditional files
-----------------
A blueprint's `conditional_files[]` declares additional file
mappings that apply only when a `condition` expression evaluates
true. **The planner does not currently evaluate conditions** — the
conditional-files phase is a later task. What the planner does
today:

* Reads `files[]` (unconditional) and plans those.
* Ignores `conditional_files[]` entirely.

This is a deliberate seam, not an oversight. The reason: condition
evaluation is a separate concern with its own contract (what
expression language, how are variables exposed, what happens on a
condition evaluation error), and folding it into the planner would
make the planner responsible for both path resolution *and* condition
evaluation. Keeping them separate means:

* The current planner is a pure function of `files[]` plus variables.
* When condition evaluation lands, it becomes a *separate pure
  function* — `evaluate_conditions(blueprint, variables) ->
  list[FileEntry]` — and the planner is called with the concatenation
  of `files[]` and the evaluation result.

The seam is documented in `planning/mapper.py` ("The conditional-files
seam") and in the tests. Blueprint authors today should assume that
*only* `files[]` is planned; conditional files exist in the schema but
have no effect yet.

The `MappingError` aggregate
----------------------------
When planning fails, `map()` returns `Err(MappingError(...))`. The
`MappingError` carries:

| Field | Meaning |
|-------|---------|
| `findings` | A tuple of `Finding` objects — one per problem found. Never empty in an `Err`. |
| `source_blueprint` | The blueprint's `id`. Used for attribution in batch callers. |

Each `Finding` carries:

| Field | Meaning |
|-------|---------|
| `destination` | The offending destination, or `""` if the problem is not tied to one. |
| `message` | One-sentence description of the problem. |
| `hint` | Actionable fix guidance. **Non-empty on every finding the planner emits** — this is the traceability KPI. |
| `code` | Machine-readable classification (see the destination codes above, plus `source.*` codes and `destination.collision`, `destination.render_failed`). |
| `sources` | Present on collision and source-path findings; empty otherwise. |

Callers render findings however they like. The CLI's `forge create`
in non-interactive mode renders them via `UserInputError`; a future
`forge plan --json` would render them via a JSON envelope; both
consume the same `Finding` fields. The stable interface between the
planner and its consumers is `Finding.code` (for branching) and
`Finding.destination` + `Finding.sources` (for display).

Codes reference
---------------
| Code | Source | Meaning |
|------|--------|---------|
| `source.empty` | planner | Source path is empty. |
| `source.illegal_chars` | planner | Source contains a NUL, LF, or CR. |
| `source.absolute` | planner | Source starts with `/`, `\`, or a drive letter. |
| `source.empty_segment` | planner | Source has a `//`. |
| `source.traversal` | planner | Source has a `..` or `.` segment. |
| `destination.render_failed` | planner | The destination template failed to render (undefined variable, syntax error, sandbox denial, invalid filter arguments). |
| `destination.empty` | planner | Rendered destination is empty. |
| `destination.illegal_chars` | planner | Rendered destination contains a NUL, LF, or CR. |
| `destination.too_long` | planner | Rendered destination exceeds 400 chars. |
| `destination.absolute` | planner | Rendered destination starts with `/`, `\`, or a drive letter. |
| `destination.empty_segment` | planner | Rendered destination has a `//`. |
| `destination.traversal` | planner | Rendered destination has a `..` or `.` segment. |
| `destination.invalid` | planner | Rendered destination fails a structural sanity check (rare — reserved for edge cases). |
| `destination.collision` | planner | Two or more sources claim the same normalised destination. |
