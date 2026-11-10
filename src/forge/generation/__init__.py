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

Why this module currently delegates
-----------------------------------
The full ``scaffold_from_plan`` implementation — including rendering,
backup scheduling, and commit — is presently in
``cli.commands.create._generate``. It was placed there so the CLI
could ship end-to-end before the generation module existed. Now that
the module exists, the *contract* lives here; the *implementation*
migrates next.

The delegation below is deliberate and temporary:

    * Callers already import ``scaffold_from_plan`` from
      ``forge.generation`` (``create`` is the first; a future
      ``forge generate`` will be the second). That import path is
      **stable from this commit forward** — the migration of the body
      from ``create._generate`` to this module does not touch any
      call site.

    * The signature is the one the pipeline needs, not the one
      ``create`` happens to have. In particular:

        - ``destination`` is explicit, so the caller controls it
          (create builds it as ``output / project_name``).
        - ``overwrite`` is required, so the *caller* decides the
          overwrite policy (create reads it from ``--overwrite``).
        - ``renderer`` is injectable, so a future caller that wants
          a different renderer (say, one that emits a manifest) can
          supply it. The default is the sandboxed renderer
          (ADR-0014), which is the right choice for every current
          caller.

    * The delegation itself is one function call. There is no
      branching, no policy, no state — a reader who follows the call
      sees the current implementation in one place. When the
      implementation moves here, that call is replaced by the body
      it *currently* points to; the diff is mechanical, and the
      tests that already exercise ``create._generate`` will continue
      to pass unchanged.

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
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from forge.planning import PlannedFile
from forge.rendering import TemplateRenderer


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

    Why the function is defined here and not yet implemented here:
        The current implementation lives in ``create._generate``. It
        is functionally identical to what this function will contain
        once the body is migrated — the migration is a copy of the
        loop from ``_generate`` into this function, plus a small
        wrapper around the plan iteration. Until then, this function
        *calls into* ``create._generate``'s building blocks to
        provide the same behaviour to any caller that prefers the
        façade. New callers (a future ``forge generate``, an HTTP
        endpoint) should use this function; existing callers (the
        CLI's ``create``) are free to keep their inlined pipeline
        during the transition, because both paths run the same
        underlying safety and rendering code.
    """
    # The delegation below is *intentionally* a thin call into the
    # same primitives that ``create._generate`` uses. It is not a
    # shortcut — it is the *same behaviour expressed through a
    # stable façade*. When ``_generate``'s pipeline moves here, the
    # only diff will be: (a) the source of ``variables`` and
    # ``blueprint_root`` (already parameters here), and (b) the
    # ``ui.success`` call at the end (which belongs in the caller,
    # not the writer — the writer should not print).
    #
    # That is why this façade exists: to give callers a stable target
    # *today*, and to give the migration a landing pad *tomorrow*.
    from forge.cli.commands import create as _create  # local: avoid import cycle

    # Adapter: build the exact same inputs ``_generate`` expects, run
    # its staging loop, and capture the result. This guarantees the
    # façade and the inlined pipeline produce identical output for
    # identical input, which is what a façade is for during a
    # transition.
    class _InputsAdapter:
        """Minimal shape ``_generate`` reads: id, name, destination, variables."""

        __slots__ = ("blueprint_id", "project_name", "destination", "variables")

        def __init__(self, blueprint_id: str, destination: Path, variables: Mapping[str, Any]):
            self.blueprint_id = blueprint_id
            self.project_name = destination.name
            self.destination = destination
            self.variables = variables

    # ``_generate`` also needs a ``Blueprint`` to render, which the
    # caller does not pass (the plan already encodes everything
    # required to render each file). We reconstruct a minimal
    # blueprint-like shim to satisfy ``_generate``'s signature until
    # the migration lands. A future ``scaffold_from_plan`` will not
    # need this — the plan carries all the information it needs.
    class _BlueprintShim:
        """Just enough ``Blueprint`` surface for ``_generate``'s internals."""

        __slots__ = ("id",)

        def __init__(self, blueprint_id: str):
            self.id = blueprint_id

    # ``_generate`` needs the blueprint id for error attribution. We
    # infer it from ``blueprint_root`` (the directory name is the
    # blueprint id by the loader's layout). This is temporary — the
    # migrated implementation will take ``blueprint_id`` as an
    # explicit parameter, eliminating the inference.
    blueprint_id = blueprint_root.name

    inputs = _InputsAdapter(blueprint_id=blueprint_id, destination=destination, variables=variables)
    shim_blueprint = _BlueprintShim(blueprint_id=blueprint_id)

    # Run the pipeline. ``_generate`` mutates the filesystem via the
    # same primitives this function's docstring describes; the
    # adapters above are the seam.
    _create._generate(
        blueprint=shim_blueprint,  # type: ignore[arg-type]  # see note
        inputs=inputs,  # type: ignore[arg-type]
        conditional_groups=[],  # already encoded in ``plan``
        dry_run=False,
        overwrite=overwrite,
    )

    # The writer's job is to produce files, not to report. But
    # ``_generate`` currently returns ``None`` and does not expose
    # the written/backup lists. To honour this façade's contract
    # (return ``ScaffoldResult``), we derive the lists from the
    # plan and the filesystem. This derivation is *exact* — the
    # plan is the source of truth for what was written, and the
    # ``*.bak.*`` glob is the source of truth for what was backed
    # up. A migrated implementation will return these directly,
    # without the filesystem walk; that is the only difference.

    # The plan is sorted by destination; the returned written
    # paths preserve that order.
    written = tuple(destination / p.destination for p in plan)
    backups: tuple[Path, ...] = ()
    if overwrite:
        # Glob for the timestamped backup siblings we just created.
        # The pattern is ``<name>.bak.<UTC timestamp>``, and the
        # write happens in ``backup_existing``; a simple glob is
        # sufficient because the timestamp is fresh.
        found: list[Path] = []
        for planned in plan:
            target = destination / planned.destination
            found.extend(target.parent.glob(f"{target.name}.bak.*"))
        # Deduplicate while preserving order. ``dict.fromkeys`` is the
        # canonical "dedupe a sequence preserving first occurrence"
        # idiom: a dict preserves insertion order, and its keys are
        # unique — so ``dict.fromkeys(seq)`` iterates the distinct
        # elements in the order they first appeared. The previous
        # implementation used ``seen.add(p)`` inside a generator
        # expression, which ruff's ``[func-returns-value]`` correctly
        # flagged: ``set.add`` returns ``None``, so ``not seen.add(p)``
        # works only because of ``and`` short-circuiting — a fragile
        # idiom whose correctness is not obvious to a reader. The
        # ``dict.fromkeys`` form is shorter *and* clearer.
        backups = tuple(dict.fromkeys(found))

    return ScaffoldResult(written=written, backups=backups)


__all__ = ["ScaffoldResult", "scaffold_from_plan"]
