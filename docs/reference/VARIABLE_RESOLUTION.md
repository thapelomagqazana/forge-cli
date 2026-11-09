# Variable Resolution Reference

> A guide to how ``forge create`` turns raw user input — CLI flags,
> interactive prompt answers, blueprint defaults — into validated,
> typed values that the rendering engine can trust.
>
> This document is the *contract*. It describes what users can
> expect from each source, what rules apply, and what each failure
> mode looks like when it reaches stderr. It is written for two
> audiences: **users** who want to know *"why didn't my flag take
> effect?"*, and **contributors** who want to know *"where does
> this value come from and why?"*.

---

## The pipeline at a glance

```
     CLI flags ───────────────┐
                              │
     prompt answers ──────────┼──►  VariableResolver.resolve  ──►  ResolvedValues
                              │                                    (immutable,
     blueprint defaults ──────┘                                     validated,
                                                                    typed)
```

`VariableResolver.resolve(blueprint, cli_args, prompt_answers)` is a
**pure function**. Given the same inputs, it produces the same output,
every time — no I/O, no environment lookups, no prompts, no clock.
That purity is deliberate: it makes the resolver trivially testable,
safe to call in batch or in a future HTTP handler, and immune to the
"it works on my machine" class of bug.

The output is `Result[ResolvedValues, list[ValidationError]]`:

- `Ok(ResolvedValues)` — every variable was resolved and validated.
- `Err(list[ValidationError])` — every variable that *failed*, in
  one aggregated list. The resolver never stops at the first error.

---

## Precedence

For each blueprint variable, the resolver picks the value from the
**highest-priority source that supplies a non-`None` value**:

| Priority | Source | What counts as "supplied" |
|---------:|--------|---------------------------|
| 1 (highest) | `--var name=value` | The flag was passed with a non-`None` value. `--var author=` (empty string) *counts as supplied* — the empty value is a real value the user chose, and the validator decides whether it is acceptable. |
| 2 | Interactive prompt answer | The wizard asked and the user answered. Empty strings are *not* supplied — the prompt engine rejects them (via `ask_text`'s `allow_empty=False` default) before the resolver ever sees them. |
| 3 | Blueprint default | The variable's `default:` field in `blueprint.yaml` is non-`null`. |
| — | *(absent)* | None of the above. `required: true` → error; `required: false` → omitted from `ResolvedValues`. |

### Why `None` means "absent", but `""` / `0` / `False` do not

The resolver needs to distinguish three cases cleanly:

1. **The value is not present** — the user did not pass `--var`, the
   prompt did not fire, and the blueprint has no default.
2. **The value is present and empty/falsy** — the user passed
   `--var author=""`, or a default of `false`, or a prompt answer
   of `0`.
3. **The value is a legitimate non-value** — e.g. a variable whose
   declared type is `boolean` and whose value is `False`.

Only case 1 is "absent". The resolver uses `None` as its *sentinel*
for "not provided" and treats every other value — including falsy
ones — as supplied. That is why:

- `--var enabled=false` **disables** the flag (it does not fall
  through to a blueprint default of `true`).
- `--var count=0` **sets** the count to zero (it does not fall
  through to a default of `1`).
- `--var name=""` **reaches the validator** with the empty string,
  which then rejects it with a clear message — rather than silently
  using the default.

The `None` sentinel is a *caller convention*, not a schema rule. It is
what `argparse` populates an unused option with, so it is the natural
sentinel at this boundary. A blueprint that declares `default: null`
means "no default" (see § Defaults and `null`), not "the default is
`None`".

---

## Defaults and `null`

A blueprint variable's `default:` field is honoured **only when it
is not `null`**. The two cases are:

| Blueprint declares | Behaviour |
|--------------------|-----------|
| `default: "forge"` | Used when no CLI / prompt value is supplied. |
| `default: false` (for a boolean) | Used when no CLI / prompt value is supplied. |
| `default: null` | Treated as *no default* — falls through to the required/optional decision. |
| *(no `default:` field)* | Same as `default: null`. |

`null` (or absent) means "no default" rather than "the default is the
`None` value" because `None` is never a meaningful default for a
variable whose type is `string`, `boolean`, or `enum` (the three types
v1 supports). Treating `null` as "the default is `None`" would defeat
the `required: true` check for any author who wrote `default: null`
expecting "no default", which is the most common reading.

### Worked example

```yaml
# blueprint.yaml (excerpt)
variables:
  - id: author
    type: string
    description: Author name used in LICENSE and pyproject metadata.
    required: true
    default: forge
```

| Invocation | Resolved `author` |
|------------|-------------------|
| `forge create … --var author=alice` | `"alice"` (CLI wins) |
| *(no `--var`, interactive prompt answers `bob`)* | `"bob"` (prompt wins) |
| *(no `--var`, non-interactive)* | `"forge"` (default wins) |
| `forge create … --no-interactive` with `default: null` | `Err([required.missing])` |

---

## Per-type rules

Each variable has a `type` (`string`, `boolean`, or `enum`) and
optional `validation` rules (`pattern`, `enum`). The resolver
validates in this order:

1. **Type coercion** — turn the raw value into the declared type.
2. **Pattern** (strings only) — match against `validation.pattern`.
3. **Enum membership** (enums only) — value must be in `validation.enum`.
4. **Named validator** (if the id has one) — e.g. `project_name`,
   `python_package`, `npm_package`.

The first failure wins (one error per variable), but *every*
variable's failure is reported in the `Err` list. One bad variable
does not stop the resolver from reporting another bad variable.

### `string`

- **Coercion:** must already be a string. `to_string` does *not*
  call `str()` on non-strings — silent casting is a footgun (a
  YAML `version: 1.10` parses as `1.1`, and `str(1.1) == "1.1"`,
  silently losing the trailing zero). A non-string reaches the
  resolver, the resolver reports `type.mismatch`, and the user
  fixes the YAML or the flag.
- **Pattern:** if `validation.pattern` is set, the value must
  match it *in full* (`re.fullmatch`, not `re.match`). A broken
  pattern in the blueprint is reported as
  `blueprint.pattern.invalid` — a blueprint-author bug, not a
  user bug.
- **Empty string:** accepted by coercion. A blueprint that wants
  to reject empty strings must express that in the pattern (e.g.
  `pattern: ".+"`) or in a named validator. This keeps the
  coercion honest — it does one thing (type check) and does not
  decide policy.

### `boolean`

- **Coercion:** accepts `true`/`false`/`yes`/`no`/`y`/`n`/`on`/`off`
  and `1`/`0`, case-insensitively, with surrounding whitespace
  stripped. Accepts *real* booleans (from YAML defaults and prompt
  answers) unchanged.
- **Rejects:** every other spelling, including `""`, `"maybe"`,
  `"1.0"`, `"2"`, `"None"`, `"null"`. The closed set is a policy
  decision: extending it invites the `bool("false") == True` class
  of bugs, and users who need a wider input surface can wrap the
  command in their own shell script.
- **No validation rules.** A boolean has no meaningful `pattern`
  or `enum`, so a blueprint that sets them is rejected at load time
  (the `VariableDefinition` model validator catches it).

### `enum`

- **Coercion:** must be a string. The string must be an *exact*
  member of `validation.enum` (case-sensitive).
- **Why case-sensitive:** an enum's allowed values are
  blueprint-author-controlled. Case-folding would make a legal set
  like `["dev", "DEV"]` unmatchable, so the resolver refuses to
  guess. Users who mistype the case get a clear "not in allowed
  set" error naming the options.
- **`validation.enum` is required.** A blueprint that declares
  `type: enum` without an `enum` list is rejected at load time by
  `VariableDefinition`'s validator, not at resolution time. That
  distinction matters: it is a blueprint bug, not a user bug.

---

## Named validators

Some variable ids have special rules attached to them — not because
the *type* is special, but because the *meaning* of the id is. These
are the ids with named validators in `forge.resolution.names`:

### `project_name`

A project name must be:

- lowercase;
- begin with a letter (not a digit, not an underscore);
- contain only letters, digits, and hyphens;
- not end with a hyphen;
- at least one character.

| Accepted | Rejected |
|----------|----------|
| `my-api` | `My-API` (uppercase) |
| `myapi` | `my_api` (underscore — npm/URL convention is hyphens) |
| `a` | `1api` (leading digit) |
| `x1` | `my-` (trailing hyphen) |
| `svc-2` | `my project` (space) |

**Why underscores are rejected:** the same name is reused as an npm
package name in TypeScript blueprints, and npm's convention is
hyphens. Using a *different* validator (`python_package`) for
Python-package-shaped names keeps the two rules distinct.

### `python_package`

A Python package name must be a valid *identifier*:

- `str.isidentifier()` must return `True` (this is the stdlib's
  authority on the question — it handles Unicode correctly);
- not a Python keyword (`class`, `import`, `lambda`, …);
- not start with a digit.

| Accepted | Rejected |
|----------|----------|
| `my_package` | `my-package` (hyphen — not an identifier) |
| `_internal` | `123abc` (leading digit) |
| `café` | `class` (keyword) |
| `pkg2` | `my package` (space) |

**Why hyphens are rejected:** `import my-package` is a syntax error.
The hyphen-vs-underscore distinction between `project_name` and
`python_package` is the *whole point* of having two validators.

### `npm_package`

An npm package name must be:

- lowercase;
- contain only letters, digits, hyphens, dots, and underscores;
- *not* start with a dot or underscore;
- be 1–214 characters;
- not be in the reserved set (`node_modules`, `favicon.ico`,
  common Node.js built-in module names).

Scoped names (`@scope/name`) are **not yet supported** — they
require a nested directory structure the scaffolding layer does not
create. The validator rejects them with an explicit "not yet
supported" message rather than failing later during file creation.

| Accepted | Rejected |
|----------|----------|
| `my-package` | `My-Package` (uppercase) |
| `my.package` | `.hidden` (leading dot) |
| `my_package` | `_private` (leading underscore) |
| `a` | `node_modules` (reserved) |
| `my-pkg_v2.1` | `@scope/name` (scoped — not supported) |

---

## Error reporting

Every failure produces a `ValidationError` with four fields the CLI
relies on:

| Field | Purpose |
|-------|---------|
| `path` | Dotted path to the variable (`variables.author`). |
| `message` | One-sentence description of *what* is wrong. |
| `hint` | One-sentence suggestion of *what to do*. Never empty. |
| `code` | Machine-readable failure class (`required.missing`, `type.mismatch`, `enum.not_allowed`, `pattern.invalid`, `name.invalid`, `blueprint.pattern.invalid`). |

The resolver **aggregates** every error and returns them all at
once. A user with three missing variables fixes them in one edit,
not three. That is the reason for `Err(list[ValidationError])`
instead of a single exception: aggregation is a feature, and it is
only possible if the resolver treats failures as *data*, not as
*control flow*.

### Error codes

| Code | Meaning | Typical fix |
|------|---------|-------------|
| `required.missing` | A `required: true` variable had no value from any source. | Pass `--var name=value`, or add a default to the blueprint. |
| `type.mismatch` | The raw value could not be coerced to the declared type. | Use the correct type (`true`/`false` for booleans, etc.). |
| `enum.not_allowed` | The value is a string but not in `validation.enum`. | Pass one of the allowed values (the error lists them). |
| `pattern.invalid` | The value did not match `validation.pattern`. | Adjust the value to match the pattern (the error names it). |
| `name.invalid` | A named validator rejected the value. | See the specific named-validator section above. |
| `blueprint.pattern.invalid` | The *blueprint itself* has a broken pattern or missing required field. | Fix the blueprint's `validation` block, not the user input. |

The last code is the odd one out: it reports a *blueprint author*
bug, not a user bug. The distinction matters for routing — CI gates
often want to fail only on blueprint bugs, while interactive use
wants to fail on both. Keying on the `code` field lets a caller
distinguish the two without parsing prose.

---

## Interactive vs. non-interactive mode

The resolver itself is **mode-agnostic** — it takes whatever
`cli_args` and `prompt_answers` it is given and returns the same
`Result`. The *command* (`forge create`) is responsible for
collecting the inputs into those two dicts, and it does so
differently per mode.

| Aspect | Interactive | Non-interactive |
|--------|-------------|-----------------|
| Missing required variable | The wizard *prompts* for it. | The command fails with `MissingRequiredFlagsError` (for `--name`) or `Err([required.missing])` (for blueprint variables). |
| Invalid variable value | The wizard re-prompts inline with the error and hint. | The command fails with `Err([name.invalid])` or similar, aggregated with any other failures. |
| Ctrl+C | Raises `PromptCancelledError` → exit 1, "Cancelled." on stderr. | N/A (no prompts fire). |
| Flags win | Supplied flags suppress the corresponding prompt. | Supplied flags are the only source (besides defaults). |

The **one** asymmetry that matters: in interactive mode, the wizard
validates inline using the *same* named validators the resolver
would apply — so the user gets real-time feedback and does not hit
a wall after answering every question. In non-interactive mode,
there is no wizard, so the resolver validates and reports all
failures at once.

Both modes converge on the resolver. Adding a validation rule to
the resolver therefore applies to both modes automatically — that
is the whole point of the layer (ADR-0011).

---

## Relationship to the loader

The blueprint itself is validated by the **loader**
(`forge.loading.BlueprintLoader`, ADR-0010) *before* the resolver
ever sees it. That means:

- A malformed blueprint (bad schema, missing required fields,
  malformed SemVer) is reported by the loader with its own error
  list, not by the resolver.
- The resolver can assume every variable it sees has a valid
  `type`, a valid `validation` block (if any), and a well-typed
  `default`.

The two layers are complementary: the loader reports *blueprint
shape* errors, the resolver reports *user input* errors. A CI gate
that wants to fail on both runs `forge validate` (which uses the
loader) *and* `forge create --dry-run` (which uses the resolver).

---

## What the resolver does *not* do

The resolver is deliberately narrow. It does not:

- **Read files or touch the filesystem.** A future "parent
  directory must be writable" check belongs in the generation
  pipeline, not the resolver — the resolver's job is *values*,
  not *filesystem state*.
- **Prompt for values.** That is the command's job (see
  `cli.commands.create._collect_interactive_answers`). The
  resolver is a pure function; it does not know whether the
  user is at a terminal.
- **Load the blueprint from disk.** That is the loader's job. The
  resolver takes a parsed `Blueprint` object.
- **Decide the output directory.** The command computes
  `--output / name`; the resolver does not know what a
  destination is.
- **Cache results.** Repeated calls with the same inputs produce
  the same output. There is no state to invalidate, and no cache
  to clear when the user edits a file.

If a future feature needs one of the above, it belongs in a layer
that is *aware* of that concern, not here.

---

## Extending the resolver

Adding a new validation rule means editing **one file** and
**one test file**:

| Concern | File | Test file |
|---------|------|-----------|
| New type (e.g. integer) | `resolution.coercion` + `VariableType` enum | `test_type_coercion.py` |
| New named validator | `resolution.names` + `NAMED_VALIDATORS` registry | `test_<name>.py` |
| New per-variable rule | `resolution.validators` (add a phase) | `test_validators.py` |
| New error code | the file that raises it + `docs/reference/VARIABLE_RESOLUTION.md` | the test that exercises it |

**Do not** add validation logic to `cli.commands.create` — that
would reintroduce the two-funnel problem the resolution layer
exists to eliminate. The command's only job is to *collect* inputs
and hand them to the resolver; the resolver owns the *rules*.

---

## See also

- **`SCHEMA_REFERENCE.md`** — the JSON Schema for `blueprint.yaml`,
  including the `variables` block's field-by-field semantics.
- **`MODEL_REFERENCE.md`** — the Pydantic models that mirror the
  schema and are what the resolver consumes.
- **`VALIDATION_ERRORS.md`** — the loader's error catalogue (a
  different layer; same `ValidationError` shape).
- **ADR-0011** — "Variable Resolution Is a Pure Function over
  Ordered Sources with a Single Validation Funnel" (the
  architectural decision behind this document).
- **ADR-0006** — "Dual-Mode Input Handling with a Single
  Validation Funnel" (the command-layer companion).
