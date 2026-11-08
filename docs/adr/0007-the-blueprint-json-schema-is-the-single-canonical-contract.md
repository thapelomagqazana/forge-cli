# 0007. The Blueprint JSON Schema Is the Single Canonical Contract

- **Status:** Accepted
- **Date:** 2026-11-08
- **Deciders:** thapelo
- **Consulted:** — (no external parties at MVP stage)
- **Informed:** — (solo project)

## Context

Forge ships blueprints — declarative documents that describe a class of
project (a FastAPI service, a TypeScript frontend, a Python automation
script) and tell the CLI how to scaffold it. A blueprint declares
metadata, user-facing variables, template-to-destination mappings,
conditional file inclusions, a verification contract, and a minimum
compatible Forge version.

By the time this ADR is written, five components already need to agree
on what a "valid blueprint" is:

1. **The blueprint loader** (`src/forge/cli/registry.py`) — refuses to
   open a blueprint whose shape is wrong, before the wizard runs.
2. **The interactive wizard** (`src/forge/cli/prompts.py`) — reads
   `variables` to decide which prompts to present and in what order.
3. **The scaffold generator** (`src/forge/generation/`) — reads `files`
   and `conditional_files` to know what to write.
4. **The verifier** (`src/forge/verification/`) — reads `verification`
   to know what post-generation checks to run.
5. **Test fixtures and future community blueprints** — must be
   authorable by people who have never seen Forge's Python source.

Before this decision, each component validated the parts of the
blueprint it cared about using hand-written checks. Five validators,
five interpretations of "what is a blueprint," five failure messages
per issue, five places to update when a new field is added.

The consequences were already visible at MVP scope:

- A field added to support one component (say, `maintainer` for the
  loader's metadata display) was not validated by the others, so a
  blueprint missing it loaded fine but crashed the wizard with a
  `KeyError`.
- Rejection messages disagreed. The loader said "missing field 'id'";
  the wizard said "blueprint cannot be used: no id"; the verifier
  silently skipped checks. A user fixing one component's error would
  reintroduce the others'.
- Test fixtures drifted. Each component's fixture set exercised its own
  corner of the blueprint's shape, and no single fixture set exercised
  the *whole* shape. A blueprint that passed all five component-level
  test suites could still fail in production because the components
  interpreted the shape differently.
- The **canonical question** — "is this blueprint valid?" — had five
  answers, none of which was authoritative.

The constraints were:

- **Local-first.** The CLI must work offline on a fresh install. Any
  schema served from a URL is unacceptable.
- **One-file portability.** The schema must be a single document, not a
  bundle of interdependent files. This rules out `$ref: "https://..."`
  and multi-file schema trees.
- **Language-agnostic.** The schema must describe the *structure* of a
  blueprint, not the language of the generated project. A
  `typescript.*` blueprint and a `python.*` blueprint must validate
  against the same schema.
- **Extensible without breaking.** A future schema change (adding an
  optional field) must not force every shipped blueprint to update
  simultaneously. A breaking change must be expressible as a versioned
  alternative, not a rewrite.
- **Machine-readable errors.** CI pipelines, editor plugins, and future
  command-line tools must be able to *act* on violations without
  parsing prose. A schema validation failure must carry structured
  pointers to the offending locations.

## Decision

**We will use a single JSON Schema (Draft 2020-12) as the sole
canonical definition of what a valid Forge blueprint is. Every
component that reads a blueprint will validate it against this schema,
using `forge.verification.validate_blueprint_document`, and will raise
`SchemaValidationError` — which carries structured `(json_pointer,
message)` pairs — on violation.**

Specifically:

1. **The schema lives at `src/forge/schemas/blueprint.schema.json`**
   and declares its own version via `properties.schema_version.const`.
   The current version is `1.0.0`. The document declares
   `additionalProperties: false` at every object level, so unknown
   fields are rejected, not silently ignored.

2. **The schema is bundled inside the Forge wheel** and loaded through
   `importlib.resources` via `forge.schemas.load_schema()`. It is not
   fetched from a URL. A fresh `pip install forge-cli` validates
   blueprints offline.

3. **Every blueprint read passes through
   `forge.verification.validate_blueprint_document`.** This function is
   the only entry point for shape validation. It is called by the
   loader, by tests, and by any future importer (a `forge import`
   command, a schema-aware editor plugin, a CI lint job). No component
   validates blueprint shape on its own.

4. **Schema violations raise `SchemaValidationError`** (a
   `ForgeError` subclass with `exit_code = ExitCode.USER_ERROR`). The
   exception carries a tuple of `(json_pointer, message)` pairs sorted
   by pointer for deterministic output, and a human-readable `__str__`
   that renders one bullet per violation. Callers can therefore act on
   errors in three ways: human-readable rendering, pointer lookup for
   programmatic handling, and exit-code branching for shell scripts.

5. **Semantic rules that JSON Schema cannot express** — such as "the
   `default` value must match the variable's declared `type`" and "the
   `condition` expression must parse" — are enforced by the
   verification layer's *input* validator
   (`validate_blueprint_inputs`) and the variable resolver, not by the
   schema. This split is documented in the schema reference and is
   load-bearing for the exit-code contract: a schema-shape violation is
   `USER_ERROR` (exit 1); a semantic failure caught by the loader is
   `BLUEPRINT_ERROR` (exit 2).

6. **Schema evolution is versioned, not backwards-compatible by
   accident.** When a breaking change is required, a new schema file is
   added alongside the old one (`blueprint.schema.2.0.0.json`), both
   ship in the wheel, and blueprints select a version via their
   `schema_version` field. Old blueprints continue to validate against
   the version they declared. This is the mechanism that lets Forge
   evolve without breaking every blueprint on every release.

7. **The schema is the source of truth for its own documentation.**
   `docs/reference/SCHEMA_REFERENCE.md` is a rendering of the schema's
   `description` fields and validation keywords. Every property and
   every `$defs` entry must have a non-empty `description`, enforced
   by `test_every_top_level_property_is_described` and
   `test_every_def_is_described`. A schema change that adds a
   property without a description fails CI before it can ship.

## Consequences

### Positive

- **One answer to "is this blueprint valid?"** The loader, wizard,
  generator, verifier, and tests all defer to the same rule. Changing
  a rule means editing one file (the schema), not five. Adding a
  field means editing one file, not five. Removing a field is a
  versioned schema change, not a coordinated refactor across the
  codebase.

- **Machine-readable violations.** `SchemaValidationError.errors` is a
  tuple of `(json_pointer, message)` pairs, sorted deterministically.
  A CI lint job can iterate over the tuple and post inline annotations
  on the offending lines of the YAML. An editor plugin can map pointers
  to gutter markers. A test can assert `("/id", ...) is in errors`
  instead of regex-matching prose. None of this was possible with
  per-component validation.

- **Aggregate errors in one pass.** A blueprint with five problems
  produces five `(pointer, message)` pairs in one exception, not five
  exceptions in sequence. Fixing an invalid blueprint is one edit and
  one re-run, not a ping-pong loop. This mirrors the aggregated-error
  decision already made for `MissingRequiredFlagsError` (ADR-0006) and
  is consistent with the CLI's overall error-UX philosophy.

- **Blueprints are portable.** A blueprint authored by someone who has
  never seen Forge's Python source can be validated by pointing
  `validate_blueprint_document` at its parsed YAML. The schema is the
  complete specification of what a blueprint is; no Python knowledge is
  required to author a valid one.

- **The schema is testable without fixtures.** Because the schema is a
  single JSON document with `$id` and `$defs`, the schema *itself* can
  be validated against the Draft 2020-12 meta-schema
  (`Draft202012Validator.check_schema`). A broken schema fails at
  validator construction, on the first call, with a clear error — not
  at the thousandth blueprint validation with a confusing message.

- **Evolvable without forking.** The versioned-schema-file design means
  a future v2 can coexist with v1 in the same wheel. Blueprints
  declare which version they target; the loader dispatches. Users are
  not forced to upgrade their blueprint catalog when Forge upgrades.

### Negative

- **Schema changes are heavyweight.** Adding a required field is a
  breaking change and requires a major version bump of the schema.
  Making a field optional is a minor bump. Fixing a typo in a
  description is a patch. None of these are *free* changes — the
  schema's version number and the `schema_version` constant must move
  in lockstep, and the `SCHEMA_REFERENCE.md` must be updated. That is
  the cost of having a single source of truth: it must be updated in
  one place, but that one place carries the weight of every consumer.

- **JSON Schema cannot express all invariants.** Some rules ("the
  `default` value matches the variable's type," "the `condition`
  expression parses") must live in code. This creates a two-layer
  model: the schema catches shape violations, the verification layer
  catches semantic violations, and the boundary between them is a
  documented convention rather than a structural property. The
  convention is enforced by tests and stated in the schema reference,
  but it can drift if a future contributor adds a semantic rule to the
  schema or a shape rule to the verification layer.

- **The schema is a bottleneck for blueprint authoring.** Before this
  decision, a blueprint author could ship a blueprint that violated
  the schema as long as the loader tolerated it. After this decision,
  every blueprint must conform to the schema or be rejected. That is
  the *point* of the decision, but it is also a real cost: authors
  must read the schema reference and conform to its patterns, and
  cannot ship experimental blueprints with ad-hoc extensions.

- **The `if/then` conditionals require a non-trivial error-flattening
  handler.** `jsonschema` reports `allOf`/`if/then` failures as
  wrapper errors whose message describes the *rule*, not the
  *violation*, and whose `.context` sometimes contains phantom
  `if`-branch artefacts that do not correspond to real document
  locations. Filtering those out — without filtering out the real
  `then`-branch violations that *should* be reported — requires the
  `_flatten_errors` helper in the verification handler. That helper
  is correct and tested, but it is also a piece of non-obvious code
  that exists *because* the schema uses `if/then` extensively. A
  schema with fewer conditionals would have a simpler handler.

### Follow-up actions

- [x] Author the schema at `src/forge/schemas/blueprint.schema.json`
      (Draft 2020-12, version 1.0.0, with full `$defs` and description
      coverage).
- [x] Implement `forge.schemas.load_schema()` and
      `forge.schemas.schema_version()` via `importlib.resources`.
- [x] Implement `forge.verification.validate_blueprint_document` as
      the single shape-validation entry point, with
      `SchemaValidationError` carrying structured pointers.
- [x] Write positive fixtures (three target blueprints) and negative
      fixtures (missing-id, extra-field, bad-version, bad-type, and
      three defensive cases), each isolating exactly one defect.
- [x] Write `docs/reference/SCHEMA_REFERENCE.md` as the human-readable
      companion, generated from the schema's own descriptions.
- [x] Enforce near-copy discipline on rejection fixtures via
      `test_fixture_has_exactly_one_violation`.
- [ ] When the loader lands, wire it to call
      `validate_blueprint_document` before returning a blueprint to
      any caller. (Tracked as part of the loader's own task.)
- [ ] When the wizard lands, ensure its variable prompts derive their
      validation rules from the schema-declared
      `validation.pattern` / `validation.enum` — not from
      per-prompt hand-written rules. (Tracked as part of the wizard's
      own task.)
- [ ] When a breaking schema change is required, add
      `blueprint.schema.2.0.0.json` alongside the v1 file and dispatch
      in `load_schema(version=...)`. (Not yet scheduled; the
      prerequisite is a concrete breaking change.)
- [ ] Add a CI job that validates every shipped blueprint (in
      `src/forge/blueprints/`) against the schema using
      `validate_blueprint_document` and fails on any violation. This
      converts the schema from "a thing blueprint *authors* run" to "a
      thing the *product* enforces." (Scheduled alongside the first
      shipped blueprint.)

## Alternatives Considered

### Alternative 1: Per-component validation with a shared base class

Each component (`loader`, `wizard`, `generator`, `verifier`) would
inherit from a shared `BlueprintValidator` base class that defined the
required fields, and each component would add its own component-specific
checks on top.

**Rejected because** it preserves the five-answers problem. The base
class would define a *subset* of the shape (the fields that all
components need), and each component would define its own *superset*
(the fields it needs). A blueprint valid against the wizard's validator
might be invalid against the generator's — the base class does not
prevent that; it only makes the *shared* subset explicit. The user
still sees component-specific error messages, and a blueprint author
still has to satisfy N validators, not one.

### Alternative 2: JSON Schema plus a Python validation DSL

We would use JSON Schema for the structural rules (types, patterns,
required fields) and a Python-based validation DSL (like Cerberus or
Pydantic) for the semantic rules (`default` matches `type`,
`condition` parses).

**Rejected because** it doubles the surface for blueprint authors.
The schema is the one file they must understand; adding a second file
of Python rules means they must understand both, and the two can
disagree. The split between "shape" and "semantic" is a *consequence*
of JSON Schema's expressiveness, not a design goal — we do not want to
expand it into a second validation language. The semantic rules we
have are few and stable; keeping them in the verification handler's
Python code (where they are testable as ordinary functions) is simpler
than maintaining a DSL.

### Alternative 3: Pydantic models as the schema

Use Pydantic `BaseModel` classes to define the blueprint shape, generate
JSON Schema from them for documentation, and use Pydantic's
`model_validate` for runtime validation.

**Rejected because** it inverts the source of truth. Pydantic's
`BaseModel` would be the canonical document, and the JSON Schema would
be a *derived artefact*. Blueprint authors would need Pydantic (a
Python library) to validate their blueprints, which violates the
"language-agnostic" constraint. It also makes schema evolution harder:
changing a `BaseModel` field is a Python change, with Python
conventions (type annotations, `Field(...)` calls), not a JSON change.
A future schema v2 would be a Python module, not a data file, and the
versioned-file mechanism would become a versioned-module mechanism —
possible, but heavier than it needs to be.

The inverse — deriving Pydantic models *from* the JSON Schema — is a
reasonable future enhancement if and when the codebase wants
type-checked access to blueprint fields. But that enhancement is
downstream of this decision, not an alternative to it.

### Alternative 4: No schema, just documentation

Publish a prose specification of what a blueprint is and rely on
per-component checks and human reviewers to enforce it.

**Rejected because** it is what we already had, and it failed. Prose
cannot be machine-validated, cannot produce structured error pointers,
and cannot be tested in CI. A blueprint author would still need to
satisfy N hand-written validators, and the validators would still
drift. The cost of writing a schema is a few hours; the cost of
maintaining prose across five components is continuous and unbounded.

---

## References

- [ADR-0005: Stabilize exit codes as a public contract](0005-stabilize-exit-codes-as-a-public-contract.md)
  — establishes the exit-code contract that `SchemaValidationError`
  participates in (exit 1, `USER_ERROR`).
- [ADR-0006: Dual-mode input handling with a single validation funnel](0006-dual-mode-input-handling.md)
  — establishes the "single funnel" pattern that the schema extends
  from *inputs* to *documents*.
- [`src/forge/schemas/blueprint.schema.json`](../../src/forge/schemas/blueprint.schema.json)
  — the canonical schema document this ADR describes.
- [`docs/reference/SCHEMA_REFERENCE.md`](../reference/SCHEMA_REFERENCE.md)
  — the human-readable companion to the schema.
- [JSON Schema Draft 2020-12 specification](https://json-schema.org/draft/2020-12)
  — the specification the schema targets.
- [`jsonschema` Python library](https://python-jsonschema.readthedocs.io/)
  — the runtime validator used by `forge.verification`.
