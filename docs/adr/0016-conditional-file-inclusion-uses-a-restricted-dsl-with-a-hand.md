# 0016. Conditional File Inclusion Uses a Restricted DSL with a Hand-Written Parser, Not Python Evaluation

- **Status:** Accepted
- **Date:** 2026-11-10
- **Deciders:** thapelo
- **Consulted:** —
- **Informed:** —

## Context

Blueprints need to describe *optional* file groups — a Dockerfile that
only ships when the user wants Docker, a PostgreSQL adapter that only
ships when `database=postgresql`, CI configuration that only ships when
the project targets a hosted platform. Each group needs a **condition**:
a small predicate that decides whether the group's files are included
in the generated project.

The naive implementations of such conditions are all dangerous, and each
danger is concrete:

1. **Evaluate Python.** The obvious approach — store the condition as a
   string, run it through `eval()` or `exec()` — gives every blueprint
   author arbitrary code execution in the user's process. A blueprint is
   *untrusted input*; the whole point of a blueprint ecosystem is that a
   user can install one without auditing every line. `eval` collapses
   that guarantee.

2. **Evaluate Jinja.** The tempting middle-ground — "we already have a
   sandboxed Jinja environment, just run the condition through it" —
   conflates two problems (template rendering and conditional logic)
   and inherits the template engine's much larger attack surface
   (filters, globals, sandbox escape history) for a predicate that needs
   none of it. It also makes conditions *opaque* to tooling: a
   template-based condition cannot be statically inspected, indexed,
   migrated, or type-checked without running the engine.

3. **Use `ast.literal_eval`.** A common "safe `eval`" pattern —
   `ast.literal_eval` accepts a *superset* of any reasonable condition
   grammar (tuples, dicts, complex numbers). Rejecting the extra shapes
   requires a review of every Python version's `literal_eval` behaviour,
   forever, and any subtle change in that behaviour becomes a security
   event for Forge.

4. **Use a parser-generator (Lark, ANTLR, etc.).** Defensible, but
   overkill: the language has one operator pair and three literal
   kinds. A generated parser adds a build step, a dependency, a
   grammar-file format to learn, and a class of "the generated parser
   accepted something the hand-written one would have caught" bugs that
   are harder to debug than the hand-written version.

There is also a *semantic* question independent of the security one:
**what happens when a condition references a variable the user never
provided?** Two positions:

- **Falsy** (silently skip the group) — friendly for optional inputs,
  but a typo (`databse == 'postgresql'`) silently produces a project
  missing the PostgreSQL adapter the author intended, with no
  diagnostic. Users only discover the bug when their generated code
  fails to import a missing module.

- **Error** — loud and diagnosable, at the cost of failing the run when
  the user *meant* the group to be excluded by supplying nothing. This
  is the wrong framing: "the group should be excluded" is expressed by
  supplying a value that doesn't match, not by supplying nothing. A
  missing value is an *incomplete input*, not a "no" answer.

The constraints on a good solution follow from these: (a) no code
execution, (b) no runtime capability a user did not intend, (c) errors
at the right moment (parse vs. eval vs. reference), (d) statically
inspectable, (e) documented and versioned independently of the CLI, (f)
extensible without re-opening the security question, (g) deterministic.

## Decision

We will implement condition evaluation as a **restricted DSL** with the
following properties, enforced end-to-end in `forge.conditions`:

1. **Fixed grammar, hand-written parser.** The complete language is:

   ```
   var == 'string'       var != 'string'
   var == "string"       var != "string"
   var == true|false     var != true|false
   var == <integer>      var != <integer>
   ```

   No `and`, no `or`, no `not`, no arithmetic, no function calls, no
   attribute access, no parentheses, no comparison operators other than
   `==` and `!=`, no floats. **One operator per condition.** Every
   grammar rule lives in `forge/conditions/grammar.py` as an anchored
   regex with a stable `name` and a `family` tag (`string` | `bool` |
   `int`). The parser (`parser.py`) is a hand-written matcher — **no
   `eval`, `exec`, `ast.parse`, `ast.literal_eval`, or parser
   generator** touches blueprint content. Parsing is **total**: every
   input yields either an AST node or a `ParseError`; nothing raises.

2. **Typed AST, no source references.** The parser produces
   `Equals` and `NotEquals` nodes (frozen dataclasses in `ast.py`).
   The AST holds **no reference to the original string, no parser
   state, no file handle** — invariant 3 in `ast.py`. This makes nodes
   hashable, comparable, cacheable, serialisable, and impossible to
   use as a smuggling route for untrusted text.

3. **Type-aware comparison.** Two values are equal only if they are
   the *same runtime type* and equal. `"3" == 3`, `True == 1`, and
   `0 == false` are all **`False`**, because silently coercing across
   types hides "the user passed a string where the blueprint expects
   an integer" and "the user wrote a boolean literal against an
   integer variable". This is the single most important semantic rule
   in the DSL, and it is tested exhaustively in
   `tests/unit/conditions/test_evaluator.py::TestTypeAwareComparison`.

4. **Undefined references are errors, not falsy.** A condition that
   references a name not present in the resolved variable set produces
   a `condition.undefined_variable` finding. `condition.undefined_variable`
   is the *only* failure mode of "the variable has no value", and it is
   loudly distinguishable from "the variable has a value that doesn't
   match" (which is a clean, silent `False`).

5. **Structured, aggregated errors.** All failures — parse errors and
   evaluation errors alike — are `Finding` records with `condition`,
   `message`, `hint`, `code`. The evaluator aggregates them into a
   single `ConditionError` (per ADR-0010's "fail fast, fail complete"
   discipline) so a user fixes every problem in one pass.

6. **Documentation is enforced.** The DSL is documented in
   `docs/reference/CONDITION_GRAMMAR.md`, versioned independently
   (currently 1.0.0). A doc-sync test
   (`tests/unit/conditions/test_grammar_docs.py`) reads the markdown
   and the `GRAMMAR` data and asserts they agree: every grammar rule's
   example appears in the doc, every supported operator is documented,
   every literal family has a section, and every non-goal is
   enumerated. **The docs cannot rot without CI failing.**

7. **Extending the DSL is a multi-file change.** Adding an operator or
   literal kind requires touching at least seven files (grammar,
   parser, evaluator, positive test matrix, negative test matrix, this
   doc, and the fixture corpus). That friction is deliberate: every
   addition is a security review, not a convenience commit.

## Consequences

### Positive

- **No code execution, ever.** A blueprint condition is data. The
  sandboxed template renderer (ADR-0014) is not involved, and cannot
  be attacked through condition strings because conditions never reach
  it.
- **Small, auditable surface.** A reviewer can read `grammar.py`,
  `parser.py`, and `evaluator.py` in one sitting and be confident
  about *everything* the DSL can do. There is no hidden capability.
- **Static inspectability.** Because conditions parse into a typed AST
  with a closed set of node kinds, future tooling can walk them
  without running the engine: "what variables does this blueprint
  reference?", "does any condition compare a boolean to an integer?",
  "migrate all conditions from format X to format Y". None of this is
  possible with an `eval`-based DSL.
- **Deterministic errors.** Same inputs → same output, including error
  text. Snapshot tests, CI diffs, and bug reports all benefit.
- **Loud, actionable failures.** Undefined references produce
  targeted errors with hints ("add the variable, or supply a value at
  create time") rather than silently producing the wrong project.
- **Versioned independently.** The DSL's version is separate from the
  CLI's Python package version and the blueprint schema version; the
  three evolve on their own schedules. A DSL breaking change requires
  a version bump and a migration note, not a package release.
- **Doc-sync enforced by CI.** The grammar reference and the code
  cannot drift. A future contributor who adds an operator without
  documenting it will see CI fail with the specific rule that is
  undocumented.

### Negative

- **Narrower than Python users expect.** `python_major >= 3`,
  `x == 'a' or y == 'b'`, and `len(items) > 0` all fail. Users coming
  from templating engines (Jinja, Mustache) or from YAML's "arbitrary
  expressions" predecessors will ask for `and`/`or` in the first
  week. The answer is documented and firm: encode the composition in a
  variable's `enum`, or model it as two conditions on two groups.
- **No arithmetic, no ranges.** A condition cannot say "Python 3.10 or
  later". The workaround is an `enum` variable whose allowed values are
  exactly the versions the blueprint supports — which is *better*, in
  the sense that it makes the supported-version contract explicit and
  resolvable, but it is more verbose at the call site.
- **Hand-written parser means hand-written tests.** Every grammar rule
  requires its own positive test row, its own negative test rows, and
  its own doc entry. A parser generator would amortise this — but at
  the cost of the security properties listed above. The trade is the
  right one for a *predicate* language; it would be wrong for a
  *template* language.
- **DSL version bumps are breaking changes.** Removing support for a
  form (say, dropping `!=` for ints) breaks every blueprint that uses
  it. This is a *consequence of providing guarantees*: a language
  without a version contract cannot evolve without breaking silently.

### Follow-up actions

- [x] Ship `grammar.py`, `parser.py`, `ast.py`, `evaluator.py`,
      `errors.py` in `src/forge/conditions/`.
- [x] Ship `docs/reference/CONDITION_GRAMMAR.md` (version 1.0.0).
- [x] Ship doc-sync test (`tests/unit/conditions/test_grammar_docs.py`)
      that enforces the grammar-docs invariant.
- [x] Ship exhaustive unit tests
      (`test_parser.py`, `test_evaluator.py`, `test_undefined.py`,
      `test_boolean.py`).
- [x] Ship integration tests
      (`tests/integration/test_conditions_end_to_end.py`) that
      exercise the composed loader → resolver → evaluator → planner
      pipeline.
- [x] Ship fixture corpus (`tests/fixtures/conditions/`) with
      documentation of the corpus conventions and the explicit
      `integer-comparison-not-integration-testable` gap.
- [ ] **Future:** if the schema gains an `integer` `VariableType`,
      add an integration fixture that exercises integer comparison
      end-to-end. Currently not possible because `enum` resolves to
      string, `string` resolves to string, and `boolean` resolves to
      bool — none of which is a Python `int`.
- [ ] **Future:** if a static analysis tool for blueprints is
      built (e.g. `forge lint`), it must consume the AST directly,
      not re-parse the condition strings. The AST is the contract.
- [ ] **Future:** DSL version migrations follow
      `docs/adr/template.md` as new ADRs, not as edits to this one.
      This ADR describes the *original* design; a v2 DSL would be a
      new ADR-00XX superseding it.

## Alternatives Considered

### Alternative 1: Python `eval` / `exec`

Store the condition as a Python expression string and evaluate it with
`eval()` or `exec()` in the user's process.

**Rejected because:** a blueprint is untrusted input. `eval` gives
every blueprint author arbitrary code execution: filesystem access,
network access, environment variables, the whole process. There is no
`eval` sandbox that has held up under adversarial review at any scale.
Even if the *initial* use cases are benign ("just compare a string to a
literal"), the moment an advanced user asks "can I do X?" for any X, the
answer becomes "yes, in Python" — and the DSL's guarantees evaporate.

The historical analogue is YAML's `unsafe_load`, which caused
high-profile CVEs (CVE-2017-18342, CVE-2020-14343) precisely because
"just evaluate the YAML" was treated as a convenience. Forge does not
ship that footgun.

### Alternative 2: Jinja expressions (reusing the sandboxed renderer)

We already have a sandboxed Jinja environment for template bodies
(ADR-0014). Reuse it for conditions: store `{{ database == 'postgresql' }}`
and evaluate it through the same `ForgeSandboxedEnvironment`.

**Rejected because three separate problems compound:**

1. **Conflates two concerns.** Template rendering and conditional
   logic are different problems with different failure modes and
   different evolution paths. Coupling them means the template
   filter allowlist becomes a constraint on conditions, and vice
   versa — every change to one requires reviewing the other.

2. **Inherits the template engine's attack surface for a predicate.**
   Jinja's filters, globals, `{% raw %}` blocks, `{{ config }}`
   introspection, and its history of sandbox-escape advisories (e.g.
   CVE-2024-22195, CVE-2025-27516) all become relevant. A predicate
   language does not need `|tojson`, `{% for %}`, or `{% macro %}` —
   inheriting them makes every future Jinja CVE a Forge CVE by
   proxy.

3. **Opaque to tooling.** A Jinja condition can only be understood by
   running it through the engine. A condition grammar with a typed
   AST can be walked statically: "what variables does this blueprint
   reference?", "which conditions depend on which variables?", "does
   this condition ever evaluate to a type mismatch?". A Jinja
   condition defeats all of these without re-running the engine.

The template-body sandbox is still the right design (ADR-0014); it is
simply the wrong tool for a predicate.

### Alternative 3: `ast.literal_eval` as the "safe eval"

Parse the condition with `ast.parse`, run through `ast.literal_eval`,
and only accept nodes whose evaluation produces a `bool`.

**Rejected because:** `literal_eval` accepts a superset of any
condition grammar we would want — tuples, dicts, sets, complex numbers,
`Ellipsis`, nested structures. Rejecting the extra shapes requires
walking the resulting AST and rejecting every unexpected node type,
which is a *more complex* implementation than a hand-written parser
(for a language with two operators) and a *more fragile* one (its
behaviour depends on the Python version's `ast` module, which has
changed between 3.10 and 3.12). The hand-written parser is smaller,
simpler, more portable across Python versions, and impossible to
extend accidentally.

There is also an ambiguity that `literal_eval` cannot resolve:
`x == 'y'` in the AST is `ast.Compare`, not a literal, so
`literal_eval` **rejects it**. The conditions we want to support are
comparisons, not literals — a mismatch that would be discovered only
after implementing the whole thing.

### Alternative 4: A parser generator (Lark, ANTLR, PLY)

Define the grammar in a `.lark` / `.g` file, generate a parser at build
time, ship the generated code.

**Rejected because:** the language has **one operator pair** and
**three literal kinds**. That is small enough that a hand-written
recursive matcher is ~80 lines, auditable in one screen, and has no
build step. A parser generator would add:

- a dependency on the generator (build-time) and its runtime
  (run-time),
- a grammar file format to learn,
- a generated-code review burden (you cannot confidently review a
  2000-line generated parser without understanding the generator's
  internals),
- a class of "the generated parser accepts X" bugs that require
  understanding the generator to debug,
- a divergence risk between the grammar file (source of truth) and
  the checked-in generated parser (drift detector required, like
  ADR-0008's Pydantic model drift guard).

Each of these is a real cost paid for a language that does not need
them. If the DSL ever grows to the size of a real expression language
(hundreds of rules, precedence tables, operator overloading), the
generator becomes worth it. Today it is not.

### Alternative 5: YAML boolean field instead of a condition string

Instead of `condition: database == 'postgresql'`, store
`condition: {var: database, equals: postgresql}` — a structured object
instead of a string.

**Rejected because:** the DSL is intended to be *readable* to blueprint
authors, who write YAML by hand. `condition: database == 'postgresql'`
is legible; the structured form is not, and it makes the YAML noisier
for the same semantics. The DSL's parser is what makes the *string*
form safe — reading a string does not mean evaluating it. A future
`forge lint` could offer to canonicalise the string into the structured
form for tooling that prefers it, but the human-facing form should stay
a string.

### Alternative 6: Undefined variables evaluate to `False` (falsy)

Instead of erroring on undefined references, treat a missing variable
as `None`, which compares unequal to everything under strict rules —
so `missing == 'x'` is `False` and the group is excluded.

**Rejected because:** it converts *incomplete input* into a *silent
"no"*. A blueprint author who typo'd `databse == 'postgresql'` gets a
project missing the PostgreSQL adapter, with no diagnostic; the bug
surfaces days later as a missing import. The author's intended answer
was almost certainly "yes, include the adapter", not "no". Silent
falsehoods are the exact failure mode that ADR-0010, ADR-0011, and
ADR-0015 all exist to prevent across the pipeline.

Making the failure loud at the *right moment* (evaluation) and
distinguishable from a legitimate `False` (the "variable has a value
that doesn't match" case) is the whole reason `condition.undefined_variable`
is a separate code with its own hint.

The alternative is worth revisiting **only if** the DSL later adds a
distinct `optional(var, default)` operator — but that would be an
addition, not a change to the default semantics, and it would need its
own ADR.

## References

- **ADR-0006** — Dual-mode input handling (established the
  `Result`-based error aggregation pattern this DSL reuses).
- **ADR-0010** — Blueprint loading returns `Result[Blueprint,
  list[ValidationError]]`; the "fail fast, fail complete" aggregation
  discipline that `ConditionError` follows.
- **ADR-0011** — Variable resolution's type-aware coercion and
  explicit precedence; `condition.undefined_variable` only makes sense
  because variables have an established "present or absent" contract.
- **ADR-0014** — Sandboxed Jinja for template bodies; the reason we
  *don't* reuse it for conditions, and the reason we still have a
  sandbox for the layer below.
- **ADR-0015** — Planning as a pure function; the evaluator is the
  filter the planner consumes, and "safe by default" is why
  `conditional_groups=None` means "no conditional files".
- **`docs/reference/CONDITION_GRAMMAR.md`** — the user-facing
  reference for the DSL (version 1.0.0), enforced against the code by
  the doc-sync test.
- **CVE-2017-18342 / CVE-2020-14343** — YAML's `unsafe_load` history,
  the canonical cautionary tale for "just evaluate the data".
- **CVE-2024-22195 / CVE-2025-27516** — recent Jinja sandbox-escape
  advisories, the reason "reuse the sandbox" is not a free move.
- **`tests/unit/conditions/test_evaluator.py::TestTypeAwareComparison`** —
  the exhaustive cross-type matrix that pins the `True != 1` and
  `"3" != 3` rules.
- **`tests/integration/test_conditions_end_to_end.py`** — the composed
  pipeline proving the loader → resolver → evaluator → planner seam.
