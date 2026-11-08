# Blueprint Model Reference

The typed Python models in `forge.models` are the runtime projection of the
canonical Blueprint JSON Schema. This document is the human-readable map of
that projection: **which class corresponds to which schema construct**,
**which invariants are enforced where**, and **what a consumer can rely on**
when importing from the facade.

If you are looking for the *rules* a blueprint must satisfy, read
[`SCHEMA_REFERENCE.md`](SCHEMA_REFERENCE.md) instead — that document is
about the wire format. This one is about the Python objects you get after a
blueprint has been validated.

---

## Where the models live

```
forge.models              ← public facade; import from here
forge.models.blueprint    ← generated; do not import from here directly
```

The facade exists so the codegen backend, the internal organisation of the
generated file, and the model tree's class hierarchy can change without
touching call sites. Consumers should treat `forge.models.blueprint` as an
implementation detail, even though it is technically importable.

The names re-exported from `forge.models` are the public surface. That set is
pinned by `tests/integration/test_models_public_api.py`; adding to it or
removing from it is a deliberate change to the contract.

---

## The model tree at a glance

```
Blueprint                         ← root document
├── schema_version: Literal["1.0.0"]
├── id, name, version, description, category, maintainer   (identity)
├── variables: list[VariableDefinition]
│   └── validation: VariableValidation
├── files: list[FileEntry]
├── conditional_files: list[ConditionalFileEntry]
│   └── files: list[FileEntry]
├── verification: list[CheckDefinition]     (≥ 1)
└── compatibility: Compatibility

BlueprintMetadata                 ← view returned by Blueprint.metadata()
```

Every class inherits from `forge.models.blueprint._StrictModel`, which sets:

| `model_config` key    | Value    | Why |
|-----------------------|----------|-----|
| `extra`               | `"forbid"` | Mirrors the schema's `additionalProperties: false`. An unknown key fails identically in both engines. |
| `strict`              | `True`   | Disables Pydantic's default coercion. `"true"` will not become `True`; `1` will not satisfy an `int` field. The schema requires the wire value to be typed correctly; matching that here keeps the parity test honest. |
| `frozen`              | `True`   | Blueprints are read-only descriptors once loaded; the generation pipeline may pass the same instance to multiple workers. Frozen makes that safe. |
| `populate_by_name`    | `True`   | Reserved for a future alias-based renaming; harmless today. |
| `str_strip_whitespace`| `False`  | Free-text fields (name, description) may contain leading/trailing whitespace the caller wrote deliberately; stripping is the caller's decision. |

`_StrictModel` is not part of `__all__`. It is an implementation detail of the
model tree; consumers should not subclass it.

---

## Class-by-class reference

### `Blueprint`

Root document. Flat — every field of the JSON document is a field of the
model, with no nesting, so schema↔model parity is a straight-line comparison.

| Attribute | Python type | Schema source | Notes |
|-----------|-------------|---------------|-------|
| `schema_version` | `Literal["1.0.0"]` | `properties.schema_version.const` | Expressed as a literal so a future v2 migration is a compile-time change, not a runtime check. |
| `id` | `str` (constrained) | `properties.id.pattern` | Dotted lowercase identifier, 3–128 chars. |
| `name` | `str` (1–120 chars) | `properties.name` | Human-readable; the wizard selector reads this. |
| `version` | `str` (SemVer) | `properties.version.pattern` | SemVer 2.0.0. |
| `description` | `str` (1–2000 chars) | `properties.description` | |
| `category` | `Category` enum | `properties.category.enum` | |
| `maintainer` | `str \| None` | `properties.maintainer` | Optional. |
| `variables` | `list[VariableDefinition]` | `properties.variables.items` | Order is preserved and significant (the wizard prompts in this order). Defaults to `[]` if absent in the document. |
| `files` | `list[FileEntry]` | `properties.files.items` | Defaults to `[]`. |
| `conditional_files` | `list[ConditionalFileEntry]` | `properties.conditional_files.items` | Defaults to `[]`. |
| `verification` | `list[CheckDefinition]` | `properties.verification.items` | **Required and non-empty** (`minItems: 1`). |
| `compatibility` | `Compatibility` | `properties.compatibility` | |

Convenience method:

- **`Blueprint.metadata() -> BlueprintMetadata`** — returns an identity-only
  view. It is a *derived* object (constructed on each call from the root's
  fields); it stores no state of its own. Use it when you need only
  `id`/`name`/`version`/`description`/`category`/`maintainer` (listings,
  selectors, `forge list` output) and want to avoid dragging the full
  document into that layer.

### `BlueprintMetadata`

Ergonomic view of a blueprint's identity. Returned by
`Blueprint.metadata()`. Not present in the JSON Schema — it is a Python
convenience, not a document type. Its fields mirror the identity block of
`Blueprint` exactly, so a regression that hard-coded a value or dropped a
field in the view would be caught by `test_metadata_view_mirrors_root_fields`.

### `VariableDefinition`

One user-facing variable. Ordering within `Blueprint.variables` is significant.

| Attribute | Python type | Constraint | Notes |
|-----------|-------------|-----------|-------|
| `id` | `VariableId` | snake_case, ≤ 64 | Referenced from templates and conditional expressions. |
| `type` | `VariableType` | `string` · `boolean` · `enum` | |
| `description` | `str` | 1–500 | Shown in the wizard. |
| `required` | `bool` | | Defaults to `True`. |
| `default` | `Any \| None` | | Runtime *kind* must match `type`; enforced by a `model_validator` (see below). |
| `validation` | `VariableValidation \| None` | | For `enum` variables, must be present with a non-empty `enum` list. |

Enforced invariants (all in hand-tuned `# region: hand-tuned` blocks):

- **`type="enum"` requires `validation.enum` with at least one entry.** The
  schema expresses this with `allOf`/`if-then`; Pydantic has no direct
  equivalent, so it lives in a `model_validator`.
- **`default`'s kind must match `type`.** For `string` and `enum`, `default`
  must be a `str`. For `boolean`, `default` must be a `bool` (explicitly
  checked because `bool` is a subclass of `int`, which is the classic
  `isinstance(True, int)` footgun). For `enum`, `default` must additionally
  be one of the declared `validation.enum` values.

These are the only two model-validator rules in the tree. They are also the
reason the file has `# region: hand-tuned` markers — without them, the
generator would erase the validators on the next `make models`.

### `VariableValidation`

Optional validation rules for a variable's value. Both fields are optional and
complementary — the schema permits either or both.

| Attribute | Python type | Constraint |
|-----------|-------------|-----------|
| `pattern` | `str \| None` | RE2-compatible regex; 1–2048 chars. |
| `enum` | `list[str] \| None` | ≥ 1 element; each element ≥ 1 char. |

### `FileEntry`

A source-template → destination-file mapping. Used both for the top-level
`files` list and for entries inside a `ConditionalFileEntry`.

| Attribute | Python type | Constraint |
|-----------|-------------|-----------|
| `source` | `str` | 1–400 chars; relative; no path traversal. |
| `destination` | `str` | 1–400 chars; relative; may contain `{{variable}}` interpolation. |

The path-traversal rule is enforced by the schema's `pattern` (a negative
lookahead for leading `/` and for `..` segments). The model inherits the rule
verbatim from the schema; it does not add its own.

### `ConditionalFileEntry`

A group of file mappings applied only when `condition` evaluates true at
generation time.

| Attribute | Python type | Constraint |
|-----------|-------------|-----------|
| `condition` | `str` | 1–500 chars. |
| `files` | `list[FileEntry]` | ≥ 1 entry. |

`condition` is a simple boolean expression over variable IDs (`include_docker
== true`). It is deliberately not a Python expression or AST — the evaluator
is sandboxed elsewhere, and keeping the type a plain `str` means the schema,
the model, and the evaluator can evolve independently.

### `CheckDefinition`

A single post-scaffold verification check. `type` is the discriminator.

| Attribute | Python type | Constraint | Notes |
|-----------|-------------|-----------|-------|
| `id` | `CheckId` | snake_case, ≤ 64 | |
| `type` | `CheckType` | `command` · `required_file` | |
| `command` | `str \| None` | 1–400 | Required when `type="command"`. |
| `path` | `str \| None` | 1–400 | Required when `type="required_file"`. |
| `timeout` | `int` | 1–3600; default 60 | Seconds before a `command` check fails. |

Enforced invariants (in a hand-tuned `# region: hand-tuned` block):

- **`type="command"` requires `command`, forbids `path`.**
- **`type="required_file"` requires `path`, forbids `command`.**

The schema's `if/then` pairs enforce the *positive* half of both rules; the
*negative* half (forbidding the wrong payload) is model-only, because a
`required_file` check that also carries a `command` is a copy-paste mistake
that the schema does not catch. The model is deliberately stricter here. The
parity test's mutation strategy is scoped accordingly — it does not generate
wrong-payload combinations, because those are a documented divergence, not a
parity failure.

### `Compatibility`

Minimum CLI version required to consume the blueprint.

| Attribute | Python type | Constraint |
|-----------|-------------|-----------|
| `min_forge_version` | `SemVer` | SemVer 2.0.0. |

The schema could grow `max_forge_version` or a supported-range matrix
additively; the model would regenerate and gain the corresponding fields
without breaking existing documents.

### Enumerations

| Enum | Members | Used by |
|------|---------|---------|
| `Category` | `api`, `automation`, `frontend`, `library`, `cli`, `other` | `Blueprint.category` |
| `VariableType` | `string`, `boolean`, `enum` | `VariableDefinition.type` |
| `CheckType` | `command`, `required_file` | `CheckDefinition.type` |

All three are `str`-mixin enums, so comparisons against the raw JSON string
work (`blueprint.category == "api"` is `True`) as well as comparisons against
the enum member (`blueprint.category is Category.api`). Use the member form
in new code — it is exhaustive-checked by mypy.

---

## Error shapes

Two failure types describe "the blueprint document is invalid":

- **`SchemaValidationError`** — the document failed JSON Schema validation.
  Exit code **1** (user error). The bundled schema is the referee, so if it
  refuses a document, that is the failure surfaced.
- **`ModelValidationError`** — the document passed schema validation but the
  Pydantic model rejected it. Exit code **5** (internal error). This is a
  **parity regression** between the schema and the models — a Forge bug, not
  a user mistake. It is raised by `validate_blueprint_document` and is
  accompanied by a hint pointing at `make models-check`.

Both carry the same structured error shape:

```
exc.errors  →  tuple[tuple[str, str], ...]
               each entry is (json_pointer, message)
               pointers follow RFC 6901
```

Example:

```python
from forge.verification import validate_blueprint_document
from forge.core.exceptions import SchemaValidationError

try:
    validate_blueprint_document(document, source="my-blueprint.yaml")
except SchemaValidationError as exc:
    for pointer, message in exc.errors:
        print(f"{pointer}: {message}")
    # /id: 'id' is a required property
    # /version: '1.0' does not match '...'
```

Scripts should iterate `exc.errors` directly and branch on the pointer. The
`__str__` rendering is stable but not contractual — the message text of any
individual violation is whatever `jsonschema` or Pydantic produces, and that
is not part of Forge's contract.

Consumers that do not care about the distinction between the two error types
can catch `forge.core.exceptions.ForgeError` — but the recommended pattern is
to catch the specific type, because the correct user response differs
(fix-your-blueprint vs. report-a-bug). Catching the base is a code smell that
treats two different problems as one.

---

## Using the models

The recommended entry point is `forge.verification.validate_blueprint_document`.
It is the **only** function that both validates and parses; the return value
is the `Blueprint` instance.

```python
from pathlib import Path
import yaml

from forge.verification import validate_blueprint_document

document = yaml.safe_load(Path("blueprint.yaml").read_text())
blueprint = validate_blueprint_document(document, source="blueprint.yaml")

# Typed access — no dict-poking.
print(blueprint.id)
print(blueprint.category.value)
for variable in blueprint.variables:
    print(variable.id, variable.type.value)
```

Do **not** call `Blueprint.model_validate(document)` directly unless you have
a specific reason. It skips the schema check, which means:

- A schema-only violation (a `required` field missing, an unknown property, a
  pattern mismatch) is caught by the *model* rather than by the schema, which
  changes the failure type from `SchemaValidationError` (exit 1) to
  `ModelValidationError` (exit 5). The user gets the wrong error.
- The error pointer is reported by Pydantic, which uses its own location
  format. The two are now normalised to the same shape (both are RFC 6901),
  but bypassing the funnel means the user's failure is attributed to Forge
  rather than to their document.

`Blueprint.model_validate` is legitimate in one place: the parity test, which
deliberately exercises both engines separately to prove they agree. Everywhere
else, use the funnel.

---

## What this reference is not

- **Not the schema.** For the wire format — every `pattern`, every
  `minLength`, every `allOf` rule — see
  [`SCHEMA_REFERENCE.md`](SCHEMA_REFERENCE.md). The model inherits those
  rules from the schema; it does not restate them.
- **Not the CLI contract.** For exit codes, error messages, and the shape of
  what a user sees, see [`exit-codes.md`](exit-codes.md) and
  [`streams.md`](streams.md).
- **Not exhaustive.** Every field is documented, but the *rationale* for
  individual constraints (why SemVer is `min_length=5`, why
  `min_forge_version` is SemVer rather than a range) lives in the schema's
  own `description` fields. This document is the map; the schema is the
  territory.

---

## Maintaining this document

The model tree is generated from the schema (ADR-0008). When the schema
changes:

1. Regenerate with `make models`. The `# region: hand-tuned` blocks in
   `forge.models.blueprint` preserve the model validators; everything else is
   overwritten by the generator.
2. Update the corresponding row in this document. If a class was added or
   removed, update the tree diagram at the top and the class-by-class
   reference.
3. If a new validator was added inside a hand-tuned block, add its rule to
   the "Enforced invariants" section of the owning class.
4. If a new class was added to the schema with a `title`, it will be emitted
   by the generator and will appear in the model tree; it also needs a
   section here, and it must be added to `_EXPECTED_PUBLIC_NAMES` in
   `tests/integration/test_models_public_api.py`.

The drift test (`tests/unit/models/test_generated_in_sync.py`) will fail if
step 1 is skipped. This document has no automated drift check — it is
maintained by the person changing the schema, and its accuracy is part of
that change's review.
