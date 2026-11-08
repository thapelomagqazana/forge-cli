# 0010. Blueprint Loading Returns a Result and Aggregates Every Error

- **Status:** Accepted
- **Date:** 2026-11-08
- **Deciders:** thapelo
- **Consulted:** —
- **Informed:** —

## Context

A blueprint can fail in four different ways: the raw bytes may not be
valid UTF-8, the YAML may be malformed, the document may not conform
to the JSON Schema, the document may be schema-valid but
model-invalid (a Forge bug), or the document may reference template
files that do not exist on disk. That is a lot of failure surface for
a single file.

Before this decision, each of the four `forge` commands that touch
blueprints — `validate`, `create`, `list`, `doctor` — assembled its
own loading pipeline. Each ran the same four phases in the same
order, but each one handled errors differently:

- `validate` printed all errors and returned exit 1.
- `create` aborted on the first error and returned exit 1.
- `list` swallowed errors as warnings and returned exit 0.
- `doctor` aggregated errors but formatted them into a prose report
  that `validate` could not reuse.

The commands disagreed about *what* a valid blueprint is, because
each had its own copy of the phase order and its own translator
between the four engine-specific error shapes
(`UnicodeDecodeError`, `yaml.YAMLError`, `jsonschema.ValidationError`,
`pydantic.ValidationError`) and the CLI's user-facing output. Every
new rule — a schema addition, a model validator, a filesystem
check — had to be added to four places, and was invariably missed in
at least one.

Two structural problems emerged from this:

**First, the short-circuit / aggregation trade-off was being made four
times, inconsistently.** A user running `forge validate` wanted *all*
the errors in one report so they could fix everything in one edit. A
user running `forge create` wanted the *first* error, so a bad YAML
did not make them wait for template lookups. Both behaviours are
defensible, and both are what the user wants — but they are
incompatible within a single function unless the function returns a
data structure that lets the caller decide. A `raise` cannot express
"here are three errors, render them all"; a `return` cannot express
"stop at the first error, I do not want to know about the rest".

**Second, the failure-type taxonomy was leaked into every caller.** A
caller that wanted to know "is the YAML malformed, or is the schema
wrong, or is a template missing?" had to `try/except` three
different exception classes and inspect `args`, `__cause__`, and
`__str__` in each. That is fragile (an exception's `__str__` is not
a stable API), untestable (each caller tested its own handling), and
the opposite of what `core/exceptions.py` promises — that every
user-facing error has a stable `exit_code` attribute and a `hint`
attribute, so the CLI handler can dispatch on *type*, not text.

The competing forces were:

- **Uniformity**: every command that loads a blueprint should
  produce the same set of errors for the same document, with the
  same paths, the same hints, and the same exit code.
- **Mode-appropriateness**: `validate` needs all errors; `create`
  needs the first; `doctor` needs a summary. A single return shape
  must support all three without one mode's needs dictating the
  others'.
- **Testability**: the aggregation logic must be testable without
  spinning up the CLI, and the CLI's exit-code logic must be
  testable without a real subprocess.
- **Extensibility**: adding a new phase (a compatibility check, a
  security scan) must not require touching four commands.
- **Failure isolation**: a malformed local blueprint in
  `discovery` must not crash `forge list`; it must warn and skip.

## Decision

**We will define a single pipeline — `BlueprintLoader.load()` — that
returns `Result[Blueprint, list[ValidationError]]`.** Every command
that needs to load a blueprint calls this function and branches on
the `Result`. No command owns its own pipeline.

**We will model the outcome as a `Result` sum type, not as a raised
exception.** `Ok(blueprint)` and `Err([...])` are the only two values
the loader returns. This is a deliberate departure from Python's
"errors are exceptions" convention, and it is justified by the
aggregation contract: a single exception cannot carry three errors,
and re-raising an aggregate would either lose information or require
every caller to `try/except` around a call that is designed to
succeed on both branches. `Result` is the shape that expresses
"either this succeeded or it failed with these specific problems",
which is exactly what a loader does.

**We will define a single error type — `ValidationError` — that every
phase produces.** It is a frozen dataclass with six fields: `path`,
`message`, `hint`, `source`, `line`, `column`. No phase produces any
other error shape. `UnicodeDecodeError`, `yaml.YAMLError`,
`jsonschema.ValidationError`, `pydantic.ValidationError`, and the
filesystem's `OSError` are all caught at the phase boundary and
translated to `ValidationError`. The translation is tested in
isolation; the phase functions never see their engine's exception
escape.

**We will aggregate errors across phases, but short-circuit between
them.** The loader runs its four phases in a fixed order:

    parse YAML → JSON Schema → Pydantic model → filesystem

A failure in one phase stops the pipeline — the later phases are not
run. This is not an optimization; it is a correctness requirement.
The schema phase cannot validate a document whose structure the
parser rejected, and the model phase's paths are only meaningful on
documents the schema accepted. The user sees *every error from the
phase that rejected them*, and only that phase's errors. They fix
them, re-run, and either succeed or see the next phase's errors.
This is the "one edit, not ping-pong" contract the loader aims for,
and it is compatible with the schema being the referee: schema errors
are never buried under model errors, because model errors never
occur when schema errors do.

**We will centralize hints in a deterministic catalogue.** Every
`ValidationError` carries a `hint`. Hints are not inline strings at
each raise site; they come from
`forge.loading.hints.hint_for(path)`, a pure function that maps a
path to a hint. The catalogue is a small ordered list of
`(predicate, hint_text)` rules with first-match semantics, and it
guarantees that no path returns an empty hint. The catalogue is
tested for totality, specificity, determinism, ordering, and
phase-tolerance. When a new field is added to the schema, its hint is
added to the catalogue in the same commit.

**We will expose phase-specific functions alongside the façade.**
`schema_errors(document)` and `model_errors(document)` are public.
They exist for two consumers:
- The loader, which needs each phase's output independently so it
  can prefix paths with the phase name (`schema.version` vs
  `model.version`) and so it can aggregate them into a single
  `Err([...])`.
- Tests, which need to assert on each phase in isolation without
  constructing the whole pipeline.

The façade `validate_blueprint_document(document, source=...)` runs
both phases and returns the parsed `Blueprint` or raises the
appropriate structured error. It is the entry point for callers
(loaders, mocks, tests) that do not need the phase split.

**We will treat the loader as the source of truth for "is this a
valid blueprint?" and the CLI's exit-code contract as the source of
truth for "what does the shell see?".** The two are related but not
identical. The loader returns `Err([...])` for every failure — schema,
model, or filesystem. The CLI translates that `Err` into exit 1.
But a loader's `Err` never becomes exit 5; exit 5 is reserved for
*unexpected* exceptions from the loader, and the CLI catches those
separately (`handle_exception`). A user seeing exit 5 means "Forge
crashed", not "the loader rejected my file". That distinction is
what makes exit codes a stable contract: the loader's `Err` can be
an internal-data structure, while exit codes are the process-level
interface that scripts branch on.

## Consequences

### Positive

- **One rulebook, one pipeline, one error shape.** A new validation
  rule is added to the schema, the model, or the filesystem check —
  in one place. Every command inherits it. No command needs to be
  updated when a rule is added, unless the rule changes the exit-code
  contract.

- **The short-circuit / aggregate trade-off is made once, correctly.**
  The loader short-circuits between phases and aggregates within a
  phase. `validate` uses the aggregated report directly; `create`
  uses the aggregated report but aborts on the first error before any
  filesystem side effects. The decision of *whether* to short-circuit
  is captured in `create.py::_resolve_inputs`, where it can be tested;
  the loader's decision (short-circuit between phases) is captured in
  `loader.py`'s phase comments, where it can be tested.

- **Errors carry actionable hints.** The KPI "100% of errors carry a
  hint" is a checked property, not an aspiration, because the hint is
  a required field of `ValidationError` and its non-emptiness is
  tested per-path in `tests/unit/loading/test_hints.py`.

- **The CLI can render every error uniformly.** The CLI handler is a
  loop over `result.error` that calls `error.format()`. It does not
  know which phase produced each error, and does not need to. Adding a
  phase or a rule does not change the CLI's rendering logic.

- **Errors are data, not exceptions.** A `ValidationError` can be
  serialized (a future `--json` mode is a serialization of the
  existing shape, not a redesign), logged, stored in a test fixture,
  or passed to a future web UI — all of which would be awkward with
  exceptions. The `Result` type is the boundary that makes the
  loader's output a value rather than a control-flow signal.

- **The phase-specific functions are testable in isolation.** A test
  that wants to exercise the schema phase does not need to load a
  file, run the parser, or check the filesystem. It constructs a
  document dict and calls `schema_errors(document)`. That is the
  fastest possible test loop for a validation rule.

- **Discovery can be metadata-only.** `forge list` calls
  `BlueprintLoader.load(manifest, check_files=False)`, which runs the
  parse, schema, and model phases but skips the filesystem phase. A
  blueprint whose templates are missing is still listed (so the user
  can find and fix it), not silently dropped from the catalogue.

- **The exit-code contract is enforceable.** Because the loader's
  `Err` is a value, the CLI can branch on whether to return exit 1 or
  5 with a simple `isinstance` check. A crash in a *specific phase*
  (a bug in `from_phase`, a missing bundled resource) that escapes
  the loader is caught at the CLI boundary, classified as an internal
  error, and produces exit 5 — while the loader's own rejections
  always produce exit 1. That distinction is a stable public
  contract: exit 5 means "report a bug", exit 1 means "fix your
  input".

### Negative

- **The loader returns a data type, not an exception, so a naive
  caller can forget to check `.is_err()`.** Python's type checker will
  not force the check, and neither will Python's runtime. The
  mitigation is that `Result` has no `__bool__`, so `if result:` is a
  `TypeError` rather than a silent success; and `Err.unwrap()`
  raises with the full error payload, so any caller that tries to use
  the value without checking gets a loud failure. That is a strong
  enough guardrail in practice, but it is not as strict as Python
  would be if the loader raised exceptions.

- **Short-circuiting between phases means a document with both a
  schema error and a filesystem error reports only the schema error.**
  A user fixing the schema error re-runs and *then* discovers the
  missing template. That is a two-step fix where a single-step fix
  would be possible if all phases always ran. The trade-off is
  deliberate: running all phases unconditionally would bury the
  schema error under a cascade of filesystem errors that are
  irrelevant until the schema is fixed, and the phase ordering
  (schema before model, model before filesystem) is chosen so the
  most actionable error is always first.

- **Every phase must implement the same translation from its native
  error type to `ValidationError`, and that translation is a source of
  bugs.** A new phase added by a contributor unfamiliar with the
  contract could raise `UnicodeDecodeError` instead of returning a
  `ValidationError`, breaking the aggregator's assumption. The
  mitigation is that each phase function has a docstring stating the
  contract, and the loader has a phase-level `try/except` that converts
  any escape into a `ModelValidationError` with a "this is a Forge
  bug" hint — so a misbehaving phase produces exit 5 with a clear
  message rather than a crash.

- **`Result` is a bespoke type.** Callers must learn `is_ok()`,
  `is_err()`, `unwrap()`, `unwrap_or()`, `map()`, `map_err()`. These
  are not part of the standard library (there is a `result` package on
  PyPI, but this project did not adopt it — see Alternatives). The
  four names the project uses are documented in
  `src/forge/loading/result.py` and tested in
  `tests/unit/loading/test_result.py`, but a new contributor
  encountering `Result` for the first time will need to read both.

- **Hints are code, not data.** A change to a hint's wording is a
  change to a Python file, which means it goes through code review
  and requires a rebuild. Some projects externalize hints to
  translation files or JSON so non-developers can edit them. That is
  a future change; the current catalogue-in-code approach is
  deliberately small and keeps hints next to the paths they describe.

### Follow-up actions

- [ ] Add `--json` output to `forge validate`, serializing the
      existing `ValidationError` shape. The shape is already
      JSON-friendly (six scalar/optional fields); the work is a new
      flag and a new renderer, not a redesign.
- [ ] Add the compatibility phase (`min_forge_version` check) between
      the model and filesystem phases, per the "Adding a new error
      category" section of `docs/reference/VALIDATION_ERRORS.md`.
- [ ] Extract the hint catalogue into a data file (YAML or JSON) if
      it grows past ~50 rules, so translators and technical writers
      can edit it without touching Python.
- [ ] Document the four-phase pipeline in an architecture doc under
      `docs/architecture/loader.md`, including the short-circuit
      rules and the reasoning behind each one.
- [ ] Add a `forge doctor` check that verifies the bundled schema and
      the generated Pydantic models are in parity (`make
      models-check` as a runtime check). A model-validation error at
      load time is currently the only signal that parity has failed;
      a proactive check would catch it earlier.
- [ ] Audit the other command modules (`create`, `list`, `doctor`,
      `verify`, `ping`) for the `@app.callback()` vs
      `@app.command(name="")` shape bug. Every module that is
      registered as a *group* rather than a *leaf command* has the
      same invocation problem that `validate` had before the fix.

## Alternatives Considered

### Alternative 1: Keep exceptions, raise on first error

The simplest design: every phase raises its own exception, the
caller catches the exception it cares about, and aggregation is a
caller responsibility. This is what the pre-decision code did, and
what most Python code does by default.

Rejected because:

- **The user needs all errors, not the first.** `forge validate`
  exists for exactly this reason. A user with a blueprint containing
  three schema violations should not have to fix one, re-run, fix
  the next, re-run, fix the third. The ping-pong loop is the worst
  possible UX for a validation command.

- **`raise` cannot express "here are three errors".** You can raise
  a `SchemaValidationError` that carries a list of errors — that is
  what `StructuredError` does — but you cannot raise one error per
  violation, and the caller cannot inspect *which phase* rejected
  the document without inspecting the exception type. That coupling
  is the drift this ADR is fixing.

- **Testability suffers.** A test that wants to assert "this
  document produces these three specific errors" has to
  `with pytest.raises(SchemaValidationError) as info:` and inspect
  `info.value.errors` — which is fine, but the same test for a
  `create` command's input handling cannot use the same shape,
  because `create` needs to catch and translate. Different tests
  for the same rule, because different layers translate differently.

### Alternative 2: Return a plain list of errors

`load(path) -> tuple[Blueprint | None, list[ValidationError]]` —
the classic "success and errors in a single return value" pattern.

Rejected because:

- **`None` is not a value.** A `Blueprint | None` union forces every
  caller to check `is None` before using the value, and a caller
  that forgets gets an `AttributeError` three frames later — the
  same bug class that `Optional` was supposed to fix, and has not.
  A `Result` type makes the failure mode "this branch is `Err`, not
  `Ok`" explicitly, and its `unwrap()` raises with the full error
  list rather than a bare `AttributeError`.

- **`(Blueprint | None, list[ValidationError])` is representable in
  an invalid state.** A caller can construct `(None, [])`, meaning
  "failed with no errors" — which the loader never produces but the
  type allows. `Result` cannot represent that state: `Ok` has a
  value, `Err` has a non-empty list, and the type checker enforces
  it.

- **The `map`/`map_err` methods are useful.** A caller that wants
  to post-process a successfully loaded blueprint (e.g. extract its
  variables for the wizard) calls `result.map(lambda bp:
  extract_variables(bp))`. A tuple has no such method, so the
  caller has to write the same `if is_ok` dance twice — once for
  the value, once for the error, once for each transformation.

### Alternative 3: Adopt the `returns` library

The `returns` package (PyPI: `returns`) is a mature,
well-tested implementation of `Result`, `Maybe`, and related types.
It has combinator methods, a `do` notation, an extensive test
suite, and a well-documented style guide.

Rejected, for now, because:

- **The surface we use is tiny.** `Ok`, `Err`, `is_ok`, `is_err`,
  `unwrap`, `unwrap_or`, `map`, `map_err`. That is eight names.
  Adopting a library for eight names means taking on its dependency,
  its changelog, and its upgrade cadence, and *paying the cost of
  learning it* for every new contributor.

- **`returns` is opinionated about style.** It provides a `do`
  notation and a functional-composition style that this project's
  codebase has not adopted. Adopting the library wholesale would
  either mean adopting its style (a large refactor) or fighting it
  (a large, ongoing friction).

- **The bespoke implementation is ~50 lines.** It is easy to read,
  easy to test, and easy to replace if the needs grow beyond eight
  names. Replacing it with `returns` later would be a mechanical
  change confined to `loading/result.py` and its imports; the rest
  of the code would be unchanged.

- **The `unwrapper` behavior is a contract.** This project's
  `Err.unwrap()` raises `RuntimeError` with the error payload in the
  message. `returns`' `unwrap()` raises `UnwrapFailedError` with a
  similar message. If the project ever migrates to `returns`, the
  change would be visible in the exact exception type a bug report
  might cite. Keeping the project's own `Result` means the project
  owns the failure mode.

The caveat: if the project ever needs `Result` combinators beyond
`map`/`map_err` (e.g. `and_then` for chaining loaders, `apply` for
applicative-style composition), the bespoke `Result` will need to
grow — and at that point, adopting `returns` becomes more
attractive. That decision can be made then, based on the actual
needs, not on speculation now.

### Alternative 4: Use `typer.Exit` for all commands, not `Result`

Argued from the observation that `forge` is a CLI — its only purpose
is to be invoked from a shell, and its only output is an exit code
plus stdout/stderr. Why not skip `Result` entirely and have the
loader raise `typer.Exit` with the right code?

Rejected because:

- **The loader is not a CLI concern.** It is imported by tests,
  future non-CLI entry points (a future REST API, a future SDK), and
  in-process tools (the wizard). None of those want to exit the
  process; they want to branch on the outcome. A loader that raises
  `typer.Exit` is a loader that cannot be used except by a CLI.

- **`typer.Exit` is a single-signal channel.** It carries an exit
  code, but not the errors. A `create` command that wants to render
  all errors before exiting must first catch the `typer.Exit`, inspect
  the exception's `args` (which `typer.Exit` does not populate), and
  reconstruct the error list — which means the loader's error data
  is going through an exception's `args` field, which is not a
  contract any project should rely on.

- **The tests would be worse.** `with pytest.raises(typer.Exit) as
  info: load(path)` gives you an exit code but not the errors.
  `result = load(path); assert result.is_err(); assert len(result.error) == 3`
  gives you the errors directly. The latter is what the loader's
  tests need to prove; the former is what the CLI's tests need to
  prove. Both are valuable, but they test different layers.

### Alternative 5: Aggregate errors across *all* phases unconditionally

Rather than short-circuiting between phases, run all four phases on
every document and merge the errors into one list. A user sees every
error in one report, with no need to re-run.

Rejected because:

- **Later phases produce noise on documents the earlier phases
  rejected.** A document whose YAML is malformed (unclosed bracket)
  parses to something the schema engine will refuse to touch — its
  reported "root must be an object" error is meaningless because
  there is no root object yet. The schema phase's errors on such a
  document are not information about the user's blueprint; they are
  information about the parse failure's aftermath, and the user is
  not helped by seeing them.

- **The filesystem phase is expensive on documents that cannot be
  used.** It stats every referenced template file. On a
  schema-invalid document, its errors are all "missing template" for
  files the user has not yet fixed the manifest to reach. Running it
  produces a wall of errors that will all disappear once the user
  fixes the manifest's first schema error.

- **The phase order is itself information.** "The YAML is broken, so
  I cannot tell you whether the schema is valid" is a more honest
  message than "here are 3 YAML errors and 15 schema errors and 4
  filesystem errors", because the second message implies all 22 are
  independent. They are not — most of the 19 errors from later
  phases are consequences of the first 3.

The short-circuit between phases preserves the layered information:
a user who fixes the current phase's errors re-runs and sees the next
layer cleanly. That is a slower but more honest feedback loop, and it
is the same loop a user would design for themselves if they were
debugging by hand.

## References

- `src/forge/loading/loader.py` — the loader's implementation and
  its phase comments.
- `src/forge/loading/errors.py` — the `ValidationError` definition.
- `src/forge/loading/result.py` — the `Result` type.
- `src/forge/loading/hints.py` — the hint catalogue.
- `src/forge/loading/aggregator.py` — the phase-to-error-list
  translation.
- `src/forge/verification/handlers/__init__.py` — the schema and
  model phase implementations.
- `tests/unit/loading/test_loader_multi_error.py` — the aggregation
  contract.
- `tests/unit/loading/test_loader_malformed_yaml.py` — the
  short-circuit contract.
- `tests/unit/loading/test_result.py` — the `Result` type's
  contract.
- `tests/unit/loading/test_hints.py` — the hint catalogue's
  contract.
- `docs/reference/VALIDATION_ERRORS.md` — the user-facing reference
  for the error shape, streams, and exit codes.
- `docs/reference/exit-codes.md` — the exit-code contract.
- ADR-0007 — the schema is the canonical contract.
- ADR-0008 — the Pydantic models are a projection of the schema.
- ADR-0009 — blueprint discovery and precedence.
