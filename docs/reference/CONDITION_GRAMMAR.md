# Condition Grammar

**Version:** 1.0.0

Conditions control whether a group of files in a blueprint is included
in the generated project. They are deliberately **not** Python, **not**
Jinja, and **not** arbitrary expressions. This document is the complete
reference for what a condition can say, what it means, and what it
refuses.

The grammar is small on purpose. Every feature below exists because a
real blueprint needed it; every feature *not* below was considered and
rejected, with the reasoning stated so the rejection is a decision, not
an omission.

---

## Supported forms

A condition is a single comparison: one variable, one operator, one
literal. No chaining, no parentheses, no arithmetic. The complete set:

| Form | Example | Meaning |
|------|---------|---------|
| `var == 'string'` | `database == 'postgresql'` | equality with a single-quoted string |
| `var == "string"` | `database == "postgresql"` | equality with a double-quoted string |
| `var != 'string'` | `database != 'sqlite'` | inequality with a single-quoted string |
| `var != "string"` | `database != "sqlite"` | inequality with a double-quoted string |
| `var == true` | `include_docker == true` | equality with the boolean literal `true` |
| `var == false` | `include_docker == false` | equality with the boolean literal `false` |
| `var != true` | `include_docker != true` | inequality with `true` |
| `var != false` | `include_docker != false` | inequality with `false` |
| `var == <int>` | `python_major == 3` | equality with an integer literal |
| `var != <int>` | `python_major != 2` | inequality with an integer literal |

That is the whole language. Every row is parsed by a specific rule in
`src/forge/conditions/grammar.py`; the parser and this table are
checked against each other by `tests/unit/conditions/test_grammar_docs.py`,
so neither can drift from the other silently.

---

## Variable names

A variable in a condition must be a **lowercase snake_case identifier**:

```
[a-z_][a-z0-9_]*
```

Concretely:

- **First character** is a lowercase letter (`a`–`z`) or an underscore
  (`_`). Leading underscores are valid, matching Python's own
  convention.
- **Remaining characters** are lowercase letters, digits, or
  underscores.
- **No uppercase, no hyphens, no spaces, no dots.**

The grammar enforces this rule when *parsing* — so `Database == 'x'`
fails with a syntax error rather than reaching evaluation, even though
`Database` might look like a typo the user meant. Enforcing the rule
at parse time makes the error message accurate: it says "this is not
a valid identifier", not "this variable isn't defined". The two are
different problems with different fixes.

### Why the rule is strict

A condition that parses is a condition that names a *variable* the
blueprint author might have declared, or might have typo'd. The
grammar requires valid identifiers because:

1. **Typo detection.** `databse == 'x'` (transposed letters) parses
   fine, reaches the evaluator, and produces the same
   `condition.undefined_variable` error a typo of any kind produces.
   The error message tells the user exactly which name was not found.

2. **Cross-tool consistency.** Variables are declared in the
   blueprint as snake_case `id` values (the schema enforces this).
   Requiring the condition's variable reference to use the same
   grammar means the reader of a blueprint sees the same spelling
   in both places, and a copy-paste from the declaration to the
   condition produces valid syntax.

3. **Predictability.** No "does the DSL allow this identifier?"
   questions. If it's a valid `VariableDefinition.id`, it's a valid
   condition variable. If it isn't, both files are wrong in the same
   way, and the fix is the same.

### If your condition needs a hyphenated name

Rename the variable. Hyphens in identifiers are a cross-language
hazard (bash, Python, YAML anchors, and JSON Pointers all disagree
about what they mean). The DSL's snake_case rule forces the
variable to be safe in every consumer downstream.

---

## Literal kinds

The grammar supports three literal kinds, each with its own rules.

### Strings

Strings are quoted with either single quotes (`'...'`) or double
quotes (`"..."`). The two are **semantically identical** — the choice
is aesthetic:

```yaml
# These two conditions mean exactly the same thing:
- condition: database == 'postgresql'
  files: [...]
- condition: database == "postgresql"
  files: [...]
```

String literals do **not** support escape sequences. There is no
`\'`, no `\"`, no `\n`, no `\uXXXX`. This is deliberate: the DSL
exists for category comparison, and any string that would need an
escape is a bug in the blueprint's design (a category value should
never contain a quote). If you find yourself needing an escape,
declare the variable as `type: string` with a `pattern` validation
and use the pattern's constraints instead.

Case sensitivity: string comparison is **case-sensitive**.
`'postgresql'` and `'PostgreSQL'` are different values, and the
evaluator will not fold case. A variable declared with an `enum`
validation that lists `"postgresql"` expects that exact spelling.

### Booleans

Boolean literals are **lowercase only**: `true` and `false`. This
matches YAML and JSON conventions, so the same blueprint reads
identically in any tool that consumes it.

```yaml
# Correct — matches YAML/JSON spelling:
- condition: include_docker == true
  files: [...]

# Rejected with condition.syntax — Python's capitalised spelling:
- condition: include_docker == True
  files: [...]
```

Uppercase `True`/`False` are **syntax errors**, not silently-accepted
aliases. The reason is cross-tool consistency: a user who writes
`True` in their blueprint and then consumes the same blueprint in a
tool that reads YAML literally will get `True` as a *string*, not a
boolean, and their condition will silently never match. Making the
capitalised form an error at the DSL layer catches the inconsistency
before it becomes a debugging mystery downstream.

### Integers

Integer literals are signed: an optional `-` followed by one or more
digits. No plus sign, no leading zeros beyond what parses as a
decimal integer, no hexadecimal, no binary, no numeric separators.

```yaml
# Valid:
- condition: python_major == 3
  files: [...]
- condition: retry_count != -1
  files: [...]

# Rejected with condition.syntax:
- condition: python_major == 03         # leading zero
  files: [...]
- condition: python_major == 0x3        # hex
  files: [...]
- condition: python_major == 1_000      # numeric separator
  files: [...]
```

**Floats are not supported.** There is no `3.14`, no `1.0`, no
`.5`. Float equality is a footgun — `0.1 + 0.2 != 0.3` — and a
blueprint branching on a float is almost always a mistake. If a
condition genuinely needs a numeric threshold, use an integer (a
year, a major version, a count) or encode the comparison in the
variable's `enum` validation and compare against the enum.

---

## Semantics

### Type-aware comparison

Comparison is **type-aware**: two values are equal only if they are
the *same type* and equal. Cross-type pairs are **never equal**,
regardless of their Python value:

| Left | Right | Equal? | Why |
|------|-------|--------|-----|
| `"3"` | `3` | No | str vs int |
| `3` | `"3"` | No | int vs str |
| `"true"` | `true` | No | str vs bool |
| `true` | `"true"` | No | bool vs str |
| `1` | `true` | No | int vs bool — *the subclass trap* |
| `true` | `1` | No | bool vs int — *the subclass trap* |
| `0` | `false` | No | int vs bool |

The `bool`/`int` cases are the ones a naive implementation gets wrong:
in Python, `isinstance(True, int)` is `True`, so a comparison that
just does `actual == expected` would accept `True == 1`. The DSL
explicitly refuses this, because a blueprint comparing a boolean
variable to an integer literal is a bug in the blueprint (the
variable was declared with the wrong type, or the condition is
comparing against the wrong constant), and silent coercion would
hide it.

`!=` is the strict negation of `==` under this rule: if two values
are of different types, they are unequal, so `!=` is `True`.

### Undefined variables are errors

A condition that references a variable **not present in the resolved
variable set** produces a `condition.undefined_variable` error. It
is *never* treated as `false`, never silently skipped, never coerced
to an empty value.

```yaml
# If `database` was never declared and never supplied:
- condition: database == 'postgresql'
  files: [...]
  # → ConditionError with code condition.undefined_variable
  # → message: "undefined variable: database"
  # → hint: "Add 'database' to the blueprint's variables, or provide a value at create time."
```

The rationale: a blueprint author who typo'd a variable name, or
forgot to declare it, would otherwise get a *silently different*
project — one missing files they expected, with no diagnostic. They
would discover the bug only after the generated code failed to
compile (or worse, compiled into something subtly wrong). The
evaluator's job is to make that mistake loud and immediate.

### Determinism

For the same inputs (blueprint + resolved variables), evaluation is
purely deterministic: identical survivor set, identical ordering,
identical error messages. There is no randomness, no time-dependence,
no environment access. This is what makes blueprint evaluation
diffable in CI, snapshot-testable, and reproducible across machines.

---

## Explicitly *not* supported

The following are **rejected at parse time** with `condition.syntax`.
They are not oversights; each was considered and refused, and the
reasoning is stated so a future contributor knows to change the ADR,
not just the parser.

### Boolean combinators: `and`, `or`, `not`

```yaml
# Rejected:
- condition: database == 'postgresql' and region == 'us'
  files: [...]
```

**Why refused:** combinators invite arbitrary boolean logic, which
is exactly the escape hatch a declarative language exists to prevent.
Two conditions on two groups accomplish the same thing, with the
added benefit that each group's inclusion is independent and
independently testable. If you need files included only when *both*
conditions hold, encode the conjunction in the variable's type — an
`enum` whose values are the meaningful combinations — and compare
against the enum.

### Arithmetic: `+`, `-` (as operator), `*`, `/`

```yaml
# Rejected:
- condition: python_major + 1 == 4
  files: [...]
```

**Why refused:** arithmetic in conditions is arithmetic in the
blueprint, which means the blueprint computes values the resolver
would need to reproduce. That is the start of a general-purpose
language, and the DSL is not one. Compute the value in the variable
resolver (via a `pattern`/`enum` validator) or in the variable's
declared default; the condition should compare against a *concrete*
value, not compute one.

### Comparison operators other than `==` and `!=`

```yaml
# Rejected:
- condition: python_major >= 3
  files: [...]
- condition: python_major > 2
  files: [...]
- condition: python_major <= 4
  files: [...]
- condition: python_major < 5
  files: [...]
```

**Why refused:** ordering comparisons introduce the same problem
as arithmetic — the DSL becomes a computation language. They also
have a subtler hazard: ordering only makes sense on ordered types
(ints, strings in some languages), and the DSL supports booleans
where `>` is meaningless. A DSL that allowed `python_major >= 3` but
rejected `include_docker > true` would be arbitrary. Restricting to
`==`/`!=` keeps the operators applicable to every type the DSL
supports, uniformly.

If a blueprint genuinely needs "Python 3 or later", the variable
should be an `enum` (`["3.10", "3.11", "3.12"]`) and the condition
should compare against one specific value. The enum's allowed set
is the "3 or later" contract, expressed where the resolver can
validate it.

### Function calls: `foo(...)`, `len(x)`, `x.lower()`

```yaml
# Rejected:
- condition: database.lower() == 'postgresql'
  files: [...]
```

**Why refused:** function calls are code execution. Even "safe"
functions like `lower()` establish a precedent — the next request is
`startswith()`, then `contains()`, then `matches()`, and eventually
the DSL has become a regex engine with a parser. The DSL's value
comes from its smallness; every callable added shrinks that value.
If a blueprint needs a case-folded comparison, declare the variable
with an `enum` whose values are already lowercased, or with a
`pattern` that accepts only the folded form.

### Attribute access: `x.y`

```yaml
# Rejected:
- condition: database.name == 'postgresql'
  files: [...]
```

**Why refused:** attribute access from template-like content is the
classic escape vector for sandboxing exploits. `x.__class__`,
`x.__init__.__globals__`, and their many variants are all
attribute-access paths. The DSL supports only flat variables — no
nested access — because the AST's simplicity is the guarantee that
no such path exists. A blueprint that needs a nested value should
declare a flat variable for it (with an `enum` or `pattern`
enforcing the shape), not reach into a structure.

### Escape sequences in strings

```yaml
# Rejected (the string is `\n`, not a newline):
- condition: mode == 'line1\nline2'
  files: [...]
```

**Why refused:** escapes reintroduce a mini-parsing language inside
string literals. Category values never need them; a string that
would need one is a design flaw in the blueprint's variable
definition. Declare a `pattern` if the value's shape matters, and
the pattern's regex will enforce it.

### Raw Python spellings: `True`, `False`, `None`, `elif`, ...

```yaml
# Rejected:
- condition: include_docker == True
  files: [...]
```

**Why refused:** Python spellings suggest Python semantics (case
sensitivity, truthiness, `None` comparisons), none of which the DSL
provides. `True` looks like Python's boolean but is not accepted;
`None` is not a literal the DSL supports. Accepting the *spelling*
without the *semantics* would be misleading. YAML/JSON spelling
(`true`/`false`) is what the DSL uses because that is what the
blueprint file itself uses.

---

## Errors

Every condition failure produces a `ConditionError` with a
`findings` list. Each finding carries:

| Field | Content |
|-------|---------|
| `condition` | A representation of the offending condition (verbatim for parser findings, canonical form for evaluator findings — see the field's docstring on `Finding`) |
| `message` | One-sentence description of what went wrong |
| `hint` | Actionable suggestion — what to *do* about it |
| `code` | Stable, machine-readable classifier |

### Error codes

| Code | Meaning | Example trigger |
|------|---------|-----------------|
| `condition.empty` | The condition string is missing or whitespace-only | `condition: ""` |
| `condition.syntax` | The string does not match any rule in the grammar | `database = 'x'`, `database == True`, `x == 'a' and y == 'b'` |
| `condition.invalid_variable` | The variable name violates the identifier grammar | `Database == 'x'` |
| `condition.undefined_variable` | The variable is well-formed but not present in the resolved variable set | `missing_thing == 'x'` when nothing named `missing_thing` was declared or supplied |
| `condition.internal` | A defect in the conditions module itself (never expected in normal operation) | — |

Each code is stable across releases. Downstream tooling (the CLI's
error renderer, the `--json` output, CI filters) may branch on it.

### The `--json` output

`forge create --json` (and the future `forge validate --json`)
serialises the aggregate error as:

```json
{
  "schema_version": "1.0.0",
  "source": "python.fastapi.api",
  "findings": [
    {
      "condition": "database == 'postgresql'",
      "message": "undefined variable: database",
      "hint": "Add 'database' to the blueprint's variables, or provide a value at create time.",
      "code": "condition.undefined_variable"
    }
  ]
}
```

The envelope's `schema_version` is the conditions DSL version (this
document's version), not the CLI's Python package version. The two
evolve independently.

---

## Adding a new form

Extending the DSL is a deliberate, multi-file change. Every step is
visible in code review, so expressiveness cannot grow by accident:

1. **Add a rule to `GRAMMAR`** in `src/forge/conditions/grammar.py`.
   Assign it a stable `name` and a `family` (the coercion strategy).

2. **Add parser support** in `src/forge/conditions/parser.py`. The
   parser is hand-written; a new form needs a new match-and-coerce
   branch.

3. **Add a positive test row** to `tests/unit/conditions/test_parser.py`'s
   `_VALID_CASES`. The `test_every_grammar_rule_has_a_positive_case`
   test fails otherwise.

4. **Add a negative test row** if the new form has plausible
   near-misses (a common typo, a wrong-cased variant, etc.) to the
   same file's `_INVALID_CASES`.

5. **Add a row to this document's "Supported forms" table.** The
   doc-sync test (`tests/unit/conditions/test_grammar_docs.py`)
   fails otherwise.

6. **If the new form introduces a new literal kind**, add a
   *"Literal kinds"* section here and a corresponding family entry
   to `TestFamilyCoverage._FAMILY_KEYWORDS` in the doc-sync test.

7. **If the new form introduces a new error code**, add it to the
   "Error codes" table above and to `Finding`'s docstring in
   `src/forge/conditions/errors.py`.

Steps 1 and 2 change behaviour; steps 3–7 change documentation and
tests. All seven live in different files, and a PR that changes
fewer than all seven is incomplete by construction. That is what
keeps the DSL, its tests, and its documentation in lockstep.

If the new form requires *any* of the "Explicitly *not* supported"
sections to be relaxed (adding `and`, adding arithmetic, etc.),
that is an ADR-level change, not a parser tweak. The rationale for
the refusal is documented above; changing it requires making an
equally explicit case for the change, which lives in a new ADR.

---

## Related documents

- [`CONDITION_GRAMMAR.md`](CONDITION_GRAMMAR.md) — this document.
- [`BLUEPRINT_SCHEMA.md`](SCHEMA_REFERENCE.md) — the schema of the
  blueprint itself, including the `conditional_files` field.
- [ADR-0016: Conditional File Inclusion Uses a Restricted DSL](../../docs/adr/0016-conditions-are-a-restricted-dsl.md)
  — the reasoning behind the DSL's design and non-goals.
- [`TEMPLATE_FILTERS.md`](TEMPLATE_FILTERS.md) — the allowlist of
  filters available in template bodies (a separate DSL with a
  separate rationale).
