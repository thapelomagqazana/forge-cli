"""The generation pipeline — turning a plan into files on disk.

What this module is
-------------------
Generation is the *commit* phase: it takes the finished, validated
plan (``list[PlannedFile]``) plus the resolved variables and produces
files in the destination directory. It is the only place in the
codebase that writes to the *user's* filesystem, and it does so
through two non-negotiable invariants:

    1. **Every write goes through staging** (``forge.safety.StagingArea``).
       There is no API here that writes to a destination directly.
       Callers who want files on disk call ``scaffold_from_plan``;
       there is no second way.

    2. **Every overwrite is backed up** (``forge.safety.backup_existing``).
       When the plan replaces an existing file, the original is copied
       to a timestamped sibling *before* any write, atomically, with
       rollback on failure. The user never loses data that the plan
       overwrote.

Both invariants are already enforced by the safety layer (ADR-0017);
this module is where the *composition* of them happens — the code
that calls ``backup_existing``, then ``StagingArea``, in that order,
with the plan. Moving that composition here (rather than leaving it
in ``cli.commands.create``) is what lets a future ``forge generate``
(or an HTTP endpoint, or a batch runner) reuse the exact same
pipeline without re-implementing the ordering.

Why ``scaffold_from_plan`` and not, say, ``write_plan``
-------------------------------------------------------
"Scaffold" is the domain verb: the CLI, the docs, and every user-
facing message say "scaffold a project". "Write" would describe the
implementation (files on disk), which is the wrong level of
abstraction — a future generation path might not write files at all
(e.g. a ``--dry-run`` variant that returns bytes for a manifest).
"Scaffold" describes the *intent* (produce a project from a
blueprint), and it leaves room for the intent to grow without the
name becoming a lie.

Naming symmetry: ``FileMapper.map`` produces the plan;
``scaffold_from_plan`` consumes it. Both verbs are about the artifact
(the project, the plan), not the mechanism (write, copy). That
symmetry is what makes the code read as a sentence:
``plan = FileMapper().map(...); scaffold_from_plan(plan, ...)``.

What this module deliberately does *not* export
-----------------------------------------------
``StageResult``, ``BackupRecord``, and every internal helper stay
private. The public surface is exactly one function:

    scaffold_from_plan(plan, *, variables, destination, overwrite,
                       blueprint_root, renderer=None) -> ScaffoldResult

Adding to ``__all__`` is non-breaking; removing is breaking. The
discipline: the façade's names are the *pipeline's verbs*, not its
data structures. A caller that needs a data structure imports it
from the module that *defines* it (``forge.safety.Backup``,
``forge.planning.PlannedFile``), because those types are stable on
their own axes and should not be re-exported through a façade that
is trying to say "here is what generation *does*".

Why ``scaffold_from_plan`` has a ``# type: ignore[no-any-unimported]``
---------------------------------------------------------------------
``TemplateRenderer`` is imported from ``forge.rendering``, whose
façade lazy-loads the concrete class from ``forge.rendering.renderer``,
which in turn imports ``jinja2`` — a package gated behind
``ignore_missing_imports`` in ``pyproject.toml``. Mypy can therefore
see the *name* ``TemplateRenderer`` but cannot resolve its shape, and
treats the parameter type ``TemplateRenderer | None`` as
``Any | None``. Strict mode's ``[no-any-unimported]`` check flags
that.

The suppression is narrowly scoped: it covers only
``[no-any-unimported]``, only on this one ``def`` line. The parameter's
*runtime* contract is unchanged — the caller passes either a
``TemplateRenderer`` instance or ``None``, and passing anything else
fails at ``.render(...)`` with a clear ``AttributeError``. If
``jinja2`` ever gains type stubs (or the project drops
``ignore_missing_imports`` for it), the ignore becomes unused and
mypy's ``unused-ignore`` check — on under strict mode — will force
its removal in the same PR that lands the stubs. That is the desired
signal: the ignore is a *placeholder for an external dependency's
gap*, not a permanent part of this module's contract.

Why the commit phase is inlined rather than delegated
-----------------------------------------------------
An earlier revision of this module delegated to
``cli.commands.create._generate`` to avoid duplicating the commit
loop while the CLI was being built out. That delegation is now gone,
for two reasons:

    1. **The signatures diverged.** ``_generate`` now takes an
       opaque ``_ResolvedInputs`` bundle plus ``overwrite`` — it
       builds the plan *and* commits it. ``scaffold_from_plan`` is
       handed a finished plan by its caller (that is its whole
       contract). Calling ``_generate`` would build a second plan
       from the blueprint and commit *that*, ignoring the caller's
       plan entirely. A façade that silently ignores its primary
       argument is not a façade.

    2. **The adapter shims were unsound.** To call ``_generate``,
       this module had to manufacture a ``_ResolvedInputs``-shaped
       object (via ``_InputsAdapter``) and a ``Blueprint``-shaped
       stand-in (via ``_BlueprintShim``). Both were held together
       by ``# type: ignore[arg-type]`` comments, and neither could
       ever be *right* — ``_ResolvedInputs`` carries a real
       ``Blueprint`` document, and a shim cannot provide one
       because the caller never had one. The façade's job is to
       *use* the plan, not to fabricate the inputs that would let
       a different function build one.

The inlined commit loop below is short, uses only the primitives
the CLI's ``_generate`` uses (``backup_existing`` and
``StagingArea``), and is the natural home for the ordering invariant
the module docstring states. It also eliminates the last runtime
dependency on ``forge.cli`` from this module — ``forge.generation``
is now importable without pulling in the CLI.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from forge.core.exceptions import (
    FilesystemConflictError,
    TemplateRenderError,
)
from forge.planning import PlannedFile
from forge.rendering import TemplateRenderer
from forge.safety import StagingArea, backup_existing

if TYPE_CHECKING:
    from forge.rendering.errors import TemplateRenderError as RenderingTemplateError


@dataclass(frozen=True, slots=True)
class ScaffoldResult:
    """The outcome of a successful scaffold.

    Returned by ``scaffold_from_plan`` so a caller can report "wrote
    12 files" without re-listing the directory. The result is
    *minimal* by design — it carries:

        * ``written`` — the final destination paths, in commit order.
          This is what a caller needs to (a) print a count, (b) log
          what changed, (c) drive a future ``forge status``.

        * ``backups`` — the timestamped siblings created for
          overwritten files. Empty when ``overwrite=False`` or when
          nothing existed to overwrite. This lets the caller say
          "3 files replaced; backups at *.bak.20240101T000000Z"
          without re-walking the filesystem.

    Not included:

        * **Timing.** Timing is a *measurement* concern, not a
          *generation* concern. A caller that wants timing wraps the
          call with ``time.perf_counter()`` around it; embedding
          timing here would force the result to be non-frozen and
          invariant under test, which is the opposite of what it is
          for.

        * **Rendered bytes.** The bytes were consumed by the commit
          phase; keeping them would double memory per call and
          defeat the streaming nature of staging. A caller that
          wants bytes re-renders (the template cache makes this
          cheap) or inspects the committed files (which are now on
          disk).

        * **Errors.** Failures raise — see ``scaffold_from_plan``'s
          docstring for the exception contract. A result type with
          an optional error field is a *worse* ``Result``; the
          caller already knows how to handle ``Result`` from the
          loader and planner, and consistency wins over a bespoke
          shape here.

    Frozen and slotted: a result is a value; mutating it is
    meaningless and would surprise a caller that caches it.
    """

    written: tuple[Path, ...]
    backups: tuple[Path, ...]


# ``[no-any-unimported]`` is suppressed on the ``def`` line only. See
# the module docstring's "Why ``scaffold_from_plan`` has a
# ``# type: ignore[no-any-unimported]``" section for the reasoning.
# The suppression is load-bearing for the *type checker* (which cannot
# follow ``TemplateRenderer`` through the ``forge.rendering`` façade
# into the ``jinja2``-gated module) but does not affect runtime
# behaviour: the parameter is a ``TemplateRenderer`` (or ``None``) at
# call time, and passing anything else fails at ``.render(...)`` with
# a clear ``AttributeError``.
def scaffold_from_plan(  # type: ignore[no-any-unimported]
    plan: list[PlannedFile],
    *,
    variables: Mapping[str, Any],
    destination: Path,
    overwrite: bool,
    blueprint_root: Path,
    renderer: TemplateRenderer | None = None,
) -> ScaffoldResult:
    """Produce a project from a plan.

    This is the *only* function in the codebase that writes files for
    a scaffolded project. Every other module either produces the plan
    (``FileMapper``), validates it (``forge.safety``), or reports the
    result (the CLI). The ordering below is the invariant:

        1. **Backup existing targets** (only when ``overwrite=True``).
           Runs *before* any staging write, so an interrupted commit
           leaves a recoverable state: the backups are complete, and
           the destination is unchanged (staging is not yet committed).

        2. **Stage every file.** Renders each ``PlannedFile`` into a
           hidden ``.forge-stage-*`` directory inside ``destination``.
           No file outside staging is touched. If any render fails,
           the staging directory is removed by ``StagingArea``'s
           context manager and the destination is untouched.

        3. **Commit.** Each staged file is moved to its final
           destination via a per-file atomic ``os.replace``. A failure
           mid-commit leaves the *already-committed* files in place
           (they were moved atomically; each is complete), the
           failed file still in staging (removed by the context
           manager's ``finally``), and the remaining files still in
           staging (also removed). The backups from step 1 are what
           the user restores from.

    Args:
        plan:
            The output of ``FileMapper.map``. Sorted by destination;
            the writer iterates in that order so log output is
            stable. Empty is legal — a blueprint with no files still
            creates the destination directory (the caller's job, see
            below) and returns an empty ``ScaffoldResult``.
        variables:
            The resolved variables (``ResolvedValues`` mapped to a
            ``dict`` by the caller, or any mapping). Passed through
            to the renderer; the writer does not inspect values.
        destination:
            The *final* directory the project will occupy. Must be an
            ancestor of every plan destination. Must exist (the
            caller creates it: ``StagingArea.for_root`` uses
            ``mkdtemp`` which requires an existing parent).
        overwrite:
            If ``True``, existing files at plan destinations are
            copied to timestamped backups before being replaced. If
            ``False``, this function *assumes* the caller has already
            verified there are no collisions (via
            ``detect_collisions``) — passing ``overwrite=False`` with
            an existing file will let staging overwrite it silently.
            The contract is: **the caller owns collision policy; this
            function owns overwrite mechanics.** That split keeps
            the function total over its arguments — it does one thing
            (given a plan, produce files) with one parameterisation
            (backup or not).
        blueprint_root:
            The directory plan ``source`` paths are relative to. The
            caller resolves this from the discovery layer (see
            ``create._blueprint_root``); the writer uses it to read
            template/copy sources.
        renderer:
            The template renderer to use. Injectable for testing and
            for future callers with a different rendering
            requirement. Defaults to ``TemplateRenderer()`` — the
            sandboxed, allowlisted renderer (ADR-0014) that every
            current caller should be using.

    Returns:
        ``ScaffoldResult`` with the written paths (in commit order)
        and the backup paths (empty if none were made).

    Raises:
        ``forge.core.exceptions.FilesystemConflictError`` — an I/O
            failure during backup or during staging (unwritable
            parent, disk full, permission denied). The hint names the
            recovery ("check permissions", "no files have been
            modified").
        ``forge.core.exceptions.TemplateRenderError`` — a template
            failed to render. The message includes the template path,
            line, and the renderer's own hint.
        ``forge.core.exceptions.SafetyError`` /
            ``forge.core.exceptions.CollisionError`` — *not raised
            here*. The safety check runs in the caller's pre-flight
            (see ``create._generate``) so the writer's inputs are
            already known-safe. If the caller skips that check and
            passes an unsafe plan, this function will not detect it
            — the safety layer's contract is "callers check before
            writing", and this function honours that contract rather
            than duplicating the check.

    Why the function is *not* pure with respect to the filesystem:
        It is *total* (never crashes, always produces a result or a
        typed exception) but not pure (it writes). That is deliberate
        and honest: a "pure" writer would have to be a data structure
        the caller then applies, which is exactly the layering we are
        trying to avoid. The purity lives in ``FileMapper`` (plan)
        and ``forge.safety`` (validate); this function is the *only*
        impure step, and it is short enough to read in one sitting.
    """
    effective_renderer = renderer if renderer is not None else TemplateRenderer()

    # ------------------------------------------------------------------
    # Phase A: backups (opt-in via --overwrite, and only for files
    # that already exist at plan destinations).
    #
    # Runs *before* any staging write. If this phase fails, the caller
    # sees a ``FilesystemConflictError`` naming the I/O problem, and
    # the destination is unchanged — no files have been touched.
    # ------------------------------------------------------------------
    backups: tuple[Path, ...] = ()
    if overwrite:
        existing = _existing_targets(plan, destination)
        if existing:
            try:
                created = backup_existing(existing)
            except OSError as exc:
                raise FilesystemConflictError(
                    f"Could not back up existing files before overwrite: {exc}",
                    hint=(
                        "Check permissions and free disk space, then retry. "
                        "No files have been modified."
                    ),
                ) from exc
            backups = tuple(b.backup for b in created)

    # ------------------------------------------------------------------
    # Phase B: staging and commit.
    #
    # ``StagingArea.for_root`` creates a hidden ``.forge-stage-*``
    # directory *inside* ``destination`` (same filesystem, so
    # ``os.replace`` is atomic). The context manager removes it on
    # exit, including on exception. This is the *only* phase that
    # mutates the destination root; phase A writes only to backup
    # files, which are new and outside the plan's target set.
    #
    # ``mkdtemp`` (used by ``StagingArea.for_root``) requires its
    # parent to exist. Creating the root is idempotent and
    # side-effect-free if it already exists.
    # ------------------------------------------------------------------
    destination.mkdir(parents=True, exist_ok=True)

    with StagingArea.for_root(destination) as stage:
        for planned in plan:
            _stage_one(
                stage=stage,
                renderer=effective_renderer,
                planned=planned,
                blueprint_root=blueprint_root,
                variables=variables,
            )
        committed = stage.commit()

    # ``committed`` is the authoritative list of written paths — the
    # staging area returns them in commit order, which is the same
    # order as ``plan`` (the caller sorted the plan by destination).
    # Using ``committed`` rather than re-deriving from ``plan`` keeps
    # the result *honest* if a future staging-area implementation ever
    # skipped a file (e.g. a no-op for an empty source). The plan is
    # the *intent*; the commit result is what *happened*.
    return ScaffoldResult(
        written=tuple(committed),
        backups=backups,
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _existing_targets(plan: list[PlannedFile], root: Path) -> list[Path]:
    """Return absolute paths of plan destinations that exist on disk.

    Uses ``lstat`` — a broken symlink is a name in use and will be
    replaced by ``--overwrite``, so it must be backed up. Fail closed:
    an unreadable entry is treated as existing, because a spurious
    backup is cheap and a missed backup is data loss.

    Kept identical to ``create._generate``'s helper because both call
    sites perform the same check against the same plan shape. A future
    consolidation (hoisting this into ``forge.safety``) would let both
    callers share it; until then, the duplication is small enough
    (~15 lines) that an extra import edge isn't worth it.
    """
    existing: list[Path] = []
    for planned in plan:
        target = root / planned.destination
        try:
            os.lstat(target)
        except FileNotFoundError:
            continue
        except OSError:
            pass  # fail closed: treat as existing
        existing.append(target)
    return existing


def _stage_one(
    *,
    stage: StagingArea,
    renderer: Any,  # see the docstring for why this is ``Any``
    planned: PlannedFile,
    blueprint_root: Path,
    variables: Mapping[str, Any],
) -> None:
    """Render or copy one planned file into the staging area.

    * ``COPY`` — read bytes verbatim and write them to staging.
    * ``RENDER`` — render through the sandboxed renderer; translate
      any ``TemplateRenderError`` into the CLI error hierarchy.

    ``renderer`` is typed ``Any`` rather than ``TemplateRenderer``
    because the latter is unfollowed by mypy (see the module
    docstring's ``# type: ignore[no-any-unimported]`` section) — a
    ``TemplateRenderer`` annotation would just re-introduce the same
    ``[no-any-unimported]`` diagnostic on *this* function's ``def``
    line. ``Any`` is honest here: the runtime contract is enforced by
    ``renderer.render(...)`` failing with a clear ``AttributeError``
    if the caller supplied the wrong object, and the *caller* (which
    is ``scaffold_from_plan``, whose own parameter carries the real
    ``TemplateRenderer | None`` annotation) has already type-checked
    the value before passing it down. The parameter is *internal* to
    this module — it is not part of the public surface — so loosening
    its type costs nothing a caller can observe.
    """
    source_path = blueprint_root / planned.source

    if planned.is_render:
        result = renderer.render(source_path, variables)
        if not result.is_ok():
            raise _as_template_error(result.error, source=str(source_path))
        stage.write_text(planned.destination, result.value)
        return

    try:
        data = source_path.read_bytes()
    except OSError as exc:
        raise FilesystemConflictError(
            f"Could not read source file {planned.source!r} for copy: {exc}",
            hint="Check the blueprint's file entry; the source must be readable.",
        ) from exc
    stage.write_bytes(planned.destination, data)


def _as_template_error(
    error: RenderingTemplateError,
    *,
    source: str,
) -> TemplateRenderError:
    """Translate a rendering-side error into the CLI's error type.

    The rendering-side error carries ``template``, ``line``, ``col``,
    and ``name`` — richer than the CLI's ``TemplateRenderError``
    (which is a ``StructuredError`` with ``(pointer, message)``
    pairs). This function projects them into a single tuple so the
    handler can render it uniformly.

    Identical to ``create._generate``'s helper of the same name.
    Kept local rather than imported from the CLI because importing
    it would re-introduce the module-level dependency on
    ``forge.cli`` that the inlined implementation was introduced to
    remove.
    """
    pointer = ""
    if error.line is not None:
        pointer = f"line {error.line}"
        if error.col is not None:
            pointer += f", col {error.col}"
    return TemplateRenderError(
        source=source,
        errors=((pointer, error.message),),
    )


__all__ = ["ScaffoldResult", "scaffold_from_plan"]
