# 0011. Variable Resolution Is a Pure Function over Ordered Sources with a Single Validation Funnel

- **Status:** Accepted
- **Date:** 2026-11-09
- **Deciders:** thapelo
- **Consulted:** —
- **Informed:** —

## Context

`forge create` is the primary entry point for project generation.
It takes a blueprint ID, resolves that blueprint from the discovery
layer, collects the variables the blueprint declares, and hands the
resolved values to a generation pipeline that writes the project to
disk. Between "the user typed something on the command line" and
"the renderer sees a value" there is a boundary, and the shape of
that boundary determines almost every downstream behaviour of the
command:

- Which error a user sees when a required variable is missing.
- Whether `--var debug=false` actually disables a default of `true`.
- Whether the interactive wizard and the non-interactive flag path
  accept the same project names.
- Whether the rendering engine can trust every value it receives, or
  must re-check them.
- Whether a bug in validation is diagnosed from one file's diff or
  from four.

Three concrete forces pushed the boundary in different directions
and made "just validate at the call site" untenable:

**1. Two input modes, one set of rules.** Interactive mode (`forge
create` at a TTY) collects values through the wizard in
`cli.prompts`; non-interactive mode (`--no-interactive` or a
non-TTY stdin) collects them from `--var` flags and defaults. If
each mode validated independently, they would drift — a name the
wizard accepts would eventually fail in a script, or vice versa. The
project has already paid this debt once: the CLI's original funnel
(`validate_blueprint_inputs`) was called from one mode and not the
other, and the resulting behaviour differed depending on how the
command was invoked.

**2. Fail-fast, fail-complete is non-negotiable for scripted
callers.** A CI job that runs `forge create` with three missing
variables should get all three errors in one pass, not one per
retry. That requires the resolver to treat failures as *data* (an
aggregated `list[ValidationError]`) rather than as *control flow*
(raise on the first failure). It also requires an explicit success
type that carries the guarantee "these values were validated" —
otherwise the renderer must re-check, which reintroduces the second
funnel the project is trying to eliminate.

**3. The renderer must not be responsible for validation.** A
rendering engine that accepts a plain `dict[str, Any]` cannot know
whether its inputs were validated. It must either re-validate
(defeating the boundary) or trust blindly (which is not a boundary).
The only way to make "unvalidated input cannot reach the renderer" a
*checkable* property is to give the resolver its own output type,
constructible only by the resolver.

Prior to this decision, the project had:

- A `validate_blueprint_inputs()` function that validated *some*
  inputs for *some* code paths.
- Per-command ad-hoc validation in `create.py` that re-checked
  project-name rules the resolver would apply anyway.
- A `ResolvedValues` type mentioned in design discussions but not
  yet implemented.
- No aggregation guarantee — a validator failure aborted the
  resolution, so users saw one problem at a time.

The inadequacy was structural, not incidental: without a dedicated
resolution layer, every new validation rule had to be added in
multiple places, and every new consumer had to be trusted to
re-check. The project was one feature away from a "the wizard
accepted a name the renderer rejected" bug becoming impossible to
diagnose.

## Decision

We will introduce a dedicated resolution layer at
`src/forge/resolution/` whose public contract is a single pure
function:

    resolve(blueprint, cli_args, prompt_answers)
        -> Result[ResolvedValues, list[ValidationError]]

with the following properties, each of which is load-bearing:

**Ordered sources.** The resolver picks each variable's value from
the highest-priority source that supplies it:

1. `--var name=value` (CLI flags)
2. Interactive prompt answers
3. Blueprint defaults (when not `null`)

"Supplied" means *non-`None`*. `""`, `0`, and `False` are supplied
values — they reach the validator, which decides whether they are
acceptable. Only `None` (and absence) means "not supplied". This is
the rule that makes `--var debug=false` actually disable the
feature, rather than silently falling through to a default of
`true`.

**A single validation funnel.** All values — regardless of source
— flow through the same validation pipeline:

1. Type coercion (`resolution.coercion`) — turn strings into the
   declared type (`string`, `boolean`, `enum`).
2. Pattern matching (strings only) — `re.fullmatch` against
   `validation.pattern`.
3. Enum membership — exact-match against `validation.enum`.
4. Named validators (`resolution.names`) — for variable ids with
   well-known meanings (`project_name`, `python_package`,
   `npm_package`).

The order is deliberate: each phase assumes the previous one
succeeded, and the *first* failure for a given variable is the one
reported. Errors across *variables* are aggregated — the resolver
never stops at the first failure.

**Explicit coercion, not Python defaults.** `to_boolean` accepts
the closed set `true`/`false`/`yes`/`no`/`y`/`n`/`on`/`off`/`1`/`0`
case-insensitively; `to_string` refuses non-strings rather than
calling `str()`; `to_enum` requires exact membership. The alternative
(`bool(raw)`, `str(raw)`, `raw in choices`) produces the canonical
footguns (`bool("false") == True`, `str(1.10) == "1.1"`) and the
resolver's job is precisely to close those.

**An immutable, resolver-owned output type.** `ResolvedValues` is a
frozen dataclass backed by `MappingProxyType`, constructible only by
the resolver. Downstream APIs (rendering, generation) annotate
parameters as `ResolvedValues`, so mypy rejects any call site that
passes a plain `dict`. This turns "unvalidated input cannot reach
the renderer" from a code-review promise into a compile-time
guarantee.

**A pure function.** `VariableResolver.resolve` does no I/O, no
prompting, no filesystem access, no environment lookups, no clock.
Given the same inputs it produces the same output, always. It can be
called from the CLI, from tests, from a future HTTP handler, or from
batch tooling without adaptation.

**No exception-based validation.** The resolver returns `Err(errors)`
for user-input failures. It does not raise. Exceptions are reserved
for *developer* errors (an invalid `Blueprint` instance) and
unrecoverable conditions (I/O failures while loading a blueprint,
which belong to the loader). The distinction matters because
`Result` is *aggregable* and exceptions are not.

## Consequences

### Positive

- **One rulebook, two collectors.** The interactive wizard and the
  non-interactive flag path both feed the resolver; a rule added
  once applies to both. There is no second implementation of
  "project name must be lowercase" and no second implementation of
  "boolean accepts these spellings". The class of bug where one
  mode accepts what the other rejects becomes structurally
  impossible.

- **Fail-fast, fail-complete.** A CI job with three missing
  variables gets all three errors in one invocation. A user
  editing a blueprint sees every problem in one `forge create`
  run, not one per retry. The aggregation property is what makes
  this possible, and it exists because the resolver returns
  *data*, not *control flow*.

- **The renderer cannot be surprised.** `ResolvedValues` is not a
  `dict`; it is a typed container that only the resolver can
  produce. Downstream signatures use it, so "did this value come
  from the resolver?" is answered by *the type checker*, not by a
  code reviewer's reading of the call site. The KPI ("0
  unvalidated inputs reach the rendering engine") is delivered
  structurally rather than by convention.

- **Testability.** A pure function that takes three dicts and
  returns a `Result` is trivially unit-testable — no filesystem
  fixtures, no subprocess spawning, no TTY simulation. The bulk
  of the resolution layer's behaviour is verified in
  `tests/unit/resolution/` at the speed of a unit test. Only the
  end-to-end wiring (argv → exit code) needs a subprocess, and
  that is tested separately in `tests/integration/test_create_
  resolution.py` at a much smaller footprint.

- **Uniform error shape.** Every failure — a missing required
  variable, a bad boolean spelling, an invalid project name — is a
  `ValidationError` with `path`, `message`, `hint`, and `code`.
  The CLI's renderer, the loader's error report, and the wizard's
  inline feedback all speak one dialect, so adding a new consumer
  does not require a new error type.

- **Adding a rule is a one-file change.** A new named validator
  is a function in `resolution.names` plus an entry in
  `NAMED_VALIDATORS` plus a test file. A new type is a function in
  `resolution.coercion` plus a member of `VariableType` plus a
  test file. There is no "remember to update the other mode" step,
  because there is only one mode in the resolver's eyes.

### Negative

- **One more layer.** The command now depends on `VariableResolver`
  rather than on a local `_validate_inputs` helper. Contributors
  who only touch `create.py` must understand that validation lives
  elsewhere, and the module docstring has to explain the boundary
  for them (it does; the "Why the project name is validated
  *outside* the resolver" section exists for exactly this reason).

- **`ResolvedValues` is not a `dict`.** Code that treats the
  resolver output like a dict must either use the `Mapping`
  protocol (which `ResolvedValues` implements) or call
  `.as_dict()` for a copy. That is a small friction, but it is
  also the *reason* the guarantee holds — a `dict`-shaped type
  could be produced by any caller, and the whole KPI would
  dissolve.

- **An extra type conversion at the command boundary.**
  `_resolve_variables` in `create.py` has to combine the resolver's
  output with the command's project name (which is a command
  argument, not a blueprint variable) into a fresh
  `ResolvedValues`. That is a few lines of wrapping, and it is
  what keeps the resolver's contract honest: "resolve what the
  blueprint declares", nothing more.

- **Command-argument validation is duplicated by necessity.** The
  project name is a command argument; the resolver only knows
  blueprint variables. So `create.py` validates the project name
  *and* passes blueprint variable values through the resolver. The
  two use the *same* named validator, so they cannot disagree —
  but the duplication is a consequence of the boundary, and a
  future contributor must understand why it is not a regression.

### Follow-up actions

- [x] Implement `resolution.coercion`, `resolution.validators`,
      `resolution.names`, `resolution.precedence`, and
      `resolution.resolver`.
- [x] Add `ResolvedValues` with the "no convenient constructor"
      property and the frozen-mapping internals.
- [x] Route `cli.commands.create._resolve_variables` through
      `VariableResolver`.
- [x] Add the KPI guard test
      (`tests/unit/resolution/test_no_unvalidated_inputs.py`).
- [x] Add the end-to-end integration test
      (`tests/integration/test_create_resolution.py`).
- [x] Write `docs/reference/VARIABLE_RESOLUTION.md` — the user-facing
      reference for precedence, per-type rules, named validators,
      and error codes.
- [ ] Update `cli.commands.create`'s docstring to remove the
      reference to `validate_blueprint_inputs` (the old funnel),
      which is no longer called.
- [ ] When the generation pipeline lands (Task 1.3.1), remove the
      `_GENERATION_PENDING` markers from the four pipeline tests in
      `tests/integration/test_create_resolution.py`. The
      `strict=True` markers will fail the run automatically, which
      is the intended prompt.

## Alternatives Considered

### Alternative 1: Validate at each call site (the status quo ante)

The project *already* had a validation layer before this decision —
`verification.handlers.validate_blueprint_inputs` — and the
interactive wizard and the non-interactive flag path *both* called
it. The problem was not that no layer existed; it was that the layer
was:

- **Not typed.** It returned a `ValidatedInputs` dataclass, but
  downstream code often consumed the `dict` it was derived from,
  so the "these values were validated" property was not enforced by
  the type system.
- **Not aggregated.** It raised on the first error, so a user with
  two missing variables saw one at a time.
- **Not exhaustive.** It validated project name and destination,
  but blueprint variables were passed through as a raw dict.
- **Not pure.** Some call sites constructed the resolver's inputs
  from `os.environ` or from `pathlib.Path.cwd()`, so "the same
  inputs produce the same outputs" did not hold.

The decision to build a *new* layer rather than patch the existing
one was driven by the last two properties. Aggregation and purity
are not additions to a "validate this input" function; they are
different contracts. Patching `validate_blueprint_inputs` to return
`Result` would have been a rewrite of every call site anyway, with
no benefit from the compatibility.

### Alternative 2: Pydantic models as the resolver's output

The blueprint's variables are already Pydantic models
(`VariableDefinition`). It is tempting to make the resolver return a
dynamically-constructed Pydantic model whose fields are the
variables' ids, so callers get attribute access
(`values.author`) instead of mapping access (`values["author"]`).

We rejected this for three reasons:

- **Dynamic model construction is slow and fragile.** Building a
  `BaseModel` class at runtime from a variable list is a Pydantic
  feature (`create_model`), but it is not free: every call
  re-validates and re-compiles the model. For a blueprint with a
  dozen variables, that cost is measurable, and for a batch tool
  validating dozens of blueprints it is prohibitive.

- **Attribute access invites mutability confusion.** A Pydantic
  model is mutable by default (`model_config` can make it frozen,
  but that is a per-model configuration). The resolver's output
  must be *provably* immutable, not "immutable if the constructor
  remembered to set the config". The `MappingProxyType`-backed
  `ResolvedValues` makes immutability a property of the class, not
  a discipline of its constructor.

- **`ResolvedValues` is what downstream signatures can require.**
  A dynamically-constructed Pydantic model has a different class
  per invocation, so a downstream function cannot annotate its
  parameter as `ResolvedModel` — there is no `ResolvedModel` class.
  `ResolvedValues` is a fixed class; that is what makes the
  compile-time guarantee possible.

The dynamic-Pydantic approach is *better ergonomics* and *worse
guarantee*, and the decision prioritises the guarantee.

### Alternative 3: Two separate resolvers (one per mode)

Because interactive mode has a wizard and non-interactive mode does
not, one design would be to have two resolvers: one that prompts
for missing values, one that fails when values are missing. Each
could be simpler than the "two modes, one resolver" design.

We rejected this because *the whole point of the resolution layer
is that the two modes cannot disagree*. If there are two resolvers,
the validation rules live in two places, and the class of bug where
"the wizard accepts what the flag path rejects" becomes possible
again. That class of bug is exactly what the resolution layer
exists to eliminate.

The current design keeps the *collection* logic in the command
(`_collect_interactive_answers` prompts; `_parse_var_flags` parses)
and the *validation* logic in the resolver. The command decides
*what to ask*; the resolver decides *what is legal*. That split
gives the same simplicity as two resolvers, without the
drift.

### Alternative 4: Raise a `ValidationError` on the first failure

The simplest implementation of a validator raises on failure, and
lets the caller's `try/except` aggregate if they want to.

We rejected this because *the aggregation is the point*. A user
with three missing variables should not need three retries; a CI
job should not re-run a command per error. Raising a single
exception is the shape of a validator that does not care about
that user experience. The `Result[ResolvedValues, list[...]]`
shape is what makes aggregation the *default*, not an opt-in.

We also rejected a mixed approach — raise on some failures,
aggregate on others — because it makes the failure contract
undecidable from the signature. A caller cannot know whether a
given `try/except` will see a `ValidationError` or an `Err`, so
every caller must handle both. That is worse than either
homogeneous choice.

## References

- [ADR-0006](./0006-dual-mode-input-handling.md) — "Dual-Mode
  Input Handling with a Single Validation Funnel". The command-layer
  companion: how `create` dispatches into interactive vs.
  non-interactive mode, and why the two modes share a validation
  funnel.
- [ADR-0007](./0007-blueprint-schema-as-canonical-contract.md) —
  "The Blueprint JSON Schema Is the Single Canonical Contract". The
  schema that defines `VariableDefinition` and
  `VariableValidation` — the inputs to the resolver's per-variable
  rules.
- [ADR-0008](./0008-schema-generated-pydantic-models.md) —
  "Pydantic Models Are a Generated, Parity-Tested Projection of
  the Canonical Schema". Why the resolver's inputs are Pydantic
  models, and what guarantees those models carry.
- [ADR-0010](./0010-loader-result-and-error-aggregation.md) —
  "Blueprint Loading Returns a Result and Aggregates Every Error".
  The loader uses the same `Result` and `ValidationError` shapes
  that the resolver does; the two layers form a *complete* input
  pipeline (loader validates the blueprint, resolver validates the
  user's values).
- [`docs/reference/VARIABLE_RESOLUTION.md`](../reference/VARIABLE_RESOLUTION.md)
  — the user-facing reference for precedence, per-type rules,
  named validators, and error codes.
- [`docs/reference/SCHEMA_REFERENCE.md`](../reference/SCHEMA_REFERENCE.md)
  — the JSON Schema that defines the variable shape.
- [`docs/reference/VALIDATION_ERRORS.md`](../reference/VALIDATION_ERRORS.md)
  — the loader's error catalogue, which shares the
  `ValidationError` shape with the resolver.
- KPI: **0 unvalidated user inputs reach the rendering engine** —
  the acceptance criterion this ADR delivers, enforced by
  `tests/unit/resolution/test_no_unvalidated_inputs.py`.
