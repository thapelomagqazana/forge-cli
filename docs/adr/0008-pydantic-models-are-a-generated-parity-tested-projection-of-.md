# 0008. Pydantic Models Are a Generated, Parity-Tested Projection of the Canonical Schema

- **Status:** Accepted
- **Date:** 2026-11-08
- **Deciders:** CLI Engineer
- **Consulted:** Architect
- **Informed:** —

## Context

Forge's canonical definition of a blueprint lives in a single JSON Schema
document, `src/forge/schemas/blueprint.schema.json` (ADR-0007). That schema is
authoritative: it is shipped in the wheel as a package resource, loaded
through `importlib.resources`, and applied by
`forge.verification.validate_blueprint_document` to every document before it
is used.

Schema validation alone, however, is not sufficient for the rest of the
application. The CLI's interactive wizard, the generation layer, and the
verification contract all need *typed* access to a blueprint: `blueprint.id`
rather than `blueprint["id"]`, `variable.type is VariableType.enum` rather
than `variable["type"] == "enum"`. Dictionaries lose type information at the
boundary, and every field access re-opens the question "what if this key is
missing?" that the schema has already answered.

The obvious solution — write Pydantic models by hand — creates a second
source of truth. Any field, constraint, or enum in the schema that is not
mirrored in the models, or vice versa, becomes a silent divergence: the
schema accepts a document the model rejects (a false rejection), or the model
accepts a document the schema would reject (a false acceptance). Both classes
of divergence are latent — they do not surface until a document happens to
exercise the divergent rule — and both are hard to diagnose because the
failure points at the *second* engine (the model), not the first (the
schema), so the developer's instinct is to fix the wrong file.

The naive alternative — generate the models and trust the generator — has its
own problem: `datamodel-code-generator` cannot express every JSON Schema
construct. Discriminated payloads (`type="command"` ⇒ `command` required)
and cross-field requirements (`type="enum"` ⇒ `validation.enum` present) are
expressible in the schema via `allOf`/`if-then` pairs, but the generator
emits no equivalent validator in Pydantic. Hand-editing the generated file to
add them, in turn, is erased by the next regeneration — a loop the project
has already been through more than once.

The design needs three properties simultaneously:

1. **One source of truth.** The schema remains the referee; nothing in the
   model tree can override or contradict it.
2. **Typed ergonomics.** Application code sees Pydantic models, not raw
   dicts.
3. **Enforced parity.** The two engines must accept and reject identically,
   and the parity must be checked mechanically rather than trusted.

## Decision

We will generate the Pydantic model tree (`forge.models.blueprint`) from the
canonical JSON Schema using `datamodel-code-generator` (0.25.x, pinned), and
we will treat the generated file as **checked-in, hand-tunable source** rather
than as a build artifact.

Concretely:

- **Generation.** `scripts/generate_models.py` invokes
  `datamodel-code-generator` with a fixed flag set and composes the result
  into three parts: the file header, the raw generated body, and any hand-
  tuned regions extracted from the currently committed file.

- **Hand-tuned regions.** Invariants the generator cannot express are marked
  with `# region: hand-tuned` / `# endregion` markers inside the class body
  they belong to. The generator extracts these blocks from the *committed*
  file before regenerating and re-injects them into the newly generated
  output, anchored by class name. Two invariants live in such regions today:
  `VariableDefinition`'s `enum`-requires-`validation.enum` rule and
  `CheckDefinition`'s discriminated-payload rule. Their truth tables are
  identical to the schema's `allOf`/`if-then` rules — that identity is not
  asserted by construction but by the parity test (see below).

- **Schema as the source of class names.** The generated file's class names
  come from the schema's `title` fields (`"title": "Blueprint"`,
  `"title": "VariableDefinition"`, etc.) combined with
  `--use-title-as-name`. This makes the schema *self-documenting* about the
  Python names, and it eliminates the entire class of "the generator emitted
  `Type1` instead of `CheckType`" bugs by construction. `--class-name
  Blueprint` is retained as a fallback for the root model, but the root's
  `title` is what actually determines the name.

- **Parity is enforced, not assumed.** Two mechanisms:

  1. **Drift detection.** `make models-check` regenerates the file in memory
     and diffs it against the committed file. A non-zero diff fails the
     build. This catches "someone changed the schema and forgot to
     regenerate" and "someone hand-edited a generated file outside a hand-
     tuned region." Implemented in `scripts/generate_models.py --check` and
     exercised by `tests/unit/models/test_generated_in_sync.py`.

  2. **Property-based parity.** `tests/unit/models/test_schema_model_parity.py`
     generates documents with Hypothesis, exercises both engines (`jsonschema`
     and `pydantic`), and asserts `schema_accepts(doc) == model_accepts(doc)`
     for every generated example. Each strategy mutates one field of a known-
     good baseline so that the property being tested is narrow and diagnosable
     — "for every value of `variables[0].type`, the two engines agree" rather
     than the unprovable "for every possible document, the two engines agree."

- **The schema is the referee.** A document the schema rejects is rejected by
  the funnel as a `SchemaValidationError` (exit 1, user error). A document
  the schema accepts but the model rejects is raised as a
  `ModelValidationError` (exit 5, internal error) and its hint says, in
  effect, "this is a Forge bug — run `make models-check` and report the
  diff." The two failure modes are distinguished by construction and cannot
  be misattributed at the call site.

- **Consumers import from the façade.** `forge.models` re-exports the public
  class names and pins the set in `tests/integration/test_models_public_api.py`.
  `forge.models.blueprint` is technically importable — nothing in Python
  prevents it — but it is not a supported import path; the facade exists so
  the codegen backend, the internal organisation, and the model hierarchy can
  change without touching call sites.

- **The generator lives outside the package it generates.**
  `scripts/generate_models.py` imports only the standard library. It cannot
  import `forge.models` because doing so would trigger `import
  forge.models.blueprint` — the very file the generator is invoked to
  repair. A generator that lives at `forge.models._generator` cannot fix a
  broken `blueprint.py`; a generator at `scripts/` can. The trade-off
  (a slight awkwardness in imports and invocation) is preferred over a
  bootstrap deadlock that has already been hit twice during this project's
  development.

## Consequences

### Positive

- **No drift between schema and models.** Adding a field to the schema and
  forgetting to regenerate fails CI (`make models-check`), not production.
  Hand-editing the generated file outside a `# region: hand-tuned` block
  fails the same way. The two failure modes are indistinguishable from the
  guard's point of view, which is correct — from the schema's perspective,
  both are "the file on disk does not match what the schema would produce."

- **Typed ergonomics without duplication.** Downstream code sees `blueprint.
  id` and `variable.type is VariableType.enum`. There is no second source of
  truth for the field set, because the field set comes from the schema.

- **Honest failure attribution.** A schema failure is a user error; a model
  failure is a Forge bug. Both are surfaced with distinct exception types and
  distinct exit codes, so scripts, users, and CI jobs can act on the actual
  cause rather than a misleading one.

- **Reproducible generation.** The generator's flags are pinned, the
  timestamp is disabled, and the output is deterministic. Regeneration on
  a different machine produces a byte-identical file, so `make models-check`
  is a meaningful signal in CI and not a "works on my machine" flake.

- **Cheap parity failures to diagnose.** The property tests use narrow
  mutation strategies with named baselines, so a failure reports something
  like "for version='1.0', the schema rejected but the model accepted" — a
  specific claim about a specific field. Debugging a broad Hypothesis
  strategy is far more expensive, and this design avoids that.

### Negative

- **Regeneration is a mandatory step.** Contributors who change the schema
  must remember to run `make models`. This is enforced by CI, not by
  convenience, so a forgotten regeneration shows up as a build failure at
  review time rather than as a pleasant local experience. The cost is one
  command; the benefit is drift is impossible to merge.

- **Hand-tuned regions require care.** They live inside the generated file,
  which means editing them is a two-step operation: edit, then verify
  regeneration preserves the edit (`make models` followed by
  `make models-check`). A contributor who edits a hand-tuned region without
  running `make models` will pass local tests but fail CI — the drift check
  will detect the mismatch on the next run, but only after the edit has
  landed. The mitigation is to `make models && make models-check` before
  committing, which is the same discipline as any codegen workflow.

- **Generator version pinning is load-bearing.** `datamodel-code-generator`
  0.25.x is pinned in `pyproject.toml`. An upgrade requires:
  re-running `make models`, reviewing the diff (which may be substantial if
  the generator changed its output formatting), and re-running the parity
  tests. That upgrade is a deliberate act, not a `pip install -U` — which is
  the correct trade-off but does add friction.

- **A failure in the parity test is not always a model bug.** Some parity
  failures are schema bugs (a rule that over- or under-accepts relative to
  the model's actual behaviour), and the correct fix is to update the schema,
  not the model. The failure message tells you *that* the engines disagree,
  not *which* one is wrong. Diagnosing requires reading both files and
  deciding which is the referee's intent. This is intrinsic to any parity
  test — "which of two engines is correct?" is only answerable by appeal to
  an external spec — and the schema's role as the referee resolves it, but
  the resolution is a human decision, not an automated one.

- **The schema's titles are now load-bearing for code names.** A schema
  `title` change renames a class in the model tree, which is a breaking
  change for the public facade. That coupling is intentional (it is what
  makes the schema self-documenting), but it means an innocuous-looking
  schema edit can cascade into a public API change. The facade test catches
  this — a renamed class fails `test_public_all_matches_expected_surface` —
  but the coupling is real and worth being aware of.

### Follow-up actions

- [ ] Extend `scripts/generate_models.py` to preserve `# region: hand-tuned`
      blocks at *module top* (outside any class body). Currently such blocks
      are rejected, which means top-level hand-written content like
      `_StrictModel` and the `SemVer`/`BlueprintId` aliases cannot survive
      regeneration. The fix is either to extend the block extractor to
      handle module-scope blocks or to require every hand-written component
      to live inside a class body. This is the highest-priority follow-up;
      the recurring erasure incidents in development all trace back to this
      limitation.
- [ ] Add a `make models-apply` target that regenerates and *shows* the diff
      before writing. The current `make models` writes unconditionally, and
      a contributor who runs it on a file with uncommitted hand-tuned edits
      can lose them if the anchor detection fails. Showing the diff first
      turns the write into a deliberate act.
- [ ] Publish `docs/reference/MODEL_REFERENCE.md` (currently drafted in a
      separate work item) explaining the class-by-class mapping, the two
      `model_validator` invariants, and the recommended `validate_blueprint_
      document` entry point. The reference exists as a document skeleton;
      it needs the final wording pass.
- [ ] Add a `tests/unit/models/test_model_mutation_strategies.py` that
      verifies each Hypothesis strategy in the parity test has been exercised
      at least once with a value the schema accepts and a value the schema
      rejects. Hypothesis `@given` tests are only as strong as their
      strategies; a strategy that happens to sample only the accept side is
      silent. A coverage check on the strategies catches that.
- [ ] Migrate `tests/unit/models/test_generated_in_sync.py` to invoke
      `scripts/generate_models.py` rather than the (now-deleted) module path.
      This is a mechanical update tracked in a separate PR; the ADR records
      the *decision* to have the generator at `scripts/`, not the specific
      test paths.

## Alternatives Considered

### Alternative 1: Hand-written Pydantic models

The obvious approach: write `forge.models.blueprint` by hand, mirroring the
schema. This is what most projects do, and it works well until it doesn't.

**Rejected because** it creates a second source of truth. Every schema change
would require a coordinated model change, and the coordination would be
enforced by nothing but review discipline. The two files would start in sync
and drift on the first change that touched one and forgot the other. The
project would then face the familiar failure mode: a user submits a document
the schema accepts, the model rejects it, and the failure points at the
wrong file. This is a documented failure of every "keep two schemas in sync
by hand" approach, and the mitigation (review checklists, periodic audits)
does not actually solve it.

Hand-written models have one genuine advantage — every field's `default` and
`description` can be tuned to the *consumer's* needs rather than inherited
from the schema. But that advantage is not worth the drift risk, and the
same ergonomics can be provided by the façade (aliases, helper methods)
without duplicating the field set.

### Alternative 2: Pydantic models as the sole source of truth

Invert the direction: write the Pydantic models by hand and *generate* the
JSON Schema from them (`TypeAdapter.json_schema()`).

**Rejected because** the schema must be present as a *resource* in the wheel
for `importlib.resources` (ADR-0007) and it must be stable across Pydantic
versions. Pydantic's `json_schema()` output has changed between 2.6 and 2.7
in observable ways (keyword ordering, `$ref` collapsing, `description`
formatting), so the generated schema would not be byte-stable. Downstream
consumers that parse the schema — the CLI's own `--dump-schema` output,
external tooling, the model generator itself — would see churn that
reflected Pydantic's release cadence rather than Forge's contract.

There is also a philosophical mismatch: the schema is a *format specification*
(a stable contract for the wire), and Pydantic models are a *runtime
convenience*. Making the runtime convenience the source of truth inverts the
dependency and would force every downstream consumer to import Pydantic just
to read a blueprint's shape. JSON Schema is the interoperable lingua franca;
Pydantic is a Python-specific convenience. The dependency should flow
schema → model, not the other way.

### Alternative 3: Generate and do not check in the generated file

Treat `forge.models.blueprint` as a build artifact: generate it at install
time, never commit it, and never inspect the generated diff in review.

**Rejected because** it removes the drift guard entirely. The whole point of
`make models-check` is that CI can detect drift *between* two committed
files. If the model file is not committed, drift cannot be detected — the
model is regenerated from the schema on every install, which means the two
are automatically consistent but the parity test has nothing to compare
against (it would be comparing a fresh generation against a fresh
generation). The parity test's value comes from the two files being
*independently committed* and therefore independently auditable.

There is also a distribution problem: generating at install time requires
`datamodel-code-generator` to be a *runtime* dependency, which it is not
(currently a dev-only dependency). Adding it to runtime dependencies for a
file that is deterministic from the schema would inflate the wheel for no
benefit.

### Alternative 4: Avoid Pydantic; use `dataclasses` + `jsonschema`

Parse validated documents into plain `@dataclass` instances, using
`jsonschema` for validation and a hand-written mapping layer for the
translation.

**Rejected because** `dataclasses` provide no validation, so every rule
would need to be hand-written twice — once in the schema, once in the
constructor. That is the "hand-written models" failure mode with more code.
`pydantic.dataclasses` exists and avoids this (it wraps Pydantic for
validation), but then the choice is between `pydantic.BaseModel` and
`pydantic.dataclasses`, and `BaseModel` is the better fit here:
`model_validate`, `model_dump`, and `model_config` are the entry points the
rest of the code relies on, and they are idiomatic on `BaseModel`.
`dataclasses` would be a stylistic preference masquerading as a design
choice.

### Alternative 5: Enforce parity without a property test

Rely solely on the drift check (`make models-check`) and skip the Hypothesis
parity test. The argument is that a model generated *from* the schema is
necessarily identical to it, so the property test verifies a tautology.

**Rejected because** it is not a tautology in practice. The generator emits
output that *approximates* the schema's rules:

- `allOf`/`if-then` rules are not translated (this is why hand-tuned regions
  exist).
- `default` values are translated as `Field(default=...)`, which Pydantic
  then applies during validation — but JSON Schema's `default` is not applied
  during validation. Those two semantics diverge in a specific, observable
  way: a document missing a field with a `default` is invalid under the
  schema's `required` but valid under the model's `default` (which silently
  fills it in). This is the exact class of divergence the property test is
  designed to catch, and it has already caught it once in this project's
  development — the schema's `required` array listed `variables` and `files`
  while they also had `default: []`, and the parity test surfaced the
  contradiction.

The property test is not verifying the generator; it is verifying that the
generator plus the hand-tuned regions plus the schema is a coherent contract.
Skipping it would leave the hand-tuned regions unverified, and those are
exactly the parts most likely to drift.

## References

- [ADR-0005](0005-stabilize-exit-codes-as-a-public-contract.md) — Exit codes
  as a public contract; establishes the schema-vs-model exit-code
  distinction.
- [ADR-0007](0007-blueprint-schema-as-canonical-contract.md) — The JSON
  Schema is the single canonical contract; establishes the schema as the
  referee for this ADR.
- [SCHEMA_REFERENCE.md](../reference/SCHEMA_REFERENCE.md) — Field-level
  reference for the JSON Schema itself.
- [MODEL_REFERENCE.md](../reference/MODEL_REFERENCE.md) — Field-level
  reference for the Pydantic model tree.
- [`datamodel-code-generator`](https://github.com/koxudaxi/datamodel-code-generator)
  — The codegen tool; pinned to 0.25.x in `pyproject.toml`.
- [Hypothesis](https://hypothesis.readthedocs.io/) — Property-based testing
  framework used for the parity test.
