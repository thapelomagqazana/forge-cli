# 0015. Planning Is a Pure, Total Function; Destination Paths Have One Resolver and One Safety Net

- **Status:** Accepted
- **Date:** 2026-11-10
- **Deciders:** thapelo
- **Consulted:** —
- **Informed:** —

## Context

The generation pipeline has four phases, in order: **load** (YAML →
typed blueprint), **resolve** (variables → concrete values), **plan**
(blueprint + values → intended writes), and **write** (intended writes
→ files on disk). Each phase has an established contract (ADR-0007 for
the schema, ADR-0010 for loading, ADR-0011 for resolution), but the
planning phase had no formal decision record. This ADR fills that gap.

Three forces made planning the highest-risk phase in the pipeline and
required a formal decision about its shape:

**1. Planning is where most security failures would occur if it were
wrong.** A loader that accepted a malformed YAML produces a broken
project; a resolver that accepted a bad value produces a compile error
in the generated code. Neither is silent. But a planner that accepted
a *traversal* destination would write a file outside the target
directory, and a planner that accepted a *collision* would silently
overwrite one file with another. Both are silent failures that a user
cannot detect from the output — they only surface later, as mysterious
missing files or as a file that appeared in the wrong place. That is
the failure mode planning must prevent, and preventing it is not a
"nice to have" the phase can defer to the writer.

**2. "Purity" was not optional; it was load-bearing for three downstream
consumers.** Three things depend on planning being a pure function of
its inputs:

- **Dry runs** (`forge create --dry-run`) must render exactly what the
  writer would write, without writing anything. If planning could
  perform I/O, the dry run would need a separate "no-op writer" that
  shadows every write. Shadow implementations drift.
- **Batch validation** must answer "would this blueprint plan cleanly?"
  without creating a single file. If planning could touch the
  filesystem, running it over a directory of blueprints would be
  destructive.
- **Golden tests** require the plan to be deterministic. If planning
  could read the current working directory, use the clock, or otherwise
  observe the environment, two runs of the same blueprint could produce
  different plans.

Purity is not an aesthetic preference here; it is the mechanism that
makes three separate guarantees true.

**3. Path safety had been duplicated across phases and was starting to
drift.** Before this decision, three phases each had their own
"is this path safe?" check:

- The `FileEntry` model in `forge.models.blueprint` (a Pydantic pattern
  on `source` and `destination`).
- The loader's `check_templates` (a `Path.resolve()` + `relative_to`
  containment check).
- The resolver's destination substitution (a raw string check that did
  not exist before this decision, but was about to be added for a
  separate reason).

The duplication was not theoretical. A review of the three checks
surfaced two divergences:

- The model's pattern was written for POSIX (`/`-separated only), so
  `C:\Users\x` and `\\Windows\x` passed the model but would be caught
  by `planning.paths.is_safe_relative` — different phases, different
  answers.
- The loader used `Path.resolve()` to catch symlink escapes; the model
  did not (and could not, since it had no filesystem context); the
  resolver was about to introduce a third variant. Three implementations
  of "safe path" meant three places where a future fix could land in
  one and not the other — the exact drift the loader's ADR-0010 was
  written to prevent for *error aggregation*, now happening for *path
  safety*.

The decision had to be: either accept the duplication (and accept that
each phase can drift from the others), or centralise the rule (and
make it the single source of truth). The pipeline's own history
(ADR-0010, ADR-0011) established that centralising a rule in one
module and reusing it everywhere was the pattern that prevented
drift. The same logic applies to path safety.

## Decision

We will model the planning phase as a **pure, total function**, and we
will centralise destination-path safety in **one resolver and one
safety net**.

### Part 1 — Planning is a pure, total function

The planner exposes exactly one entry point:

```python
class FileMapper:
    def map(
        self,
        blueprint: Blueprint,
        variables: Mapping[str, object],
    ) -> Result[list[PlannedFile], MappingError]:
        ...
```

The function is **pure**:

- It performs **no I/O** — no file reads, no file writes, no
  directory traversal.
- It performs **no filesystem reads** — it does not check whether a
  template exists (that is the loader's job) and does not read a
  template's contents (that is the writer's job).
- It observes **no environmental state** — no clock, no current working
  directory, no environment variables. Two calls with the same
  `(blueprint, variables)` produce the same result, on every machine,
  at every time, in every working directory.

The function is **total**:

- Every input that reaches `map` — valid or invalid — returns a
  `Result`. No exceptions escape for user-authored conditions.
- A failure returns `Err(MappingError(...))`, whose `findings` tuple
  is never empty. A "partial plan" is not a valid return value; the
  planner either succeeds completely or fails completely.
- All findings are aggregated in one `MappingError`. A blueprint with
  three collisions produces three findings, not a first-error-and-stop.
  This mirrors the loader's aggregation contract (ADR-0010) so that
  consumers learn one discipline for both phases.

The output is `list[PlannedFile]`, **sorted ascending by
`destination`**. Ordering is part of the purity contract: the plan's
order depends only on destinations, not on the order the blueprint
author happened to write them in. Two blueprints with the same set of
files in different order produce the same plan.

### Part 2 — Destination path safety: one resolver, one safety net

The safety rule for destination paths lives in **one place**:
`forge.planning.paths`, which exposes:

- `is_safe_relative(candidate: str) -> bool` — the boolean form, used
  by callers that only need a yes/no answer.
- `_safety_check(candidate: str) -> Finding | None` — the reporting
  form, used by callers that need the specific *reason* a path was
  rejected.

Both forms call the same function; there is no way for them to
disagree. The rules the function enforces are:

- **Not empty** — an empty destination means a variable expanded to
  nothing.
- **No NUL, LF, or CR** — these break filesystem I/O at a level no
  hint can recover from.
- **Not too long** (400 chars max) — beyond this is almost certainly
  a template bug.
- **Not absolute** — no leading `/`, `\`, or drive letter. The rule is
  *platform-independent*: `C:\Users\x` is rejected on POSIX **and**
  Windows, because a blueprint must behave identically on every host.
- **No empty segments** — a `//` almost always means a variable
  expanded to nothing.
- **No `..` or `.` segments** — traversal segments indicate a template
  mistake or an escape attempt.

The **substitution layer** (`planning.destinations.resolve`) is the
only place a *template* is rendered and the *result* is validated:

```python
def resolve(
    template: str,
    variables: Mapping[str, object],
) -> tuple[str, Finding | None]:
    rendered = TemplateRenderer().render_inline(template, variables)
    if not rendered.is_ok():
        return "", _render_error_to_finding(rendered.error)

    candidate = rendered.value.strip()
    finding = _safety_check(candidate)
    if finding is not None:
        return "", finding
    return candidate, None
```

The ordering — **render first, validate second** — is critical.
Validating the *template* would pass every blueprint whose escape
arrives via a variable (`out/{{ name }}/x.py` with `name = ".."`,
which is the easiest escape to construct accidentally). The safety
check must run on the rendered result, not the template source.

### Part 3 — The model is a checkpoint, not a duplicate

The `FileEntry` Pydantic model retains its own pattern on `source`
and `destination`. That is not duplication of the *rule* — it is a
**second checkpoint of the same rule**, at the earliest possible
phase. The model pattern catches:

- Raw `..` segments (the author typed them).
- Leading `/` or `\` (the author hard-coded an absolute path).
- Drive letters (the author wrote a Windows path by habit).

What it *cannot* catch, and what `destinations.resolve` exists to
catch, is traversal introduced by substitution — the model sees the
template `"out/{{ name }}/x.py"`, not the rendered `"out/../x.py"`.
The two checkpoints are distinct not because they enforce different
rules, but because they run against *different representations* of
the path (template vs. rendered) at *different phases* (parse vs.
plan). Removing either leaves a real gap:

- Removing the model pattern moves the error from parse time to
  plan time — the user sees "escape" instead of "invalid field".
- Removing the resolver check moves the error from plan time to
  *writer time*, when a file has already been half-written.

Both checkpoints are load-bearing, and the ADR's "single safety net"
refers to the *rule*, not to the number of enforcement points.

### Part 4 — The one safety net is called from three places

The rule in `planning.paths` is invoked from three places, each of
which is necessary:

1. **`FileEntry` (via the model pattern).** A separate *copy* of the
   rule, in regex form. The copy is not a duplicate — it is the
   enforcement point at the earliest possible phase, where a
   filesystem context is unavailable.

2. **`planning.destinations.resolve` (via `_safety_check`).** The
   rendered result of a destination template is validated against
   the rule.

3. **`loading.fs_checks._check_one` (via `is_safe_relative`).** The
   loader's source-path validation uses the same boolean form. This
   is the enforcement point that catches a *source* path the model
   could not see (because it was constructed by a future caller that
   bypassed the model, or because the model's regex was tightened in
   one place and loosened in another).

Each of these three callers sees a different *representation* of a
path, at a different *phase* of the pipeline. Removing any one leaves
a real gap. Removing all three (and relying solely on the writer to
reject traversal) would move the failure from "rejected before
writing" to "rejected after a partial write". That is not a
"theoretically equivalent" position; it is a strictly worse one.

## Consequences

### Positive

- **Dry runs are free.** Rendering the plan for `forge create --dry-run`
  is a pure formatting operation over `list[PlannedFile]`. No
  filesystem access is needed, and the output is guaranteed to match
  what the writer would do (because it describes the same object).

- **Batch validation is safe.** A tool that runs the planner over
  fifty blueprints creates zero files, because the planner never
  writes. That is what makes `forge validate` safe to run in a
  directory of cloned blueprints, and what a future "plan linter"
  would build on.

- **Determinism is a property, not a hope.** The plan is sorted by
  destination, and every phase that feeds the planner (loader,
  resolver) is also deterministic. The end-to-end test
  (`test_planning_end_to_end.py::TestHappyPath::test_plan_is_deterministic_across_runs`)
  proves the property holds through the composition, not just in
  isolation.

- **One rule, one fix.** A path-safety bug is fixed in
  `planning.paths._safety_check` and nowhere else. The three call
  sites (`destinations.resolve`, the loader's `_check_one`, and any
  future caller) all see the fix immediately. The pre-decision state
  had three implementations that could drift; the post-decision state
  has one implementation and three callers.

- **Aggregation is the default.** The planner collects every finding
  before returning. A blueprint with three problems can be fixed in
  one editing pass. This mirrors the loader (ADR-0010) and the
  resolver (ADR-0011), so users learn one discipline across the whole
  pipeline.

- **Errors are data.** `MappingError` and `Finding` are immutable
  dataclasses with `code`, `message`, `hint`, and (where applicable)
  `sources`. Any consumer — the CLI's `UserInputError` translation, a
  future `--json` renderer, a batch tool — can render findings without
  parsing prose.

### Negative

- **The writer receives no partial plans.** The "one problem fails the
  whole plan" rule is deliberately strict. A user with three
  independent collision problems cannot "just fix one for now and
  re-run" — they must fix all three. The trade-off is intentional:
  the alternative (a "mostly valid" plan) would require the writer to
  decide which findings to skip, which would silently produce a
  different project than the blueprint describes. That is worse than
  fixing three problems.

- **The model's regex pattern is a separate representation of the
  rule.** If `planning.paths` adds a new check (say, forbidding
  certain Unicode control characters), the model pattern does not
  automatically gain the check. The drift is caught by
  `test_path_traversal.py`, which exercises both the model pattern
  and the resolver check for the same set of path shapes. But the
  *test* catches the drift; it does not prevent it. The cost of a
  new rule is: update `planning.paths._safety_check`, update the
  model pattern, update the test — three edits, all of which are
  visible in review and all of which fail the test suite if skipped.

- **Conditional files are not yet planned.** The planner reads only
  `files[]`, not `conditional_files[]`. A blueprint that declares a
  conditional file will not have it planned today. This is not a
  limitation of the *design*; it is the current implementation state.
  The planner's module docstring documents the "conditional-files
  seam" and the test suite has a placeholder
  (`test_traversal_in_a_conditional_file_source_is_rejected`) that
  will be updated when the conditional-files phase lands.

- **Sorting by destination is a string sort, not a path-aware sort.**
  `out/a.py` sorts before `out/a/b.py`, even though a path-aware sort
  would put files before their subdirectory contents. The choice is
  deliberate (determinism is more valuable than aesthetic preference
  here), but it is a choice, and a reader who expects path-aware
  sorting may be surprised by the output order.

### Follow-up actions

- [ ] Land the writer phase (`forge.generation.scaffold_from_plan`
      and the staging/commit pipeline). The planner is complete; the
      writer is what turns the plan into files. The interface between
      them is `list[PlannedFile]` and nothing else.
- [ ] Land the conditional-files evaluation phase. When it lands, the
      planner will be called with the concatenation of `files[]` and
      the evaluated conditional files, and the collision detector and
      kind classifier will run over the combined set.
- [ ] Add a `--json` renderer for the plan, so `forge create --dry-run
      --json` produces a machine-readable plan. The renderer will
      consume `list[PlannedFile]` and the contract will be pinned by
      a contract test in the same style as
      `test_planned_file_contract.py`.
- [ ] Reconcile the `FileEntry` model pattern with
      `planning.paths._safety_check` when the schema's JSON Schema
      `pattern` is updated to match. This is the "two checkpoints of
      one rule" tension described in Part 3; the tension is managed,
      not eliminated, and the follow-up tracks the reconciliation.

## Alternatives Considered

### Alternative 1: Planning writes as it goes (a streaming planner)

*Description.* A planner that walks the blueprint's `files[]`, renders
each destination, resolves each source, and hands the result directly
to the writer one file at a time, never materialising a plan.

*Reason for rejection.* This breaks three properties simultaneously:

- **Dry runs become indistinguishable from real runs.** The streaming
  planner and the streaming writer would be called together; a "dry
  run" would need a fake writer, which would have its own logic, which
  would drift from the real writer. Dry-run output would slowly stop
  matching actual writes.

- **Collisions cannot be detected.** A streaming planner has no way
  to know whether a destination it has already written to is about to
  be written again. Collision detection requires materialising the
  full set of destinations, which is exactly what a plan is.

- **Batch validation becomes impossible.** Running the planner over
  fifty blueprints would create fifty projects on disk. The `forge
  validate` command — which exists to check blueprints *without*
  generating projects — could not exist.

A streaming design is faster in the "one small blueprint" case, but it
trades three guarantees for that speed. The guarantees are the point of
the pipeline.

### Alternative 2: The writer owns path safety (planner trusts inputs)

*Description.* Have the planner skip destination validation entirely,
produce the plan as-is (even with traversal and empty segments), and
let the writer reject traversal at write time. The writer already has
to check permissions, disk space, and filesystem-specific concerns;
"is this path safe?" could be a fourth check.

*Reason for rejection.* This moves path safety from a **pure decision**
to a **side-effecting decision**, with three concrete costs:

- **Dry runs would show a plan that cannot be written.** The user
  runs `--dry-run`, sees "will write `out/../../etc/evil`", and only
  discovers on the real run that it was rejected. That is a confusing
  user experience, and it is the exact class of "the tool showed me
  what it would do, then did something else" that destroys trust.

- **Collisions would still be the planner's problem.** A streaming
  writer that rejected traversal at write time would still need to
  know "has this destination been written before?" — which is exactly
  the plan the planner already produced. So path safety would not
  actually move to the writer; it would *split* between the planner
  (collision detection) and the writer (traversal), with no clear
  boundary.

- **The failure would be at the worst possible time.** A traversal
  destination rejected at plan time produces a `MappingError` with a
  path and a hint, *before* any file has been written. The same
  destination rejected at write time occurs after some files have
  already been written, and requires a rollback. Rollback is exactly
  the transaction machinery this decision is trying to keep simple.

The planner is the last phase where "reject the whole thing" is
cheap. Once the writer starts, "reject the whole thing" means "undo
what you did", which is a different and much more expensive problem.

### Alternative 3: A separate "safety" module called by each phase

*Description.* Keep the three implementations of path safety that
existed before this decision, but factor the shared rules into a
`safety.py` module that all three import. Each phase keeps its own
call site; the *logic* is centralised.

*Reason for rejection.* This is closer to what we did, but it stops
short of the decision. The problem with "each phase keeps its own
call site" is not that the *rules* drift (a shared module prevents
that); it is that the *representations* drift. The three phases see
three different things:

- The model sees the *raw* string (`"../escape"`).
- The loader sees the *resolved* path (`"/abs/path/to/escape"`,
  after `Path.resolve()`).
- The planner sees the *rendered* string (`"out/../escape"`, after
  substitution).

A `safety.py` with a single `is_safe(path)` function would be called
with three different shapes of input, and a rule that works for one
shape (say, "reject leading `/`") would not work for another (the
loader's input *always* starts with `/` because it is a resolved
absolute path — so the loader would need a different function, and
the drift returns).

What we did instead is make the *rule* (a string check on a relative
path) live in one module, and make each phase responsible for
**transforming its input into a relative path** before calling it.
The loader's containment check uses `Path.resolve()` +
`relative_to()` on top of the string check; the model pattern is a
regex copy of the string check; the planner calls the string check
on the rendered result. The three callers see three shapes, but they
all call one rule, and the transformation from their shape to "a
relative path string" is explicitly the caller's job.

The alternative ("`safety.py` with one `is_safe`") does not make the
transformation explicit; it hides it, and the next drift is between
"the loader calls `is_safe` on a path that starts with `/`" and "the
planner calls `is_safe` on a path that does not", with no shared
understanding of which is correct.

### Alternative 4: Enforce safety only in the schema (JSON Schema `pattern`)

*Description.* Add the traversal, absolute-path, and drive-letter
rejections to `blueprint.schema.json`'s `pattern` for `source` and
`destination`, and remove all Python-side checks. The schema is the
referee (ADR-0007); the model and planner trust it.

*Reason for rejection.* The schema `pattern` runs against the
**template**, not the **rendered result**. A blueprint whose
destination is `"out/{{ name }}/x.py"` has no `..` in it — the
schema pattern sees a string with `{{`, `}`, and `/`, all of which
are legal, and accepts it. Only after substitution does the traversal
appear. A schema-only rule would accept every substituted-traversal
blueprint, which is the *easiest* escape to construct accidentally
(a user running `--name ".."` for a mis-typed project name).

The schema is the referee for what a *blueprint* is. Path safety after
substitution is not a property of the blueprint; it is a property of
the *combination* of the blueprint and the variable values. No
schema-only rule can enforce it.

### Alternative 5: A "safety" filter the writer passes every path through

*Description.* Leave the planner and the writer as they are, and have
the writer wrap every filesystem call in a "safety filter": before
writing `path`, call `safety.is_safe(path)`; if the path is unsafe,
raise an error and roll back.

*Reason for rejection.* This is Alternative 2 with an extra step.
The three costs (dry-run inconsistency, split responsibility for
collisions, failure at the worst possible time) all apply. The
"safety filter" is an acknowledgement that "we did not plan for
safety"; the planner *is* that plan.

More importantly, a safety filter at the writer boundary would have to
re-derive the destination root, which the writer knows, and the plan
root, which the plan does not carry (the plan only knows relative
destinations). Re-deriving the root at the writer means the plan is
no longer self-contained — a plan that was accepted in one directory
would be rejected in another. Purity says the plan's correctness
depends only on its contents; a safety filter at write time says
correctness depends on the plan *and* the write-time directory. That
is the property this decision removes.

## References

- **ADR-0007** — the blueprint schema is the canonical contract.
  Establishes the "schema is the referee" principle that Alternative 4
  runs into.
- **ADR-0010** — the loader returns a `Result` and aggregates every
  error. Establishes the "one pass, all findings" discipline that
  `MappingError` mirrors.
- **ADR-0011** — variable resolution is a pure function over ordered
  sources with a single validation funnel. Establishes the
  `ResolvedValues` type that the planner consumes and the "purity"
  pattern the planner reuses.
- **`docs/reference/PLANNING.md`** — the user-facing reference for
  the planning phase. Covers destination templates, safety rules,
  collision detection, and the `PlannedFile` shape in more depth than
  this ADR.
- **`docs/reference/TEMPLATE_FILTERS.md`** — the filter allowlist
  that destination substitution shares with file rendering.
- **`docs/security/TEMPLATE_SANDBOX.md`** — the threat model for the
  renderer that destination substitution uses.
- **`src/forge/planning/mapper.py`** — the planner implementation.
  The module docstring expands on the "conditional-files seam" that
  this ADR mentions but does not resolve.
- **`src/forge/planning/paths.py`** — the single safety rule.
- **`tests/unit/planning/test_path_traversal.py`** — the tests that
  pin the traversal rejection across both checkpoints (the model
  pattern and the resolver check). This file is the enforcement
  mechanism for the "two checkpoints of one rule" guarantee described
  in Part 3.
