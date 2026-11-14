# 0019. Staging Is Atomic and Invisible; the Rendering Engine Never Writes to the Destination

- **Status:** Accepted
- **Date:** 2026-11-14
- **Deciders:** thapelo
- **Consulted:** —
- **Informed:** —

## Context

`forge create` renders a blueprint's templates into a project on the
user's disk. The naive implementation — render each template directly
into `--output`, one `open().write()` at a time — has a failure mode
that has been observed in every scaffolding tool that has ever shipped:

> A template fails on file 47 of 50. The first 46 files are in the
> destination. File 47 is half-written (or absent, or has a truncated
> byte sequence). Files 48–50 do not exist.

The user's next action is not obvious. Re-running `forge create`
collides on the 46 existing files and, without `--overwrite`, refuses
the whole run. Running with `--overwrite` requires the user to have
already decided that the *partial* project is worth keeping. Neither is
what the user wants: they wanted the project, not the partial project,
and they want to try again without first cleaning up half a run.

The problem is not unique to `forge`. It is the reason package managers
install to a temp directory and rename; the reason `pip` unpacks into a
staging tree; the reason `apt` uses `/var/cache/apt/archives` before
committing; the reason `git` writes to a temp index and renames. The
technique is old and well-understood. The question this ADR answers is
not *whether* to use it, but *how to shape it* for `forge`'s specific
constraints:

1. **The commit must be atomic per file.** A partially-committed
   project — some files at the destination, some in staging — is a
   state the user has to reason about. A per-file `os.replace` means
   every file that *is* at the destination is *complete*; a partially-
   committed project is a valid project with fewer files, not a project
   with corrupted files.

2. **The commit must be atomic across the whole set at the directory
   level, or explicitly not.** There are two coherent choices: full
   transactionality (roll back all commits on any failure) or per-file
   atomicity with no cross-file guarantee. Full transactionality on
   POSIX requires either a copy-on-write filesystem, or a rename of a
   pre-assembled tree over the destination (which requires the
   destination to be empty or to be the pre-assembled tree itself —
   both conditions that make the operation less useful than it sounds).
   Per-file atomicity is achievable on every filesystem `forge` targets
   and is the shape every real installer uses. The trade-off is: *a
   commit that fails partway leaves the successfully-committed files in
   place*, and the caller must decide whether to retry, roll back, or
   accept.

3. **A failed run must not touch the destination.** This is the *strong*
   form: not just "no partial files in the destination," but "no
   destination at all if the run failed." The reason is subtle. If
   staging creates the destination directory (say, to hold a staging
   subdirectory inside it), a failed run leaves an empty `demo/`
   directory behind. The user sees an empty directory named after the
   project they tried to create. That is a mutation, even if it is an
   empty one. It tells the user *something happened*, but it does not
   tell them *what* — and the next `forge create demo` will collide on
   the empty directory unless the user remembers to `rmdir demo` first.
   The strong form of the guarantee — "no destination exists if the run
   failed" — is achievable, and it is what staging must provide.

4. **The render pipeline must not have a way to write into the
   destination.** This is a *structural* property, not a policy. The
   safest way to make "a failed render never leaves a partial file in
   the destination" true is to make "a successful render never writes
   to the destination" true — by construction, not by review. A
   developer adding a new call site should discover the staging
   discipline through the type they are handed (`StagingArea`), not by
   reading a docstring that says "please write into staging instead of
   the destination." The former survives refactors; the latter does
   not.

5. **The staging tree must be invisible.** During a run — which can
   take several seconds on a large blueprint — the user must not see a
   `forge-stage-XXXXXX/` directory in their project or alongside it in
   their `ls` listing. A visible staging tree is worse than a slow
   command: it tells the user *something is happening* without telling
   them *whether it will succeed*, and the mental model that implies
   ("partial output you can interrupt and restart") is exactly the
   mental model staging exists to prevent. Invisibility is achieved by
   the leading dot (hidden from plain `ls`) and by placement (outside
   the destination, so a user browsing their project sees nothing).

These five constraints, taken together, force a specific shape:

- Staging lives on the **same filesystem** as the destination (constraint 1).
- The staging tree is **not inside** the destination (constraint 3).
- The staging tree is **not inside** the destination's *parent* either,
  if the parent is what the user is `ls`-ing (constraint 5).
- Writes to staging go through a **typed API**, not a raw `Path`
  (constraint 4).
- The commit phase is the **only** phase that touches the destination,
  and it is **per-file atomic** rather than cross-file transactional
  (constraint 2).

The current system has none of these. `forge create` calls
`TemplateRenderer.render()` on each planned file, writes the result to
the destination via a bare `Path.write_text()`, and has no recovery
path if a render fails on file 47. The rendering engine
(`forge.rendering.TemplateRenderer`) is a pure function returning a
string, so it is not the immediate offender — but the code that *calls*
it (the future `scaffold_project` function, not yet written when this
ADR was drafted) has no mechanism to write anywhere other than the
destination, because there is no staging area to hand it.

This ADR is written *ahead* of `scaffold_project`'s implementation so
that the function's contract, from its first line, is shaped by the
staging discipline rather than having the discipline bolted on
afterwards.

## Decision

We will introduce a **staging phase** between rendering and commit, with
the following contract.

### The staging tree

- Every file `forge create` produces is written into a hidden staging
  directory, not into the destination.
- The staging directory's name begins with `.forge-stage-`, so it is
  hidden from plain `ls`.
- The staging directory is a **sibling** of the destination root:
  if the destination is `<output>/<project_name>`, staging is
  `<output>/.forge-stage-<random>`. Both are children of `<output>`, so
  they are on the **same filesystem** by construction, and
  `os.replace(staging/file, destination/file)` is a **true atomic
  rename** — not a copy-and-delete that can be interrupted.
- The staging engine **never creates the destination directory**. The
  destination comes into existence exactly when the commit phase's
  first `os.replace` calls `mkdir -p` on the target's parent. If a
  render fails before commit, the destination was never created;
  if the commit fails on file 1, the destination contains exactly the
  files whose `os.replace` succeeded — usually zero.

### The write API

- The staging area is a context manager:
  `with StagingArea.for_root(root) as stage:`. The `root` is the
  destination root, used to anchor the staging tree on the same
  filesystem; the staging tree itself is `root.parent/.forge-stage-XXX`.
- Files are written via `stage.write_bytes(relpath, data)` and
  `stage.write_text(relpath, text)`. There is **no other way** to
  produce a file in the destination during the staging phase — the
  staging area is the sole write handle, and its API cannot address a
  path outside itself.
- `stage.commit()` moves every staged file to its final destination via
  `os.replace`, in the order they were staged. It returns the list of
  committed paths. It is called by the commit phase only; a
  render-phase caller does not call it.
- The staging tree is destroyed on every exit path of the context
  manager — success, exception, or explicit early return. A caller
  cannot leak the tree by forgetting to clean up; it can only leak it
  by explicitly *detaching* it (`stage.detach()`), which transfers
  ownership to the caller and is the mechanism the staging engine uses
  to hand a populated tree to the commit phase.

### The read-only variant

- `StagingArea.for_root(root, read_only=True)` produces a staging area
  that refuses every write and commit, and whose construction does not
  create a directory on disk. It exists for the dry-run path, which
  must be unable to write by *construction* (a future contributor
  cannot accidentally introduce a write inside the dry-run code path
  without tripping a loud, deterministic error).
- Read-only mode is the structural half of the "no filesystem mutations
  during dry-run" guarantee. A fingerprint-based test (compare the
  destination tree before and after) is necessary but not sufficient —
  a temporary file that is created and removed within the same
  dry-run would slip past a fingerprint taken after. The read-only
  staging area catches that class.

### The rendering engine

- `forge.rendering.TemplateRenderer` is a **pure function**: it takes a
  template path and a variable mapping, returns a `Result[str,
  TemplateRenderError]`. It does not know about staging, does not know
  about the destination, and has no filesystem-writing API. This
  separation is enforced by the module's public surface — the renderer
  has no path parameter, no output stream, and no callback.
- The code that *calls* the renderer — the not-yet-written
  `scaffold_project` — is where the staging discipline applies. Its
  contract is: for each planned file, call `renderer.render()`; on
  success, write the result into `stage`; on failure, abort and let
  the staging context manager clean up. It never writes to the
  destination directly; it never holds a `Path` that points at the
  destination.
- The staging engine (`forge.staging.StagingEngine`) is the module
  that owns this loop. Its public method
  `stage(plan, variables, blueprint_root, destination_root) -> Result[
  StagingResult, RenderingError]` composes rendering, writing, and
  progress reporting. Callers of the engine do not see the renderer,
  the staging area, or the loop; they see a `Result` and, on success, a
  `StagingResult` that owns the populated tree.

### The commit phase

- `scaffold_project` calls `StagingEngine.stage(...)`. On `Err`, it
  propagates the error (the destination was never touched). On `Ok`, it
  iterates `result.manifest` — the sorted list of staged destinations —
  and `os.replace`s each file into the destination. It calls
  `result.destroy()` in a `finally` block, whether the commit succeeds
  or fails.
- The commit phase creates the destination directory as needed, via
  `os.replace`'s own `mkdir -p` on the target's parent. The destination
  comes into existence exactly when the first file is committed into
  it.
- Backup of existing files, when `--overwrite` is set, happens in a
  distinct phase *before* staging. The staging engine does not create
  backups; it only moves files. Separation of concerns: the backup
  phase owns data preservation; the staging engine owns atomicity;
  the commit phase owns reachability.

### The `--dry-run` variant

- `forge create --dry-run` does not invoke the staging engine at all.
  It uses `DryRunService`, which composes the planner, the condition
  evaluator, and the collision detector into a `GenerationPlan` DTO
  without writing anything. The read-only staging mode exists as a
  *defence in depth* for the dry-run path: if a future refactor of
  `DryRunService` somehow reaches into the staging engine, the staging
  area's read-only mode refuses the write before it can touch the
  filesystem.
- The `GenerationPlan` is a separate DTO from `StagingResult`, because
  it is a different thing: a `GenerationPlan` describes what *would*
  happen; a `StagingResult` describes what *did*. They share the
  planner, the condition evaluator, and the collision detector, but not
  the staging engine's write path.

### Composition guarantees

The following guarantees are structural — enforced by the module
boundaries, not by individual functions' care:

1. **A failed run leaves the destination untouched.** The engine
   cannot write to the destination; only the commit phase can, and
   the commit phase only runs on `Ok(StagingResult)`.
2. **A failed commit leaves no partial file.** Each file is committed
   with `os.replace` (atomic on the same filesystem). A committed file
   is complete.
3. **The destination does not exist during staging.** The engine never
   creates it; the commit phase creates it via `mkdir -p` on the first
   file's parent.
4. **The staging tree is destroyed on failure and on success.** The
   context manager's `finally` handles both; `StagingResult.destroy()`
   handles the success-then-commit-fails case.
5. **The staging tree is invisible.** Its name begins with `.` and it
   lives outside the destination. A user `ls`-ing their project during
   a run sees nothing unusual; a user `ls`-ing the parent sees a hidden
   entry they have to know to look for.
6. **Rendering is deterministic per file.** Same template + same
   variables → byte-identical staged content. The renderer is a pure
   function; the staging engine does not introduce nondeterminism
   (it does not sort, transform, or annotate the rendered bytes).

### Exit-code contract

The staging phase's failures map to `ExitCode.RENDERING_ERROR` (8). This
is deliberately distinct from `BLUEPRINT_ERROR` (2, the blueprint is
malformed) and from `INTERNAL_ERROR` (5, the tool is broken):

- A malformed blueprint fails before staging even starts.
- A template that fails to render fails during staging, but the failure
  is attributable to the *template* (a syntax error, an undefined
  variable, a sandbox denial) — the user fixes it by editing the
  template or supplying the missing variable. That is not a tool bug.
- A staging-engine bug (a `staging.*` finding) *is* a tool bug and
  should surface as `INTERNAL_ERROR` in a future revision; today, the
  `staging.*` codes exist and are aggregated into
  `RenderingError`, so they currently render at exit 8 for uniformity.
  Distinguishing them is a future follow-up (see "Follow-up actions").

## Consequences

### Positive

- **The partial-output failure mode is impossible.** A failed `forge
  create` leaves the destination in exactly the state it was in before
  the command was run (or, on commit failure, in a state where every
  file that exists is complete). This is the primary purpose of the
  decision and the failure mode the user cares about most.

- **The destination is never created unless the run will produce at
  least one file.** A failed run does not leave an empty `demo/`
  directory behind for the user to `rmdir` before retrying. This is a
  direct consequence of the sibling placement — the engine cannot
  create the destination because it has no write handle to it — and it
  is the property that distinguishes "staging" from "temp directory
  that happens to be near the destination."

- **The staging discipline is enforced by the type system, not by
  review.** `render_or_copy` accepts a `write_bytes` callback that is a
  bound method of a `StagingArea`; a caller that wanted to bypass
  staging would have to construct its own `StagingArea` and pass its
  `write_bytes`, which is a code-reviewable act (the constructor is the
  seam) rather than a silent one (a bare `Path.write_text()` in the
  render loop). Reviewers can grep for `Path.write_text` in
  `staging/`, `rendering/`, and `cli/commands/create.py` and find zero
  occurrences outside the staging area's own `_prepare`; the discipline
  is visible in the diff.

- **The dry-run path cannot write by construction.** The read-only
  staging mode exists to make "no filesystem mutations during dry-run"
  a structural property rather than a behavioural test. A future
  contributor who adds a write to the dry-run path trips a
  `RuntimeError` at the write site, with a message that names the
  staging area and the read-only contract.

- **The commit phase is easy to test and easy to review.** It is a
  ~15-line loop that iterates `result.manifest`, `os.replace`s each
  entry, and calls `result.destroy()` in a `finally`. There is no
  cleverness, no fallback logic, no recovery path. Recovery is a
  *policy* the caller chooses (via `--overwrite` and its backups, or
  via a manual `rm -rf` and a re-run), not something the commit phase
  attempts on the user's behalf.

- **Progress reporting is decoupled from rendering.** The engine
  emits typed `ProgressEvent`s through a `ProgressSink`; the CLI
  renders them to stderr; tests capture them into a list. The engine
  never prints. This means the engine's behaviour is identical under a
  TTY, a pipe, a CI log capture, and a test's `ListSink` — the only
  thing that varies is *who receives the events*, not *what the engine
  does*.

### Negative

- **Two-phase generation doubles the peak disk usage.** During a run,
  both the staging tree and (on commit) the destination exist
  simultaneously. For a 50-file project with a 5 MB destination, this
  is 5 MB extra. For a project with a large binary asset (say, a
  100 MB pre-built Docker base layer), it is 100 MB extra. The
  alternative — writing directly to the destination and rolling back
  on failure — requires copying the *original* destination files to a
  backup, which is at least as expensive and often more (a partial
  overwrite of an existing project needs backups of everything it will
  replace, whether or not the run succeeds). Staging trades peak disk
  for failure safety; that is the right trade for a scaffolding tool,
  and it is documented so a user with severe disk constraints knows
  what to expect.

- **The staging tree must be on the same filesystem as the destination,
  which constrains the *placement*.** The engine stages in
  `destination.parent`; a destination whose parent is on a different
  filesystem from where staging would prefer to live (say, `~/` for
  the tree, `/tmp` for the destination) cannot be staged on the
  user's preferred device. In practice, `destination.parent` is
  wherever the user pointed `--output`, so the same-filesystem
  constraint is satisfied by construction — but a caller that wanted
  to stage in `/tmp` for speed would have to give up the atomicity
  guarantee, which this ADR does not allow. The trade is: staging
  location is derived, not chosen.

- **`StagingResult` is a resource the caller must destroy.** A caller
  that forgets to call `result.destroy()` leaks a `*.forge-stage-*-ready`
  directory. The method is idempotent and best-effort, so a `finally`
  block is safe; but the obligation is real, and it is why the CLI's
  `_commit_staged` uses `try/finally` rather than a plain `try/except`.
  A future revision could make `StagingResult` a context manager whose
  `__exit__` destroys on unrecoverable failure, but the current design
  deliberately keeps the resource's lifetime the caller's concern —
  because the *success* path must *not* auto-destroy (the commit phase
  reads from the tree after `stage()` returns).

- **Commit is not transactional across files.** A commit that fails on
  file 5 leaves files 1–4 committed. The trade is documented in
  "Context" (constraint 2) and reflected in the code: the commit
  phase's `finally` destroys the staging tree, and the user retries
  with `--overwrite` (which backs up what exists) or manually cleans
  up. The alternative — a full rollback — would need to preserve the
  *original* destination files across the commit, which is a backup
  operation, not an atomicity primitive. The `--overwrite` path already
  provides the backup; the current design's choice is to *not* silently
  do it on the user's behalf.

- **The exit code for engine bugs is not yet distinct from user-facing
  rendering errors.** A `staging.*` finding (an engine bug) currently
  surfaces at exit 8 (`RENDERING_ERROR`), same as a `render.*` finding
  (a user-fixable template error). The remedy is a future split: a
  `STAGING_ERROR` exit code for engine bugs, distinguishing "the tool
  is broken" from "the template is broken." Until that split lands,
  automation that branches on the code cannot distinguish the two —
  and a user who reports an engine bug with exit 8 (thinking it is
  their template) wastes their time. This is a known gap, tracked in
  "Follow-up actions."

### Follow-up actions

- [x] Implement `StagingArea` (`forge/safety/staging.py`) with
      `for_root`, `write_bytes`, `write_text`, `commit`, `detach`,
      and read-only mode.
- [x] Implement `StagingEngine` (`forge/staging/engine.py`) with
      `stage()`, aggregating findings into `RenderingError`, emitting
      `ProgressEvent`s, and transferring ownership via
      `StagingResult`.
- [x] Implement `render_or_copy` (`forge/staging/_renderer_loop.py`)
      as the per-file transition function, with a `write_bytes` callback
      that keeps the write permission scoped to the staging area.
- [x] Implement `StagingResult` (`forge/staging/result.py`) with
      `destroy()` (idempotent), `manifest`, and `destination_root`.
- [x] Wire staging into `forge create` (`cli/commands/create.py`):
      the staging phase runs between planning and commit; the commit
      phase is `_commit_staged`, iterating `result.manifest`.
- [x] Add `ExitCode.RENDERING_ERROR = 8` for staging-phase failures,
      distinct from `BLUEPRINT_ERROR` (2) and `INTERNAL_ERROR` (5).
- [x] Publish `docs/reference/STAGING.md` as the reference for the
      staging phase's public API, guarantees, non-guarantees, and error
      codes.
- [ ] Split `ExitCode.STAGING_ERROR` (proposed: 9) from
      `RENDERING_ERROR` (8), so a `staging.*` finding (engine bug)
      surfaces distinctly from a `render.*` finding (user-fixable
      template error). Requires a matching bridge in
      `forge.core.exceptions` and a `_raise_safety`-style type
      selection in `_as_rendering_error`. Tests to add:
      `test_engine_render_failure.py::test_engine_bug_surfaces_at_staging_exit_code`
      and a contract test in `tests/contract/test_exit_codes.py`.
- [ ] Add a `--no-stage` escape hatch for CI workflows where disk
      space is severely constrained. The flag would render directly to
      the destination, defeating guarantee (1) but preserving
      guarantees (2) and (4) at the file level. Not proposed now
      because no user has asked, and the flag's existence creates a
      second code path that has to maintain the same rendering
      semantics — a maintenance burden that a hypothetical user has to
      justify by requesting it.
- [ ] Consider a `--keep-staging` debug flag that leaves the staging
      tree in place on commit failure, for post-mortem inspection.
      Currently, the `try/finally` in `_commit_staged` always destroys
      the tree, which is correct for production but unhelpful when a
      user reports a commit-time failure and wants to see what was
      staged. The flag would need to document the debris it leaves and
      the manual cleanup.

## Alternatives Considered

### Alternative 1: Write directly into the destination with rollback-on-failure

Each file is rendered and written into the destination immediately. On
any failure, the rollback path walks the destination, removes files the
run created, and restores files the run overwrote from a backup taken
before the run.

**Why rejected:**

- **The rollback is more dangerous than the failure it recovers from.**
  Removing files the run created requires knowing *which* files those
  are. In a directory the user might also have been editing, that is a
  conservative-but-fragile operation: the run's record of "created
  files" must be precise, or the rollback deletes a file the user
  edited concurrently. The staging discipline avoids this entirely by
  never writing to the destination until commit, and never committing
  anything the plan does not name.

- **The rollback itself can fail.** Restoring a backup from a
  `.forge-backup-XXXX` file to the destination, mid-rollback, can hit a
  `PermissionError`, a disk-full error, or a race with another process.
  The staging discipline makes this failure mode impossible: the
  destination is only ever touched by `os.replace`, and a failed
  `os.replace` is a single failed file, not a half-rolled-back tree.

- **The user's backup is not the user's backup.** A rollback that
  restores from an internal backup means the user's *current* file
  content is overwritten by the run's *prior* content, silently. The
  current design's `--overwrite` path exposes the same mechanics
  through an explicit flag (`--overwrite` creates `.bak.<timestamp>`
  files the user can see and inspect), so the user consents to the
  preservation semantics. Silent rollback does not offer that consent
  point.

### Alternative 2: Stage in the system temp directory (`tempfile.gettempdir()`)

The staging tree is created under `/tmp` (or the platform equivalent)
instead of under the destination's parent.

**Why rejected:**

- **Cross-filesystem commits are not atomic.** `os.replace(src, dst)`
  raises `EXDEV` when `src` and `dst` are on different filesystems. On
  Linux, `/tmp` is frequently a tmpfs, which is a different filesystem
  from the user's home directory or working directory. A commit that
  hits `EXDEV` has two choices: fail (defeating the purpose of
  staging), or fall back to `shutil.move` (which is a copy-and-delete,
  not atomic, and which would leave a partial file in the destination
  if the copy is interrupted). Neither is acceptable. Staging in the
  destination's parent is *by construction* the same filesystem, which
  is the only way to guarantee atomic `os.replace` without a
  filesystem-aware fallback path.

- **`/tmp` can be cleared mid-run.** Some systems run `tmpreaper` or
  equivalent that removes files under `/tmp` older than a threshold.
  For a staging phase that takes seconds, this is unlikely — but not
  impossible for a slow blueprint or a busy CI runner. Staging under
  the destination's parent is under the user's control, and the
  cleanup policy (if any) is the user's, not the OS's.

- **`/tmp` is not the user's namespace.** A staging tree under `/tmp`
  is potentially readable by other users on a multi-user system,
  especially if `/tmp` is world-readable (it usually is). Rendered
  output can contain sensitive content (API keys in generated
  configs, license text, user data being interpolated). Staging under
  the destination's parent inherits the destination's permissions,
  which is the appropriate scope.

### Alternative 3: In-memory staging (render everything, then commit from memory)

The render phase produces a `dict[destination, bytes]` in memory; the
commit phase writes each entry to the destination with `os.replace` on
a temp file.

**Why rejected:**

- **Memory scales with the blueprint's output, not with its file count.**
  A blueprint that includes a 100 MB pre-built asset would need 100 MB
  of RAM during the render phase. The staging tree on disk scales with
  the same 100 MB, but on disk, where the user has more headroom and
  the OS can page it. For a scaffolding tool that might run on a small
  CI runner or a developer's laptop, this is a real constraint.

- **The commit is not atomic per file, only per entry.** Rendering
  into memory and then writing each entry with `os.replace` to a
  destination file preserves atomicity at the *file* level, but the
  "no partial output on failure" guarantee is weaker: if the commit's
  third `os.replace` fails, files 1–2 are committed (from memory) and
  the remaining files are still in memory but lost when the process
  exits. The staging-tree design lets the commit phase fail on file 5,
  leave files 1–4 committed, and let the caller's `finally` destroy the
  tree — same behavior at the destination, but the caller *could*, if
  it wanted, resume the commit from the staging tree. In-memory staging
  cannot resume because the process is gone.

- **The renderer's output is not always small.** A blueprint can
  legitimately render a 10 MB Markdown file. The staging tree writes
  it to disk once; in-memory staging writes it to RAM, then to disk
  on commit — two writes for one file's worth of data. For a
  scaffolding tool that runs infrequently, this is not a performance
  concern, but it is a memory concern for constrained environments.

### Alternative 4: Copy-on-write filesystem for atomic tree swap

Assemble the project in a temporary *tree* on the same filesystem,
then atomically replace the destination root with the temporary tree
via a single `os.rename(temp_tree, destination_root)`.

**Why rejected:**

- **This works only for empty destinations.** `os.rename(temp_tree,
  destination_root)` fails if `destination_root` is a non-empty
  directory (a POSIX rule that predates `os.replace`; `os.replace`
  does not apply to directories). Users frequently scaffold into an
  existing directory that contains `.git/`, `README.md`, or other
  pre-existing files — the very case where atomicity is most valuable.
  The current design handles this case correctly by committing files
  *into* the existing destination, not replacing it.

- **It requires the destination to be empty at commit start, or to be
  the destination's parent.** Neither is what a user of `forge create`
  expects. The current design works for both empty destinations (the
  common case) and existing ones (the second-most-common case), and the
  user's `--overwrite` flag controls the second case explicitly.

- **Cross-filesystem renames are not possible.** The temp tree must be
  on the same filesystem as the destination, exactly as the current
  staging tree is. The current design already provides this constraint
  via sibling placement; the copy-on-write alternative does not add
  safety, only ceremony.

### Alternative 5: Keep the current "write directly to destination" and rely on `--overwrite` for retries

Do not implement staging at all. Users who hit a partial run re-run
with `--overwrite`; the backup files created by `--overwrite` provide
recovery.

**Why rejected:**

- **This is the state the ADR is replacing.** It fails constraint (1)
  (atomic commit) — a user who interrupts a slow run halfway leaves
  the destination in a state where files 1–N are new (from the
  interrupted run) and files N+1–M are either new or old (from the
  re-run). Neither `--overwrite` nor `--no-overwrite` distinguishes
  the two, so the user's only safe action is `rm -rf` and start over.
  Staging avoids this by *never writing* until it is sure it can write
  everything.

- **The retry path hides what went wrong.** A user who sees a partial
  project has to infer whether the run failed *for them* (a template
  error, an invalid name) or *for the tool* (a bug). Staging's
  `RenderingError` carries the finding code (`render.*` vs.
  `staging.*`), which tells the user immediately whether to fix their
  blueprint or report a bug. The "rely on --overwrite" approach offers
  no such distinction.

## References

- `docs/adr/0006-dual-mode-input-handling.md` — the earlier decision
  that a CLI must be usable in both interactive and scripted contexts;
  staging is what makes the scripted path fail safely.
- `docs/adr/0014-sandboxed-deterministic-templates.md` — the renderer's
  determinism guarantee, without which staging's "same input, same
  output" guarantee could not hold.
- `docs/adr/0017-filesystem-safety-layer.md` — the safety layer above
  staging; its `StagingArea` is the same class this ADR's staging tree
  is anchored in.
- `docs/adr/0018-dry-run-is-a-pure-plan-builder.md` — the read-only
  staging mode exists for the dry-run path; the two ADRs describe
  adjacent parts of the same "no writes unless committed" discipline.
- `docs/reference/STAGING.md` — the reference for the staging phase's
  public API, guarantees, non-guarantees, and error codes.
- `docs/security/FILESYSTEM_SAFETY.md` — the threat model for the
  destination (traversal, symlinks, collisions) that staging inherits
  from the safety layer.
- `src/forge/safety/staging.py` — the `StagingArea` implementation,
  with its own docstring covering the read-only mode, detach semantics,
  and duplicate-write behavior.
- `src/forge/staging/engine.py` — the `StagingEngine` implementation,
  with its own docstring covering the manifest ordering, progress-event
  emission, and abort path.
