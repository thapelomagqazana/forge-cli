# 0018. Dry-Run Is a Pure Plan Builder; Writes Are Structurally Impossible, Not Just Tested-Away

- **Status:** Accepted
- **Date:** 2026-11-10
- **Deciders:** thapelo
- **Consulted:** — (CLI engineer, security lead — see ADRs 0012, 0017)
- **Informed:** — (all contributors to `forge.cli.commands.create`)

## Context

<!--
  Describe the forces at play: technical, political, social, and project-local.
  What is the problem we are trying to solve? What constraints do we have?
  What is the current state of the system, and why is it inadequate?

  This section should be understandable by a new contributor six months from
  now who has never seen the problem before.
-->

Project generation is a destructive operation. `forge create` walks a
staging-and-commit pipeline that ultimately renames rendered files
into a user-chosen destination. When the destination is empty, a bad
plan hurts nobody; when it is not, a bad plan can overwrite hours of
uncommitted work. The `--overwrite` flag exists to make the second
case safe (backups are taken before any write — see ADR-0017), but
the flag's existence only *consents* to risk; it does not *eliminate*
it. The only way to eliminate risk is to give the user a way to see
the plan **before** it runs.

`--dry-run` is that mechanism. It answers the question "what would
`forge create` do, if I let it?" — and it must answer **completely**
(every file, every collision, every skip, every warning) and
**without side effects** (nothing in the destination tree changes,
not even a temporary file). Two forces pull at that second
requirement:

- **The pipeline is designed to write.** Every stage of `create`
  builds toward a staging directory and a commit; the dry-run is a
  detour *around* that pipeline, not a variant *of* it. Any detour
  implemented by "run the pipeline but disable the write" is one
  refactor away from a bug that writes when it should not — a bug
  whose damage (silently overwriting user data during what the user
  believed to be a read-only preview) is severe enough to outweigh
  the cost of building the detour correctly.

- **The output must be machine-readable.** Beyond the human reader
  who runs `forge create --dry-run` to eyeball the plan, CI pipelines
  want to *gate* on the plan: "refuse to merge if this blueprint
  would overwrite a file in the target directory." That means the
  dry-run's output needs a stable, versioned schema — a schema whose
  shape is decoupled from the internal pipeline (which may be
  restructured between versions) and from the human renderer (whose
  layout is cosmetic and not a contract).

Before this decision, the state of the system was:

- `forge create --dry-run` rendered a plan to stdout, but the
  *content* of the plan was assembled ad-hoc inside the command
  module. There was no `GenerationPlan` type, no versioned schema,
  no golden fixture, and no distinction between "collisions found"
  and "the plan failed."
- The dry-run path and the create path duplicated their
  plan-building logic (both called `FileMapper().map(...)` and
  `detect_collisions(...)` in slightly different orders with
  slightly different parameters). The two were *intended* to
  produce the same plan; nothing enforced that.
- Exit codes were shared: a dry-run with collisions returned the
  same exit code (6) as a create that was refused for collision —
  forcing CI to inspect stdout to tell the two apart.

That state was inadequate for three reasons:

1. **Correctness cannot be tested against duplication.** If the
   dry-run and create paths each build their own plan, the ways
   they can drift are unbounded. The only durable way to keep them
   aligned is to *remove the duplication* — to have one plan
   builder that both paths call.

2. **A plan without a schema is a plan without a contract.** Once
   CI starts parsing `--dry-run --json` output, the shape of that
   output becomes a de facto API. If it is not documented and
   versioned, the first refactor that changes a field silently
   breaks every downstream consumer.

3. **The default failure mode must be "no write."** A dry-run
   whose write can only be prevented by a *test* is not safe; it
   is safe *as long as* the test suite covers every code path that
   might write. That is a bet on the completeness of testing, and
   for a read-only promise, betting is not good enough.

## Decision

<!--
  State the decision in full sentences, with active voice. "We will..." not
  "It was decided...". This is the section future readers will skim first.

  Be specific. "We will use Typer" is not enough. "We will use Typer 0.12+
  for the CLI layer because it is built on Click, supports type hints
  natively, and generates shell completion from type signatures" is.
-->

We will model `forge create --dry-run` as **a pure function that
builds a versioned `GenerationPlan` and renders it, with no
filesystem writes on any code path**, and we will distinguish its
"collisions found" outcome from the create path's "write refused"
outcome via a **distinct exit code**.

Specifically:

1. **The plan is a DTO.** `forge.dryrun.plan.GenerationPlan` is a
   frozen Pydantic v2 model with a `schema_version` field
   (currently `"1.0.0"`). Its shape is the contract; the DTO and
   its documentation (`docs/reference/DRY_RUN_PLAN.md`) evolve
   together, and any change to the DTO that is not purely additive
   requires a version bump and a migration note.

2. **The builder is a pure service.** `DryRunService.plan()` is a
   pure function of `(blueprint, variables, destination)`. It
   composes `ConditionalEvaluator`, `FileMapper`, and
   `detect_collisions` — the same three components the create path
   uses — and returns either a `GenerationPlan` or a
   `DryRunError`. It performs no writes, reads no template bodies,
   and opens no files for writing. It is unit-testable without a
   filesystem.

3. **The dry-run path never enters the writer.** The `create`
   command dispatches to `_run_dry_run` (a plan-builder wrapper)
   or `_generate` (the seven-phase create pipeline) on the value of
   `--dry-run`. `_run_dry_run` does not call `_generate`, does not
   construct a `StagingArea`, and does not invoke
   `backup_existing`. The writer is not merely *skipped* — it is
   *not on the code path*.

4. **Writes are structurally impossible.** `StagingArea.for_root(root,
   read_only=True)` produces a staging area that refuses every write
   and commit with `RuntimeError`, and does not create a directory
   on construction. Any future refactor that introduces a
   `StagingArea` inside a dry-run path — whether accidentally or
   "helpfully" — fails at the first attempted write, not after a
   partial commit. This is the structural enforcement that complements
   the fingerprint test (see "Consequences").

5. **Collisions exit 7, not 6.** `ExitCode.DRY_RUN_COLLISIONS = 7`
   is the dry-run's "collisions found" code. It is distinct from
   `ExitCode.COLLISION = 6` (the create path's "write refused"
   code) because the two outcomes have different remediations and
   different automation patterns: 6 means "retry with
   `--overwrite` or choose a different destination"; 7 means
   "inspect the plan on stdout, then decide." A CI script can gate
   on 7 without ever risking a write; a script that saw 6 could
   not.

6. **The plan is versioned, deterministic, and snapshot-tested.**
   `render_json(plan)` produces byte-identical output across runs
   (sorted keys, no whitespace variance, no ANSI, `ensure_ascii=False`
   so non-ASCII characters appear literally). The golden fixture
   (`tests/fixtures/dryrun/golden-plan.json`) is regenerated by
   `python -m tests.fixtures.dryrun._regenerate` and locked by a
   contract test — any change to the DTO's shape becomes a
   visible, reviewable diff.

## Consequences

<!--
  What becomes easier or more difficult because of this decision?
  Be honest about trade-offs. Every decision has consequences; a decision
  without consequences is not a decision, it is an observation.

  Split into positive and negative where helpful. List follow-up actions
  that the decision creates.
-->

### Positive

- **The "no writes during dry-run" promise is enforceable, not
  aspirational.** Two complementary mechanisms enforce it: the
  service's *structure* (no `StagingArea`, no `open(..., "w")`,
  no `Path.mkdir()` on any code path), and the fingerprint tests
  that compare a filesystem manifest before and after every dry-run
  run. The read-only `StagingArea` closes the third gap — a
  hypothetical future refactor that reintroduced a writer would
  fail loudly at the first attempted write, not silently corrupt
  the destination.

- **The dry-run and create paths cannot drift.** Both consume the
  same `FileMapper`, `ConditionalEvaluator`, and
  `detect_collisions` instances. A change to how files are mapped
  (say, adding a new file kind) shows up in both the plan and the
  write, or in neither. The fix is in one place, and the tests for
  one path are (in the parts that matter) tests for the other.

- **CI can gate on the plan without risk.** A pipeline that runs
  `forge create --dry-run --json` and branches on exit code (0 =
  safe, 7 = conflicts, others = real errors) never risks a write.
  The JSON payload — a documented, versioned contract — is the
  input to whatever decision the pipeline wants to make (refuse to
  merge, print a warning, require `--overwrite` in a commit
  message).

- **The DTO's shape is a first-class artefact.** The plan is not a
  renderer that happens to look like JSON; it is a typed,
  versioned model that a renderer projects. That means the plan
  can be *tested* as data (shape tests), rendered as *human text*
  (human renderer tests), and locked as *bytes* (golden fixture
  tests) — three independent test surfaces, each catching a
  different class of regression. A change to any one of them shows
  up in a diff that a reviewer can reason about.

- **The exit-code contract is more precise than before.** `6` and
  `7` are now two distinct codes with two distinct meanings, and
  the docstrings (`create`'s callback, `core.exceptions`,
  `DRY_RUN_PLAN.md`) all say the same thing about them. A CI
  author who reads one document gets an accurate picture that the
  other two corroborate.

### Negative

- **Two code paths exist where one might have sufficed.** A naive
  design could have made dry-run a flag on the writer — "run the
  same pipeline, but skip the commit." The plan-as-DTO design
  rejects that in favor of a separate builder, which means there
  *is* a second path through the file-mapping and collision
  logic. The mitigation is that both paths call the *same*
  underlying services (`FileMapper`, `detect_collisions`), so the
  duplication is only in the *orchestration* — a few dozen lines in
  `_run_dry_run` versus a few dozen in `_generate`. That is a
  smaller surface than the alternative (a single path with a
  conditional `if dry_run: return` that would put every write site
  one indentation level away from a bug).

- **The DTO is a public contract, with the associated overhead.**
  Adding a field to `files_to_create[]` requires: a field on the
  DTO, a golden-file regeneration, a shape-test update, and a note
  in `DRY_RUN_PLAN.md`. That is four files touched for what is, in
  code, a one-line change. The overhead is the price of treating
  the JSON output as a versioned API — and it is worth paying for
  a surface that automation depends on.

- **The exit codes are now harder to remember.** Six was one
  number; six and seven are two. A contributor who reads "exit
  codes are 0 through 5" from an old document and cannot find 6 or
  7 without updating their mental model. The mitigation is
  documentation: `core.exceptions` is the single source of truth,
  and every code has a comment explaining *why* it exists.

- **The golden fixture is a maintenance burden when the DTO
  changes.** Any change to the plan's shape makes the golden test
  fail. The fixture has to be regenerated, and the diff reviewed.
  That is the *point* — a change to a public contract should be
  visible — but it is friction that a project without a golden
  fixture would not pay.

### Follow-up actions

- [x] Implement `DryRunService.plan()` as a pure plan builder
  (`src/forge/dryrun/service.py`).
- [x] Define `GenerationPlan` as a frozen Pydantic v2 DTO
  (`src/forge/dryrun/plan.py`).
- [x] Implement deterministic JSON rendering with `sort_keys=True`,
  `ensure_ascii=False`, and compact separators
  (`src/forge/dryrun/renderers/json.py`).
- [x] Implement the human renderer with content assertions only —
  no layout contract (`src/forge/dryrun/renderers/human.py`).
- [x] Add `read_only=True` support to `StagingArea` and refuse every
  write/commit with `RuntimeError`
  (`src/forge/safety/staging.py`).
- [x] Add `ExitCode.DRY_RUN_COLLISIONS = 7` and wire it into
  `_run_dry_run` (`src/forge/core/exceptions.py`,
  `src/forge/cli/commands/create.py`).
- [x] Write the JSON contract documentation
  (`docs/reference/DRY_RUN_PLAN.md`).
- [x] Ship the golden fixture and its regeneration script
  (`tests/fixtures/dryrun/golden-plan.json`,
  `tests/fixtures/dryrun/_regenerate.py`).
- [x] Add unit, contract, and integration tests for the plan's
  shape, determinism, ANSI-freedom, Unicode fidelity, and
  filesystem-inertness.
- [ ] **When the resolver's `--var` merge lands** (see the note in
  `create.py`'s "The `--var` merge" section), audit whether the
  blueprint fixtures that were patched via `--var project=…` can be
  simplified.
- [ ] **When a second renderer format is added** (e.g. YAML,
  Markdown), update `DRY_RUN_PLAN.md`'s "The two output modes"
  section and add a renderer test file — following the same
  content-vs-layout split the human renderer already follows.
- [ ] **When a `--dry-run` failure mode is discovered** (a
  `DryRunError` variant that is not a planning failure), add a new
  `ExitCode` member — never reuse 1 or 2, which are already claimed
  by user-input and blueprint errors respectively.

## Alternatives Considered

<!--
  What other options did we consider? Why did we reject them?
  This is the most valuable section for future readers: it prevents them
  from re-litigating the same decision.

  If a rejected alternative has genuine merit, say so. A rejected
  alternative with caveats is more honest than a strawman.
-->

### Alternative 1: `--dry-run` as a flag on the writer

Turn `--dry-run` into a parameter passed all the way down into
`_generate`, and have each write site check it (`if not dry_run:
stage.write_...`).

**Why it was rejected:** this design is one `if` away from a bug
whose damage is total. Any write site that forgets the check — a
new file kind, a future backup step, a hook that runs
post-generation — writes when the user believes nothing was
written. The `if not dry_run:` idiom spreads the guard across the
codebase, so its correctness is proportional to how carefully every
contributor remembers it. A single writer function that is
*present* (but disabled) is fundamentally less safe than a code
path on which the writer is *absent*.

The `StagingArea(read_only=True)` mechanism captures the honest
kernel of this alternative — the writer stays in the code, but
refuses to write — while eliminating the spread-out-if problem.
Every write funnels through `StagingArea.write_bytes` or
`write_text` or `commit`, so *one* guard catches them all.

### Alternative 2: Share the plan-building logic with the create path, but keep `--dry-run` on the command

Refactor `_generate` to accept a `dry_run` parameter and, when it
is true, stop before the staging phase — reusing the same
`FileMapper().map(...)` call the writer would use.

**Why it was rejected:** this alternative is *closer* to the right
design than #1 — it recognizes that both paths should share
`FileMapper` (which the accepted design does) — but it conflates
two different lifecycles on one function. `_generate`'s body is a
seven-phase pipeline in which the first four phases are pure and
the last three are impure. Extracting a `_build_plan` helper and
calling it from both paths (the accepted design) makes that
boundary explicit; keeping it inside `_generate` and adding an
early return makes it implicit.

The consequence of the implicit approach is that a future
contributor who wants to add a dry-run-only feature (say, "show
me the plan without even building the manifest, just to see which
blueprint variables are needed") has no place to hang it that is
not inside `_generate`. The accepted design gives that feature a
home in `_run_dry_run`, and does not touch `_generate`.

### Alternative 3: Same exit code (6) for dry-run collisions and write collisions; distinguish by stdout

Use exit code 6 for both the dry-run's "collisions found" case
and the create's "write refused" case, and let the caller
distinguish by inspecting stdout (dry-run emits JSON; create
emits an error message).

**Why it was rejected:** this design forces every consumer to
inspect stdout to make a decision that the exit code should
already have made. Two pathologies follow:

- **A CI script that branches on 6 becomes unsafe.** Its natural
  implementation — "on 6, retry with `--overwrite`" — would
  *write files* on what the script believed was a dry-run
  inspection. The user sees "the check that should not have
  written anything just overwrote three files." The distinct code
  is what makes "retry with `--overwrite`" safe to write: it can
  only fire on 6, and 6 only means "a create was refused."

- **A CI script that cannot distinguish becomes unreliable.** Its
  natural alternative — "on 6, print stdout and exit 1" — would
  treat a legitimate "your plan has conflicts, here is the JSON"
  output identically to a "your write was refused" failure, when
  the two need different responses (inspect vs retry).

Exit codes are the cheapest interface a CLI has, and they should
carry all the information that a consumer needs to make its first
decision. Everything else — message text, stdout content, error
hints — is the *second* thing a consumer reads. `6` and `7`
encode the distinction at the first thing.

### Alternative 4: Emit the plan as TOML / YAML / NDJSON

Use a format other than JSON for the machine-readable output.

**Why it was rejected:** JSON is the only format that:

- is natively parseable in every language a CI script is written
  in (`jq`, `python -m json.tool`, `node -e`, `ruby -rjson`),
- has a widely-adopted, well-understood contract for versioning
  (the `$schema` convention, or in our case a `schema_version`
  field), and
- round-trips without ambiguity through subprocess stdout
  (no encoding surprises, no YAML whitespace-significance
  weirdness).

NDJSON is even worse for this use case: the plan is a single
object, not a stream, so a line-delimited format would just be a
stream of length 1 — a needless inconsistency with the
information-theoretic content.

### Alternative 5: A golden tarball instead of a golden JSON file

Capture the dry-run's output as a directory tree the user can
untar, rather than as a JSON document.

**Why it was rejected:** a tarball is not a contract. Its contents
are opaque to a diff — a CI pipeline cannot check "did the plan
change?" by looking at a tarball's hash, because the hash
includes metadata (mtime, uid/gid) that does not matter. JSON is
a tree of atomic values, so a JSON diff means "this field
changed," which is what a reviewer needs to know. The golden
fixture is JSON precisely so that the only changes CI catches are
ones a human can evaluate.

## References

<!--
  Links to related issues, PRs, external documentation, or prior art.
-->

- **ADR-0005** — *Stabilize exit codes as a public contract.*
  The overarching policy that exit codes are a first-class API;
  ADR-0018 extends that policy with exit code 7.
- **ADR-0012** — *CLI output is a versioned DTO consumed by
  pluggable renderers.* The general pattern that `GenerationPlan`
  follows; ADR-0018 instantiates it for the dry-run.
- **ADR-0013** — *Exit codes are a public contract; `forge
  validate` uses typed, documented codes.* The exit-code table that
  ADR-0018 extends.
- **ADR-0017** — *One filesystem safety layer, one policy, one
  staging discipline.* The safety and staging infrastructure that
  `_generate` uses and that `--dry-run` structurally avoids; the
  read-only staging mode added here extends it.
- **`docs/reference/DRY_RUN_PLAN.md`** — the versioned
  specification of the JSON output, including the exit-code table
  (`0`, `1`, `2`, `5`, `7`) and the four guarantees
  (deterministic, stable schema, ANSI-free, Unicode-faithful).
- **`src/forge/dryrun/`** — `service.py` (the pure plan builder),
  `plan.py` (the DTO), `renderers/` (the two renderers),
  `snapshot.py` (the filesystem fingerprint used in tests).
- **`src/forge/safety/staging.py`** — the `read_only=True`
  `StagingArea` that structurally refuses writes on the dry-run
  path.
- **`tests/integration/test_dryrun_command.py`** — the
  end-to-end integration tests that exercise the CLI's dry-run
  path as a subprocess, asserting exit codes, JSON shape, and
  filesystem inertness.
- **`tests/unit/dryrun/test_json_contract.py`** — the contract
  tests for the JSON output (determinism, schema, ANSI-freedom,
  Unicode fidelity, ordering).
- **`tests/fixtures/dryrun/golden-plan.json`** — the byte-stable
  fixture that locks the DTO's shape; regenerated by
  `python -m tests.fixtures.dryrun._regenerate`.
