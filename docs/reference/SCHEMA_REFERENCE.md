# Blueprint Schema v1.0.0 — Field Reference

This document is the **human-readable companion** to
[`src/forge/schemas/blueprint.schema.json`](../../src/forge/schemas/blueprint.schema.json),
the canonical, machine-readable contract that every Forge blueprint
must satisfy (ADR-0007).

If this document and the schema ever disagree, **the schema wins** —
and the disagreement is a bug in this file. Every field, constraint,
and example below is a rendering of the schema's own `description`
and validation keywords. When the schema changes, this document must
change with it, or the CI test
`tests/unit/schemas/test_schema_shape.py::test_every_top_level_property_is_described`
will (correctly) fail before the drift can ship.

---

## Table of contents

1. [Purpose](#purpose)
2. [File location and loading](#file-location-and-loading)
3. [Structural rules](#structural-rules)
4. [Top-level fields](#top-level-fields)
5. [Nested shapes (`$defs`)](#nested-shapes-defs)
   - [variable](#variable)
   - [fileMapping](#filemapping)
   - [conditionalFiles](#conditionalfiles)
   - [verificationCheck](#verificationcheck)
6. [Semantic rules that JSON Schema cannot express](#semantic-rules-that-json-schema-cannot-express)
7. [Worked example](#worked-example)
8. [How violations are reported](#how-violations-are-reported)
9. [Versioning policy](#versioning-policy)

---

## Purpose

A *blueprint* is the unit of project scaffolding in Forge. It declares:

- **What** a project is (metadata: `id`, `name`, `version`, `category`).
- **What the user must decide** (`variables`).
- **What files are written** (`files`, `conditional_files`).
- **What must be true after generation** (`verification`).
- **What version of Forge can consume it** (`compatibility`).

The schema exists so that *every* consumer of a blueprint — the CLI
wizard, the loader, tests, and any future importer — agrees on what
a valid blueprint is. There is exactly one answer to the question
"is this blueprint valid?" and it is the schema.

---

## File location and loading

The schema ships **inside the Forge wheel** as a static resource:

```
src/forge/schemas/blueprint.schema.json
```

It is loaded at runtime through `importlib.resources` by
`forge.schemas.load_schema()`, which caches the parsed document. This
matters for three reasons:

1. **`pip install forge-cli` works offline immediately.** The schema
   is not fetched from a URL and cannot be missing on a fresh install.
2. **Wheels, zips, and editable installs behave identically.** A
   naive `open(__file__ + "/schema.json")` would break under any of
   those; `importlib.resources` handles them all.
3. **Consumers cannot fork the schema.** `load_schema()` is the only
   sanctioned way to read it. A caller who opens the file directly
   is bypassing the cache, and is likely to bypass the versioned
   loader in a future schema-v2 world.

The document declares its own version at
`properties.schema_version.const`, which is `"1.0.0"`. That is the
**schema version**, not the blueprint's own version (which is
`version`, a separate field).

---

## Structural rules

Every blueprint document is a JSON object (and, in practice, a YAML
file whose top level is a mapping). The schema enforces:

- **`type: "object"` at the top level.** A blueprint is not a list,
  a string, or `null`.
- **`additionalProperties: false` at every object level.** Any
  unknown key — at the top level, inside a variable, inside a file
  mapping, and so on — is rejected. This is what makes typos like
  `varibales:` fail loudly instead of silently creating a blueprint
  with zero variables.
- **No inheritance and no `$ref` to external schemas.** Every
  `$ref` in the schema points at `#/$defs/...` within the same
  document. This is what allows the schema to be a single, portable
  file with no network dependencies at validation time.

---

## Top-level fields

All fields are **required** unless marked *(optional)*.

### `schema_version`

- **Type:** `string`
- **Constraint:** must equal `"1.0.0"`
- **Purpose:** Identifies which revision of the schema the document
  was authored against.

This field is a **constant**, not a free-form version. It exists so
that a future schema v2 can coexist with v1 without either being
ambiguous. A document that declares `schema_version: "2.0.0"` is
rejected by the v1 schema, which is the correct behavior — it
prevents a document authored against a future contract from being
silently accepted by an older validator.

### `id`

- **Type:** `string`
- **Pattern:** `^[a-z][a-z0-9]*(\.[a-z][a-z0-9]*)+$`
- **Length:** 3–128 characters
- **Purpose:** Stable, dotted identifier used to reference the
  blueprint from the CLI (`forge create python.fastapi.api`).

The pattern is deliberately strict:

- **Lowercase only.** IDs are case-insensitive in user-typed input
  but case-sensitive in the schema, to avoid `Python.FastAPI` and
  `python.fastapi` being treated as different blueprints by a
  registry that normalizes.
- **Dotted.** At least two segments (`python.api`, not `python`).
  Dots are the natural grouping key for the wizard's selector
  (`python.*`, `typescript.*`) and the future version-resolver.
- **Letters and digits only inside segments.** **No hyphens**,
  underscores, or spaces. A hyphen in a segment (`python.fast-api`)
  is rejected — which is why the fixtures use `sample.badversion`
  and not `sample.bad-version`.
- **Each segment starts with a letter.** `1st.segment` is invalid;
  `first.segment` is valid.

### `name`

- **Type:** `string`
- **Length:** 1–120 characters
- **Purpose:** Human-readable display name shown in the wizard's
  selector.

`name` is free-form. It may contain spaces, hyphens, mixed case,
and punctuation. It is *not* used as an identifier anywhere in the
system — only for display. A blueprint whose `id` is
`python.fastapi.api` might have a `name` of `Python FastAPI API`.

### `version`

- **Type:** `string`
- **Pattern:** SemVer (`MAJOR.MINOR.PATCH[-prerelease][+build]`)
- **Purpose:** Semantic version of the **blueprint itself**,
  independent of both the schema version and the Forge CLI version.

`version` is what changes when the blueprint's templates or
variables evolve. It is not the schema version (that is
`schema_version`) and not the Forge version (that is
`compatibility.min_forge_version`). A blueprint at `1.2.3` may
require Forge `1.0.0` and be authored against `schema_version 1.0.0`
— three independent version numbers, each tracking a different
concern.

SemVer rules are enforced in full: `1.0` fails (no patch), `1.0.0.0`
fails (too many parts), `01.0.0` fails (leading zero),
`1.0.0-alpha.1` passes (prerelease), `1.0.0+build.5` passes (build
metadata).

### `description`

- **Type:** `string`
- **Length:** 1–2000 characters
- **Purpose:** One-paragraph explanation of what the blueprint
  generates.

`description` is shown to the user during blueprint selection, in
`forge list --verbose`, and in generated documentation. It should
answer "what will this project be?" — not "what does this blueprint
do internally?" The schema enforces a non-empty value because an
undescribed blueprint is a blueprint users cannot choose between.

### `category`

- **Type:** `string`
- **Enum:** `api` | `automation` | `frontend` | `library` | `cli` | `other`
- **Purpose:** Coarse classifier used for grouping in the selector.

The enum is closed. Adding a category is a **schema change** (bump
`schema_version`), not a field change — which is the correct
trade-off because `category` is a search facet, not free-form text,
and every value must be understood by the selector's grouping logic.

### `maintainer` *(optional)*

- **Type:** `string`
- **Length:** ≤ 200 characters
- **Purpose:** Contact handle or email for the blueprint's owner.

Presence is optional; content is free-form. A blueprint without a
`maintainer` is valid and common for one-off or experimental
blueprints. The field exists so that blueprints shipped as part of
a distributed registry can identify their owner.

### `variables`

- **Type:** `array` of [`variable`](#variable)
- **Default:** `[]`
- **Purpose:** Ordered list of user-facing variables the wizard
  prompts for.

The list is **ordered** and **preserved**. The wizard presents
variables in declaration order, so the choice to list `author`
before `python_version` before `include_docker` is a UX decision,
not an incidental one. Reordering variables is a meaningful change
to the blueprint.

See [`variable`](#variable) for the shape of each element.

### `files`

- **Type:** `array` of [`fileMapping`](#filemapping)
- **Default:** `[]`
- **Purpose:** Static template-to-destination mappings applied
  unconditionally.

Every entry in `files` is written to the generated project, no
matter what variable values the user chose. Conditional file
inclusion lives in `conditional_files`, not here — keeping the two
separate so a reader can see at a glance "these are always written,
those are conditional."

### `conditional_files`

- **Type:** `array` of [`conditionalFiles`](#conditionalfiles)
- **Default:** `[]`
- **Purpose:** Additional file mappings applied only when a
  condition expression evaluates true.

Each entry is a `(condition, files)` pair. The condition is
evaluated against the collected variable values; if it is true, the
`files` are written, otherwise they are not. This is how a
blueprint expresses "include a Dockerfile only if the user asked
for Docker."

### `verification`

- **Type:** `array` of [`verificationCheck`](#verificationcheck)
- **Minimum items:** 1
- **Purpose:** Contract the generated project must satisfy after
  scaffolding.

At least one verification check is required. A blueprint with
nothing to verify cannot be distinguished from a broken one after
generation — the absence of a check is indistinguishable from the
absence of a guarantee.

### `compatibility`

- **Type:** `object` (closed; `additionalProperties: false`)
- **Required keys:** `min_forge_version`
- **Purpose:** Minimum Forge CLI version required to consume this
  blueprint.

#### `compatibility.min_forge_version`

- **Type:** `string`
- **Pattern:** SemVer
- **Purpose:** The oldest Forge version that can consume this
  blueprint.

A blueprint that uses schema features introduced in Forge `1.2.0`
declares `min_forge_version: "1.2.0"`; an older Forge refuses to
load it with a clear error (exit code 2, `BLUEPRINT_ERROR`) rather
than producing a partially-scaffolded project. This is the schema's
forward-compatibility mechanism: it lets blueprints evolve without
breaking older CLIs.

---

## Nested shapes (`$defs`)

The schema defines four reusable shapes. Each is referenced from the
top-level fields above; each has its own `additionalProperties:
false` and its own required-fields list.

### `variable`

A single blueprint variable. Declared as an ordered list entry so
the wizard presents prompts in the author's intended order.

**Required fields:** `id`, `type`, `description`, `required`.

#### `variable.id`

- **Type:** `string`
- **Pattern:** `^[a-z][a-z0-9_]*$`
- **Length:** 1–64 characters

The identifier used to reference the variable in templates
(`{{ author }}`), in conditional expressions (`include_docker == true`),
and in the CLI (`--var author=...`). Lowercase letters, digits, and
underscores only; **no hyphens** (hyphens in variable names would
conflict with the CLI's flag parser, which treats them as
separators).

#### `variable.type`

- **Type:** `string`
- **Enum:** `string` | `boolean` | `enum`

The value type. Each type implies different prompt and validation
behavior:

| Type      | Prompt              | Validation                              |
|-----------|---------------------|-----------------------------------------|
| `string`  | Free-text input     | Optional `validation.pattern`           |
| `boolean` | Yes/No confirm      | No validation allowed                   |
| `enum`    | Single-choice menu  | `validation.enum` is **required**       |

The conditional enforcement in the schema ensures that
`type: "enum"` without `validation.enum` is rejected, and that
`type: "boolean"` with a `validation` block is rejected. Both
mistakes would produce a wizard that cannot ask the right question.

#### `variable.description`

- **Type:** `string`
- **Length:** 1–500 characters

Explanation shown to the user while prompting. Like the blueprint's
`description`, this is what makes a variable actionable: `"Port
number the generated service listens on"` is a description; `"port"`
is not.

#### `variable.required`

- **Type:** `boolean`

Whether the wizard must collect a value. When `false`, the wizard
offers a default and lets the user press Enter to accept it. When
`true`, the wizard re-prompts until a valid value is provided.

The schema cannot enforce "`required: true` implies a default exists"
because defaults are optional; the wizard's behavior is to leave
`required: true` variables blank until the user fills them.

#### `variable.default` *(optional)*

- **Type:** any
- **No schema constraint on type or shape.**

The default value. Its runtime type must match the declared
`variable.type` — a `default: "8000"` for a `type: integer` variable
would be wrong, but the schema cannot enforce this because `integer`
is not a valid `variable.type` in v1 (only `string`, `boolean`,
`enum` are). The **verification layer** enforces the runtime type
match when the wizard applies defaults.

If a `type: string` variable has no `default`, and `required: false`,
the wizard offers an empty string. If a `type: enum` variable has
no `default` and `required: false`, the wizard offers the first
`validation.enum` value.

#### `variable.validation` *(optional)*

- **Type:** `object` (closed; `additionalProperties: false`)
- **Only permitted when `type` is `string` or `enum`.** The schema
  rejects any `validation` block on a `boolean` variable.

##### `variable.validation.pattern` *(optional)*

- **Type:** `string`
- **Length:** 1–500 characters
- **Purpose:** A regular expression the string value must match in
  full.

Only meaningful when `variable.type` is `"string"`. The pattern is
**not** anchored implicitly — use `^...$` explicitly if you want an
exact-match constraint. The wizard applies the pattern on every
keystroke and refuses to advance until the input satisfies it.

##### `variable.validation.enum`

- **Type:** `array` of `string`
- **Minimum items:** 1
- **Unique items:** required (no duplicates)
- **Purpose:** The set of allowed values.

**Required** when `variable.type` is `"enum"`. Each element must be
a non-empty string. The wizard presents these as a choice menu; the
CLI flag parser accepts any of them as a `--var` value.

### `fileMapping`

A template-to-destination mapping. Applied unconditionally from the
top-level `files` array, or conditionally from within a
`conditionalFiles` entry.

**Required fields:** `source`, `destination`.

#### `fileMapping.source`

- **Type:** `string`
- **Length:** 1–400 characters
- **Pattern:** must not start with `/`, must not contain `..` as a
  path segment.

Path to the template file, relative to the blueprint's root
directory. The `..` prohibition prevents a blueprint from reaching
outside its own directory tree. The absence of a leading `/` is
enforced by the pattern `^(?!/)(?!.*\.\.(?:/|$)).+$` — a file
mapping cannot reference an absolute path on the host system.

#### `fileMapping.destination`

- **Type:** `string`
- **Length:** 1–400 characters
- **Pattern:** same traversal prohibition as `source`

Path in the generated project. May include `{{ variable }}`
interpolation (`src/{{ project_name }}/main.py`). The same
`..`-prohibition applies because a template author must not be able
to write a file *above* the generated project's root directory,
regardless of what value the user provided for `project_name`.

### `conditionalFiles`

A group of file mappings applied only when a condition expression
evaluates true.

**Required fields:** `condition`, `files`.

#### `conditionalFiles.condition`

- **Type:** `string`
- **Length:** 1–500 characters

A **simple boolean expression** over variable IDs. The grammar is
deliberately restricted:

- Comparison against a literal: `include_docker == true`,
  `python_version == "3.12"`, `license_id != "MIT"`.
- Boolean combination: `include_docker == true and include_ci == true`.
- No function calls, no arithmetic, no string manipulation.

The restriction is a **security** decision: the condition is
evaluated by the generator, and an unrestricted expression language
would require a sandbox. A simple equality-and-boolean grammar has
no attack surface, no sandbox, and no surprises. A future schema
v2 that needs richer expressions will introduce a proper expression
language — the restriction here is honest about what v1 supports.

#### `conditionalFiles.files`

- **Type:** `array` of [`fileMapping`](#filemapping)
- **Minimum items:** 1

The file mappings applied when the condition is true. Each is a
`fileMapping`, with the same `source`/`destination` traversal
prohibitions.

### `verificationCheck`

A single check the generated project must satisfy.

**Required fields:** `id`, `type`.

#### `verificationCheck.id`

- **Type:** `string`
- **Pattern:** `^[a-z][a-z0-9_]*$`
- **Length:** 1–64 characters

Stable identifier for the check, used in reports and in
`forge verify --check <id>`. Lowercase, underscores, no hyphens —
same rationale as `variable.id`.

#### `verificationCheck.type`

- **Type:** `string`
- **Enum:** `command` | `required_file`

The kind of check. `type` is a **discriminator**: the shape of the
rest of the check depends on its value.

| Type            | Required payload | Meaning                                |
|-----------------|------------------|----------------------------------------|
| `command`       | `command`        | Run a shell command; success is exit 0. |
| `required_file` | `path`           | Assert a file exists.                  |

The schema enforces the discriminator with an `allOf` of
`if/then` branches: a check that declares `type: command` without a
`command` field is rejected, as is a check that declares
`type: required_file` without a `path`.

#### `verificationCheck.command` *(required when `type: command`)*

- **Type:** `string`
- **Length:** 1–400 characters

The shell command to run. Executed with the project's root as the
working directory, with the shell environment inherited from
`forge verify`'s invocation. Success is exit code 0; any other exit
code fails the check.

#### `verificationCheck.path` *(required when `type: required_file`)*

- **Type:** `string`
- **Length:** 1–400 characters

The path to assert exists, relative to the project's root. The path
must be reachable from the project root — the same traversal
prohibition as `fileMapping.destination` applies transitively
because `path` is a `required_file` check on a file that should
have been written *inside* the project.

#### `verificationCheck.timeout` *(optional)*

- **Type:** `integer`
- **Minimum:** 1
- **Maximum:** 3600
- **Default:** 60

Seconds before a `command` check is considered failed. For
`required_file` checks, `timeout` has no effect (the check is
synchronous and instantaneous).

The 3600-second ceiling is a **safety** constraint: a check that
takes more than an hour is almost certainly an infinite loop or a
broken command, and the user should not wait for it. It also bounds
the worst-case runtime of `forge verify` on a blueprint the user
did not author.

---

## Semantic rules that JSON Schema cannot express

A handful of invariants are enforced by the verification layer
(`forge.verification.validate_blueprint_inputs` and the variable
resolver, when it lands) rather than by the schema. They are listed
here for completeness so that blueprint authors know what additional
constraints apply:

- **`default` must match `type`.** A `string` variable with
  `default: 42` fails at runtime, not at validation time. The
  schema cannot enforce this because `integer` is not a valid
  `variable.type` in v1 — the type match is a semantic rule.
- **`validation.pattern` must be a valid regular expression under
  the host's regex dialect.** The schema enforces only that the
  `pattern` string is non-empty. The verification layer compiles it
  and reports a `UserInputError` if compilation fails.
- **`conditionalFiles.condition` must be parseable by the condition
  grammar.** The schema enforces only that the `condition` string is
  non-empty. The generator parses it and fails the blueprint load
  if the grammar cannot consume it.
- **A `fileMapping.source` must exist on disk.** The schema checks
  the *shape* of the path, not the *existence* of the file. The
  blueprint loader reports a `BlueprintError` (exit 2) at load time
  if a declared template is missing — this is the difference between
  "the schema is fine but the blueprint is broken" (loader) and
  "the blueprint's shape is invalid" (schema).

The distinction matters for exit codes:

- **Schema rejection** → exit 1, `USER_ERROR`, `SchemaValidationError`.
- **Blueprint loader failure** → exit 2, `BLUEPRINT_ERROR`, `BlueprintError`.

A user who authored an invalid blueprint sees exit 1 and can fix
their own file. A user whose blueprint references a missing template
sees exit 2, which also points at their file but flags the failure as
"the blueprint is not usable" rather than "the blueprint is not
well-formed."

---

## Worked example

A minimal but complete blueprint that exercises every shape:

```yaml
schema_version: "1.0.0"
id: python.automation.script
name: Python Automation Script
version: 1.0.0
description: >-
  A single-entry-point Python automation project with argument
  parsing, structured logging, and a pytest harness.
category: automation
maintainer: "@platform-team"

compatibility:
  min_forge_version: 1.0.0

variables:
  - id: author
    type: string
    description: Author name used in pyproject metadata.
    required: true
    default: forge

  - id: python_version
    type: enum
    description: Python minor version targeted by the generated project.
    required: true
    default: "3.12"
    validation:
      enum: ["3.11", "3.12", "3.13"]

  - id: include_docker
    type: boolean
    description: Include a Dockerfile.
    required: false
    default: false

files:
  - source: templates/pyproject.toml.j2
    destination: pyproject.toml

  - source: templates/src/cli.py.j2
    destination: src/{{ project_name }}/cli.py

conditional_files:
  - condition: include_docker == true
    files:
      - source: templates/Dockerfile.j2
        destination: Dockerfile

verification:
  - id: has_pyproject
    type: required_file
    path: pyproject.toml

  - id: tests_pass
    type: command
    command: python -m pytest -q
    timeout: 180
```

Every field in this example is either required by the schema or
present to demonstrate a distinct shape:

- `category: automation` demonstrates the enum.
- `variables[1].validation.enum` demonstrates the `enum`-requires-validation rule.
- `conditional_files` demonstrates the `if/then` grammar.
- `verification` includes both check types, side by side.
- The `id` value `python.automation.script` matches the pattern;
  `sample.bad-version` would not.

---

## How violations are reported

When a blueprint fails validation,
`forge.verification.validate_blueprint_document` raises
`SchemaValidationError`, which carries a tuple of
`(json_pointer, message)` pairs. Pointers follow RFC 6901:

| Pointer                            | Meaning                                      |
|------------------------------------|----------------------------------------------|
| `""` (rendered as `/`)             | The document root                            |
| `/id`                              | The `id` field's value                       |
| `/variables/0`                     | The first variable                           |
| `/variables/0/type`                | The first variable's `type` field            |
| `/verification/2/command`          | The third check's `command` field            |
| `/conditional_files/0/files/1`     | The second file in the first condition group |

Pointers are rendered with `~0` for `~` and `~1` for `/` inside a
segment (RFC 6901 requirement), though no field name in the current
schema contains either character.

The **message** is `jsonschema`'s human-readable description of the
violation. It is stable across the `jsonschema` versions the project
supports, but not contractual — scripts should branch on the
*pointer* and treat the message as diagnostic output for humans.

Example error output for a document missing `id` and with a bad
`version`:

```
Blueprint 'my-blueprint.yaml' failed schema validation:
  • /id: 'id' is a required property
  • /version: '1.0' does not match '^(0|[1-9]\d*)\.(0|[1-9]\d*)\...'
```

Every violation is reported in one pass. There is no "fix one,
re-run, fix the next" loop — the aggregate behavior is a deliberate
UX decision (see ADR-0007).

---

## Versioning policy

The schema's own version lives in `properties.schema_version.const`
and is currently `"1.0.0"`. It follows the same SemVer rules as every
other version field in Forge:

- **MAJOR** — a change that rejects documents the previous version
  accepted. Adding a required field, tightening a pattern, removing
  an enum value. Every blueprint authoring against the old schema
  must update.
- **MINOR** — a change that accepts documents the previous version
  rejected without rejecting any previously valid documents. Adding
  an optional field, loosening a pattern, widening an enum.
- **PATCH** — a change that does not affect acceptance at all.
  Clarifying a description, fixing a typo in a message template.

The **current** schema is v1.0.0. When a breaking change is
required, the schema is updated to v2.0.0 **and** a second file
(`blueprint.schema.2.0.0.json`) is added alongside it. Both files
ship together, and `load_schema(version=...)` selects between them.
Blueprints declare their version via `schema_version`, and the
loader picks the right validator. Old blueprints continue to
validate against v1; new blueprints can validate against v2.

This policy exists so that Forge can evolve its blueprint contract
without breaking every blueprint authored for the previous version.
The cost of carrying two schemas indefinitely is bounded by the
number of major versions published; the cost of *not* carrying them
is unbounded, because blueprints ship independently of the CLI and
cannot be rewritten on every Forge release.

---

## See also

- [`src/forge/schemas/blueprint.schema.json`](../../src/forge/schemas/blueprint.schema.json) — the canonical schema.
- [`docs/adr/0007-blueprint-schema-as-canonical-contract.md`](../adr/0007-blueprint-schema-as-canonical-contract.md) — why the schema is the single source of truth.
- [`docs/reference/exit-codes.md`](exit-codes.md) — the exit-code contract for `forge create`, `forge verify`, and other commands.
- [`docs/guides/adding-a-command.md`](../guides/adding-a-command.md) — how to consume a blueprint from a new command.
