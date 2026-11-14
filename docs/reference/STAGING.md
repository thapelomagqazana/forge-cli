# Staging — Reference

The staging phase renders a `PlannedFile[]` into a hidden tree, then
hands that tree to a downstream consumer (the commit phase, the verifier,
or a dry-run renderer). This document is the reference for that phase:
what it produces, when it produces it, what it refuses, and how a caller
is expected to use it.

For the reasoning behind the design (why staging exists, why the tree
lives where it does, why certain guarantees are structural rather than
enforced by defensive checks), see
`docs/adr/0019-staging-is-atomic-and-invisible.md` and the module
docstrings under `src/forge/staging/`. This document is the *contract*;
those documents are the *rationale*.

---

## 1. The pipeline, in one diagram

```
PlannedFile[]  ──► StagingEngine.stage(...)  ──►  Ok(StagingResult)
                                                    │
                                                    ▼
                                     commit / verify / destroy
                                                    │
                                                    ▼
                                          destination_root filled

                 │
                 └──► Err(RenderingError)
                        │
                        ▼
                  no destination writes; staging tree gone
```

Every `forge create` invocation ends in one of those two outcomes.
Nothing else. No partial output. No silent partial success.

---

## 2. Public API

The public surface is exported from `forge.staging`:

```python
from forge.staging import (
    ProgressEvent,
    ProgressSink,
    RenderingError,
    StagingEngine,
    StagingFinding,
    StagingResult,
    null_sink,
)
```

### `StagingEngine.stage()`

```python
StagingEngine().stage(
    plan: list[PlannedFile],
    *,
    variables: Mapping[str, object],
    blueprint_root: Path,
    destination_root: Path,
    progress: ProgressSink = null_sink,
    renderer: TemplateRenderer | None = None,
) -> Result[StagingResult, RenderingError]
```

| Parameter | Meaning |
|-----------|---------|
| `plan` | The files to produce, in processing order. Produced by `FileMapper.map()` — see `PLANNING.md`. |
| `variables` | Resolved variable values. Snapshotted into a plain `dict` at entry, so a template that mutates its context cannot affect a sibling's render. |
| `blueprint_root` | Directory `source:` paths are resolved against. Never the process CWD. |
| `destination_root` | Where the committed files will end up. The engine uses it only to anchor the staging tree; it never writes to `destination_root`, and never creates it. |
| `progress` | Advisory sink for `ProgressEvent`s. Default: `null_sink` (a no-op). |
| `renderer` | Optional injected `TemplateRenderer`. Default: `None` — the engine's per-file loop lazily constructs one for render-kind files only. See §8. |

Returns `Ok(StagingResult)` on success or `Err(RenderingError)` if any
file failed. It **never raises** on expected input problems — missing
templates, undefined variables, sandbox denials, and I/O errors are all
converted to structured findings. It may raise on exceptional conditions
(a bug in the engine, a sink that raises, an OS-level surprise that is
not `OSError`-shaped); those propagate to `handle_exception` at the CLI
boundary.

### `StagingResult`

| Attribute | Meaning |
|-----------|---------|
| `root` | Absolute path to the populated, renamed staging tree (`<destination>/.forge-stage-XXXX-ready`). |
| `destination_root` | The destination the caller passed in. Stored so downstream phases do not re-resolve it (which could differ under `chdir`, symlinks, or CI working-directory overrides). |
| `manifest` | Sorted `tuple[(destination_relative_path, kind)]`. `kind` is `"render"` or `"copy"`. The source of truth for commit iteration — see §5. |
| `destroy()` | Removes the staging tree. Idempotent. Callers are responsible for calling this on any exit path that does not commit successfully. |
| `destroyed` | Read-only property; `True` once `destroy()` has run. |

### `RenderingError`

| Attribute | Meaning |
|-----------|---------|
| `findings` | Tuple of `StagingFinding` — every failure observed in the run. Never empty. |
| `operation` | Phase label. Always `"staging"` for this engine; future phases may reuse the type with a different label. |

`StagingFinding` has four fields:

| Field | Meaning |
|-------|---------|
| `code` | Machine-readable classifier. See §7. |
| `path` | Destination-relative path. For a render failure, this is *what the user was trying to produce*, not the template source — the source is in `message`. |
| `message` | One-sentence description. Never a traceback; never internal details. |
| `hint` | What to do about it. Mandatory. |

### `ProgressEvent`

| Field | Meaning |
|-------|---------|
| `stage` | One of `"start"`, `"render"`, `"copy"`, `"finish"`. |
| `index` | 1-based position of the file. `0` on `start`; equals `total` on `finish`. |
| `total` | `len(plan)`. Stable across all events of a single `stage()` call. |
| `path` | Destination-relative path. Empty on lifecycle events. |
| `elapsed_ms` | Milliseconds — since the phase started for lifecycle events; since the file began for per-file events. Informational only. |

`ProgressSink` is a structural protocol: any callable with signature
`(event: ProgressEvent) -> None`. `null_sink` is the no-op default.

---

## 3. Where the staging tree lives

**Sibling of `destination_root`, not inside it.**

If `destination_root` is `/projects/demo`, the staging tree is
`/projects/.forge-stage-<random>`. When ownership is transferred to the
caller, the tree is renamed to `.forge-stage-<random>-ready` — still a
sibling, now with a suffix that says "populated and detached."

Three reasons for sibling placement (see ADR-0019 for the full argument):

1. **No destination writes before commit.** A failed run must not
   create the destination just to hold the staging tree — that would
   leave an empty directory behind, which is a mutation. Sibling
   placement makes the engine's *capability* to create the destination
   non-existent, rather than merely unreached by defensive code.
2. **Atomic commit.** `os.replace` is atomic iff source and destination
   are on the same filesystem. Both are children of `destination.parent`,
   so both are on the same device by construction — the guarantee does
   not depend on the OS choosing one filesystem over another.
3. **Invisible during the run.** The leading dot hides the staging tree
   from plain `ls`, and its location outside `destination_root` means a
   user browsing their project during a run sees nothing at all.

The trade-off, documented in §6, is that the destination root may not
exist while the staging tree does. That is deliberate.

---

## 4. What the engine refuses

Staging does not enforce *user-input* safety — that is the safety
layer's job (see `FILESYSTEM_SAFETY.md`). Staging enforces
*write-discipline* safety:

| Situation | Outcome |
|-----------|---------|
| `planned.kind == COPY`, source missing | `Err` with `copy.not_found` finding. |
| `planned.kind == COPY`, source is a directory | `Err` with `copy.is_directory` finding. |
| `planned.kind == COPY`, source unreadable | `Err` with `copy.io_error` finding. |
| `planned.kind == RENDER`, template has undefined variable | `Err` with `render.undefined_variable` finding. |
| `planned.kind == RENDER`, template has syntax error | `Err` with `render.syntax_error` finding. |
| `planned.kind == RENDER`, template attempts sandbox escape | `Err` with `render.sandbox_denied` finding. |
| `planned.kind == RENDER`, template includes a missing file | `Err` with `render.include_not_found` finding. |
| `planned.kind == RENDER`, template is not UTF-8 | `Err` with `render.io_encoding` finding. |
| Any write whose destination escapes staging | `Err` with `staging.write_rejected` finding. **Always an engine bug** — the planner already rejected unsafe destinations. |
| Any file in `manifest` missing from the staged tree after the run | `Err` with `staging.missing` finding. **Always an engine bug.** |
| Any file in the staged tree not in `manifest` | `Err` with `staging.unexpected` finding. **Always an engine bug.** |
| Any symlink in the staged tree | `Err` with `staging.symlink` finding. **Always an engine bug.** |

The three `staging.*` codes (plus the parent `staging.write_rejected`)
indicate bugs in *this engine*, not in the user's blueprint. Their
`hint` field says so; a user who sees one should report it. The
`render.*` and `copy.*` codes indicate problems with the user's
blueprint or input; their hints name the specific fix.

---

## 5. Using `StagingResult` — the four-phase contract

After a successful `stage()` call, the caller owns a populated tree and
is responsible for what happens to it. There are three sanctioned
consumers and one cleanup obligation:

### 5.1 Verify (optional)

The verifier reads from `result.root`. It never writes to it — the tree
is a snapshot of the plan's output, and mutating it would defeat the
"what you verified is what you commit" guarantee.

The verifier does not exist yet. When it does, it will take a
`StagingResult` and return `Result[None, VerificationError]`; its
`Err` path is the caller's responsibility (see 5.4).

### 5.2 Commit

The commit phase moves every entry in `result.manifest` from
`result.root / relpath` to `result.destination_root / relpath` via
`os.replace`, creating intermediate directories as needed. It iterates
the **manifest** — not the filesystem — so a file that somehow appears
in the staging tree without being in the plan cannot leak to the
destination. Commit is per-file atomic (`os.replace`), but not
cross-file transactional; see §6.

The canonical implementation is `_commit_staged` in
`src/forge/cli/commands/create.py`; a caller that wants a different
policy (say, streaming commits to a network destination) can write its
own, but must honour three rules:

1. Iterate `manifest`, not the tree.
2. `os.replace` each file (never `shutil.move` — the latter silently
   degrades to a non-atomic copy across filesystems, defeating the
   guarantee).
3. Call `result.destroy()` in a `finally` block, whatever happens.

### 5.3 Dry-run render

A dry-run renderer reads the tree to produce a preview of what *would*
have been committed. It never writes to the tree and never mutates the
destination. `forge create --dry-run` does not currently consume
`StagingResult` (it uses `DryRunService` instead — see
`DRY_RUN_PLAN.md`), but a future "render to preview" mode might; the
contract is the same: read-only, never commit, always `destroy()`.

### 5.4 Destroy

If any consumer fails — verifier `Err`, commit `OSError`, a downstream
cancellation — the caller must call `result.destroy()`. The method is
idempotent and best-effort; a caller in a `finally` block can call it
unconditionally.

A leaked `StagingResult` leaks a `*.forge-stage-*-ready` directory next
to the destination. It is hidden and named, so it is harmless in the
short term, but it is debris a user will eventually notice and cannot
safely delete without understanding what it is.

---

## 6. Guarantees, and their exact scope

Staging is designed to make certain problems impossible. Those
guarantees have *precise* boundaries, and this section names them so a
caller knows what to rely on and what to defend against itself.

### 6.1 The guarantees

- **No destination writes before commit.** The engine cannot write to
  `destination_root`; its `StagingArea` is scoped to a sibling. A
  failed `stage()` leaves the destination byte-identical — verified by
  a filesystem fingerprint in the test suite.
- **No partial output on failure.** A failed run destroys the staging
  tree before returning `Err`. The caller sees either a fully populated
  tree or an empty filesystem.
- **Deterministic output.** Same `plan` + same `variables` + same
  `blueprint_root` produces byte-identical `manifest` (sorted) and
  byte-identical file contents (the renderer is deterministic per
  ADR-0014).
- **One event per file.** `ProgressSink` receives exactly one event per
  planned file, in plan order, plus one `start` and one `finish`.
- **Ownership is explicit.** `StagingResult` is the only way to reach
  the populated tree. There is no global registry, no environment
  variable, no "default staging directory" a caller might accidentally
  find.

### 6.2 The non-guarantees

These are *deliberately not* provided; a caller that needs them must
implement them above staging:

- **Cross-file atomicity.** A commit that fails on file 5 leaves files
  1–4 committed and files 6–N untouched. Files 1–4 are complete (each
  was `os.replace`d atomically); the destination is not in an
  inconsistent state *within any one file*, but it is not
  "all-or-nothing across files" either. The `--overwrite` path in
  `forge create` creates backups before staging, so a caller that wants
  recoverable semantics has them; a caller that wants *transactional*
  semantics must build it above staging.
- **The destination exists during staging.** A caller that needs to
  read from the destination mid-staging (say, to diff against an
  existing project) must create it first — staging will not.
- **Cleanup on process kill.** A `SIGKILL` mid-staging leaves the tree
  behind. The context manager's `finally` runs on `SIGINT`, `SIGTERM`,
  and any Python exception, but not on a signal that terminates the
  process without unwinding. Users must remove a `.forge-stage-*-ready`
  directory manually after a `kill -9`; the name is documented precisely
  so they can recognise it.
- **Resource limits.** A pathological plan (millions of files, GB of
  rendered output) will exhaust whatever limits the OS imposes. Staging
  does not enforce per-run budgets; the OS's container, quota, or
  `ulimit` does.

---

## 7. Error code vocabulary

Codes are stable across releases. Adding a code is a minor change
(documented here, with a note in `CHANGELOG.md`); renaming or removing
a code is a breaking change requiring a major version bump.

### `copy.*` — problems reading the source of a copy-kind file

| Code | Meaning |
|------|---------|
| `copy.not_found` | The `source:` path does not exist under `blueprint_root`. |
| `copy.is_directory` | The `source:` path is a directory; the blueprint's `files[]` entry was meant to point at a file. |
| `copy.io_error` | A non-`ENOENT`, non-`EISDIR` read error (permissions, disk error, encoding). |

### `render.*` — problems rendering a render-kind template

| Code | Meaning |
|------|---------|
| `render.undefined_variable` | The template referenced a variable that was not supplied. |
| `render.syntax_error` | The template is not valid Jinja syntax. |
| `render.sandbox_denied` | The template attempted something the sandbox forbids. |
| `render.include_not_found` | The template included another template that does not exist. |
| `render.io_encoding` | The template file is not valid UTF-8. |
| `render.io_not_found` | The template file does not exist. *Rare*: the planner normally catches this at planning time; this code covers the race where the file is deleted between planning and staging. |
| `render.io_error` | A non-`ENOENT`, non-`EISDIR` read error on the template. |
| `render.error` | The rendering phase produced a code this map does not recognise. **A bug in the engine** — a rendering code was added without extending `_staging_code_for`. |

### `staging.*` — engine bugs

| Code | Meaning |
|------|---------|
| `staging.write_rejected` | The staging area refused a destination path the plan promised. The planner should have rejected it; the fact that it did not is a bug. |
| `staging.missing` | A file in the manifest is not in the staged tree after the run. |
| `staging.unexpected` | A file is in the staged tree that is not in the manifest. |
| `staging.symlink` | A symlink is in the staged tree. Symlinks are never legitimate — the engine writes bytes. |
| `staging.missing_root` | The staging tree itself does not exist when the engine expected it to. |

A user who sees a `staging.*` finding should report it with the
blueprint they ran against; the engine's `hint` says so.

---

## 8. The injected renderer

`StagingEngine.stage()` accepts `renderer: TemplateRenderer | None`.
This is a seam for callers that need to *observe* or *substitute*
rendering:

- **Tests** that want to assert "the renderer was never invoked on a
  copy-kind file" inject a recording subclass and inspect its call log
  after the run. The alternative — monkeypatching the renderer's class
  method — depends on class-identity assumptions the engine does not
  guarantee and breaks silently when those assumptions stop holding.
- **Future features** (a render-preview mode, a metrics collector, a
  `forge render` command that renders without staging) inject their
  own renderer to control construction lifetime, error handling, or
  observable side effects.

The default (`None`) restores the same behaviour as before the seam
existed: `render_or_copy` lazily constructs a `TemplateRenderer()` for
render-kind files and never constructs one for copy-kind files.

The renderer must satisfy the interface `TemplateRenderer` exposes —
specifically, a `render(template_path, variables) -> Result[str,
TemplateRenderError]` method. The engine does not enforce this
statically (the parameter is annotated `TemplateRenderer | None` for
the benefit of type-checkers, not for runtime enforcement), so a caller
that injects a duck-typed substitute accepts that the failure mode is
an `AttributeError` at call time. For most callers, subclassing is the
right choice.

---

## 9. Concurrency

`StagingEngine` is stateless. Two `stage()` calls for *different*
`destination_root`s are safe to run concurrently in the same process:
each owns its own staging tree, and `tempfile.mkdtemp` guarantees no
collision between the two directory names.

Two `stage()` calls for the *same* `destination_root` are **not**
guaranteed safe. `mkdtemp` prevents staging-tree collisions, but the
commit phase is not idempotent across concurrent callers — both would
try to `os.replace` files to the same destination paths, and the
outcome would depend on scheduling. A caller that needs concurrent
creates against the same destination (a batch tool spawning workers,
say) must serialise the commit phase itself; staging alone does not
provide the guarantee.

`ProgressSink` implementations may be invoked from multiple concurrent
`stage()` calls if the caller passes a shared sink. The engine does not
synchronise calls; a sink that needs thread-safety must provide it. The
default `null_sink` and the `ListSink` helper in
`forge.staging.progress` are thread-safe for typical use (both are
trivial functions/appends), but a richer sink (a UI's progress bar,
say) is the caller's responsibility.

---

## 10. Testing staging

The staging test suite lives in three places, split by *what the test
verifies*:

- `tests/unit/staging/test_engine_happy.py` — the success path:
  file contents, structure, kind dispatch, placement. Fixture-sanity
  tests at the top guard the input against drift.
- `tests/unit/staging/test_engine_render_failure.py` — the failure
  path: aggregation, cleanup, byte-identical destination.
- `tests/unit/staging/test_engine_progress.py` — the sink contract:
  one event per file, ordering, fields.
- `tests/unit/staging/test_engine_invisibility.py` — the
  user-visible invariant: no debris after a successful run.
- `tests/unit/staging/test_copy_kind.py` — the copy-vs-render
  distinction, including the injected-renderer spy.
- `tests/integration/test_staging_end_to_end.py` — the wiring between
  `forge create` and the engine, through the CLI binary.

A test that duplicates another layer's coverage belongs in *one* of
those files, not both. When adding a test, the first question is "can
this test live without a real subprocess?" — if yes, it belongs in one
of the unit files; if no, it belongs in `integration/`. The rule of
thumb is the same one `test_staging_end_to_end.py`'s module docstring
states: an integration test exercises something *only the process
boundary can break*.

The staging tree's fixture lives at
`tests/fixtures/staging/`. Its README documents every fixture's
invariants — including the ones no compiler enforces (the templates
end with exactly one newline; the PNG contains NUL bytes). Those
invariants are enforced by the fixture-sanity tests at the top of
`test_engine_happy.py`; a fixture edit that breaks one fails there
first, with a message that names the fixture, not a downstream
symptom.

---

## 11. Related documents

| Document | Why read it |
|----------|-------------|
| `docs/adr/0019-staging-is-atomic-and-invisible.md` | The design decisions behind the guarantees in §6. |
| `docs/reference/PLANNING.md` | How `PlannedFile[]` is produced — the input to staging. |
| `docs/reference/TEMPLATE_FILTERS.md` | What render-kind files may contain — the renderer's allowlist. |
| `docs/reference/DRY_RUN_PLAN.md` | How `--dry-run` produces a plan without touching staging. |
| `docs/security/FILESYSTEM_SAFETY.md` | The safety layer *above* staging — destination validation, collision detection, overwrite consent. |
| `docs/reference/EXIT_CODES.md` | How `RenderingError` maps to a process exit code (8, `RENDERING_ERROR`). |
