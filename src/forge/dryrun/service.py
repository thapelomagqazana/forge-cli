"""DryRunService — build the generation plan; never touch the destination.

Composition
-----------
The service is an *orchestrator*, not an implementer. It composes three
already-validated, already-tested components:

    ConditionalEvaluator   →  which conditional groups survive
    FileMapper             →  which files would be created (with kinds)
    detect_collisions      →  which destinations already exist on disk

Plus two read-only safety checks from ``forge.safety`` (path resolution
and destination validation), whose *findings* become warnings in the
plan — the dry-run reports environment problems so the user can fix them
before running create for real.

Guarantees
----------
1. **No filesystem mutations.** The service reads metadata (``lstat``,
   ``access``) and produces a DTO. It never opens a file for writing,
   never creates a directory, never renames anything. This is verified
   by a filesystem fingerprint test (see ``tests/unit/dryrun/test_no_writes.py``)
   *and* structurally enforced: any write attempt would require a
   ``StagingArea``, and the service is documented (and reviewed) to
   never construct one.

2. **Total.** Every outcome is either a ``GenerationPlan`` or a
   ``DryRunError``. No exceptions escape for user-actionable problems.

3. **Deterministic.** Same blueprint + same variables + same destination
   filesystem ⇒ byte-identical plan. All lists are sorted before the
   DTO is constructed.

4. **Collisions are data.** The plan lists them; it does not raise. A
   separate exit code (``DRY_RUN_COLLISIONS``) lets CI gate on them
   without writing anything.

Design notes
------------
* **Early-return on condition errors.** If conditions cannot be
  evaluated, we cannot know which files are "would-be created" vs.
  "would-be skipped" — so the plan is meaningless. Return the condition
  findings immediately; a user must fix the blueprint before a plan is
  useful.

* **Early-return on mapping errors.** A mapping error means at least one
  destination is unresolvable or unsafe. Continuing would produce a plan
  with silent gaps. Fail closed.

* **Warnings, not errors, for safety findings.** A destination that's
  currently unwritable is a *warning*: by the time the user runs create
  for real, they may have fixed permissions. A dry-run should inform,
  not block.

* **Skip derivation is independent of evaluation.** We do not trust the
  evaluator to return *only* surviving groups; we derive "skipped" by
  set-differencing against every conditional group in the blueprint.
  This makes the skip list exhaustive and correct even if the evaluator
  ever changes its return shape.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from forge.conditions import ConditionalEvaluator
from forge.dryrun.errors import DryRunError
from forge.dryrun.errors import Finding as DryRunFinding
from forge.dryrun.plan import (
    CollisionDTO,
    GenerationPlan,
    PlannedFileDTO,
    PlannedFileKind,
    SkippedFileDTO,
)
from forge.loading.result import Err
from forge.models import Blueprint
from forge.planning import FileMapper, PlannedFile
from forge.safety import detect_collisions, safe_resolve, validate_destination

# Condition-derived fields we expect the evaluator to hand back. We
# accept either a parsed-expression tuple or the raw condition string
# depending on the evaluator implementation; the service only needs the
# *set of surviving sources* and the *condition text for reporting*, so
# we coerce here and keep the rest of the module decoupled.
_SurvivorGroup = tuple[Any, list[Any]]


class DryRunService:
    """Compose the generation pipeline into a ``GenerationPlan`` DTO."""

    def plan(
        self,
        blueprint: Blueprint,
        variables: Mapping[str, object],
        destination: Path | str,
        *,
        overwrite: bool = False,
    ) -> GenerationPlan | DryRunError:
        """Build a plan describing what ``create`` would do.

        Returns either a complete ``GenerationPlan`` or a ``DryRunError``
        with structured findings. Never raises for user-actionable
        problems; unexpected exceptions are bugs and propagate.
        """
        destination = Path(destination).expanduser()

        # ------------------------------------------------------------------
        # Phase 1 — conditions
        # ------------------------------------------------------------------
        # If we cannot determine which conditional groups survive, we
        # cannot build a meaningful plan. Return early with all findings.
        cond_result = ConditionalEvaluator().evaluate(blueprint, variables)
        if isinstance(cond_result, Err):
            return DryRunError(
                findings=tuple(
                    DryRunFinding(
                        path=f.condition,
                        message=f.message,
                        hint=f.hint,
                        code=f.code,
                    )
                    for f in cond_result.error.findings
                ),
                operation="dry-run:conditions",
            )
        survivors: list[_SurvivorGroup] = list(cond_result.value)

        # ------------------------------------------------------------------
        # Phase 2 — file mapping (unconditional + surviving conditional)
        # ------------------------------------------------------------------
        map_result = FileMapper().map(
            blueprint,
            variables,
            conditional_groups=survivors,
        )
        if isinstance(map_result, Err):
            return DryRunError(
                findings=tuple(
                    DryRunFinding(
                        path=_finding_path(f),
                        message=f.message,
                        hint=f.hint,
                        code=f.code,
                    )
                    for f in map_result.error.findings
                ),
                operation="dry-run:mapping",
            )
        planned: list[PlannedFile] = list(map_result.value)

        # ------------------------------------------------------------------
        # Phase 3 — skipped files (derived, not trusted to the evaluator)
        # ------------------------------------------------------------------
        skipped = _derive_skips(blueprint, survivors)

        # ------------------------------------------------------------------
        # Phase 4 — collisions (reported as data, never raised)
        # ------------------------------------------------------------------
        collisions, collision_warnings = _detect_collisions_for_plan(
            planned,
            destination,
            overwrite=overwrite,
        )

        # ------------------------------------------------------------------
        # Phase 5 — per-file safety findings (warnings only)
        # ------------------------------------------------------------------
        safety_warnings = _collect_safety_warnings(planned, destination, overwrite=overwrite)

        # ------------------------------------------------------------------
        # Phase 6 — build the DTO with deterministic ordering
        # ------------------------------------------------------------------
        return GenerationPlan(
            blueprint_id=blueprint.id,
            blueprint_version=blueprint.version,
            destination=str(destination),
            overwrite=overwrite,
            files_to_create=sorted(
                (
                    PlannedFileDTO(
                        source=p.source,
                        destination=p.destination,
                        kind=PlannedFileKind(p.kind.value),
                    )
                    for p in planned
                ),
                key=lambda d: d.destination,
            ),
            files_to_skip=sorted(skipped, key=lambda d: d.destination),
            collisions=sorted(collisions, key=lambda d: d.destination),
            warnings=sorted(collision_warnings + safety_warnings),
        )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _finding_path(f: Any) -> str:
    """Return the best human-facing path from a mapping finding.

    Mapping findings vary in shape (destination errors have empty dest,
    source errors list sources). We prefer the destination, fall back to
    the first source, then to a placeholder — the user always sees
    *something* they can grep for.

    The finding is typed ``Any`` at the parameter because ``FileMapper``
    returns findings with a *heterogeneous* shape — some carry a
    ``destination`` attribute, some carry ``sources``, some carry both.
    Widening the parameter to a ``Protocol`` would be more precise but
    would couple this module (which only needs to *render* findings) to
    the planning module's exact field names, defeating the "the dry-run
    DTO has its own shape" contract the module docstring establishes.

    The ``str()`` coercion on each return is load-bearing for mypy:
    ``getattr(f, "...", "")`` returns ``Any`` (mypy cannot prove what
    shape an ``Any``-typed attribute access produces), and a function
    annotated ``-> str`` cannot return ``Any``. Coercing at the boundary
    is both a *type* assertion ("the field really is a string or
    string-like") and a *runtime* guarantee (a bogus field — say, a
    ``Path`` — is still renderable in the plan).
    """
    destination = getattr(f, "destination", "")
    if destination:
        return str(destination)
    sources = getattr(f, "sources", ())
    if sources:
        return str(sources[0])
    return "<plan>"


def _derive_skips(
    blueprint: Blueprint,
    survivors: list[_SurvivorGroup],
) -> list[SkippedFileDTO]:
    """Return every conditional file *not* in the surviving set.

    Derived by set difference, not by trusting the evaluator to return
    "the excluded ones." The evaluator's contract is "return survivors";
    the service's contract is "report every skip." Those are different
    obligations and live in different modules for that reason.
    """
    surviving_sources: set[str] = set()
    surviving_conditions: dict[str, str] = {}
    for expr, files in survivors:
        condition_text = _condition_text(expr)
        for entry in files:
            surviving_sources.add(entry.source)
            surviving_conditions[entry.source] = condition_text

    skips: list[SkippedFileDTO] = []
    for group in getattr(blueprint, "conditional_files", []) or []:
        for entry in group.files:
            if entry.source in surviving_sources:
                continue
            skips.append(
                SkippedFileDTO(
                    source=entry.source,
                    destination=entry.destination,
                    condition=group.condition,
                    reason="condition evaluated false",
                )
            )
    return skips


def _condition_text(expr: Any) -> str:
    """Recover a printable condition string from the evaluator's AST.

    The evaluator returns parsed expressions; we reconstruct a readable
    form rather than importing the AST types (which would couple the
    dry-run DTO to the conditions module's internals).
    """
    var = getattr(expr, "variable", "<expr>")
    kind = getattr(expr, "kind", "equals")
    literal = getattr(expr, "literal", "?")
    op = "!=" if kind == "not_equals" else "=="
    if isinstance(literal, str):
        return f"{var} {op} '{literal}'"
    if isinstance(literal, bool):
        return f"{var} {op} {str(literal).lower()}"
    return f"{var} {op} {literal}"


def _detect_collisions_for_plan(
    planned: list[PlannedFile],
    destination: Path,
    *,
    overwrite: bool,
) -> tuple[list[CollisionDTO], list[str]]:
    """Detect collisions and classify them for the plan.

    Returns ``(collisions, warnings)``. Collisions are the actionable
    core of the plan; warnings are non-fatal inspection failures (e.g.
    a destination directory we cannot stat) that we do not want to
    silently drop.
    """
    if overwrite:
        # `detect_collisions` with overwrite=True does not flag existing
        # files, so the collision list is empty by design. We still run
        # it to surface case-insensitive duplicates in the *plan*.
        pass

    findings = detect_collisions(planned, destination, overwrite=overwrite)

    collisions: list[CollisionDTO] = []
    warnings: list[str] = []
    for f in findings:
        if f.code == "collision.exists":
            collisions.append(
                CollisionDTO(
                    destination=f.path,
                    existing_kind=_existing_kind(destination / f.path),
                    sources=[p.source for p in planned if p.destination == f.path],
                )
            )
        elif f.code == "plan.case_insensitive_duplicate":
            # A plan-level duplicate, not a disk collision. Report it as
            # a warning — the *mapper* would have caught this first for
            # exact-path collisions; case-only duplicates need the
            # filesystem's help to become real problems, so we warn.
            warnings.append(f"{f.path}: {f.message}")
        else:
            # inspect_failed, permission issues, etc.
            warnings.append(f"{f.path}: {f.message}")

    return collisions, warnings


def _existing_kind(path: Path) -> str:
    """Describe what is currently on disk at ``path`` (for collision DTO).

    Uses ``lstat`` so a symlink is reported as ``symlink`` — not as its
    target. ``stat`` would follow the link and lie about the thing the
    user actually has to deal with.
    """
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "unreadable"

    mode = st.st_mode
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISDIR(mode):
        return "dir"
    if stat.S_ISREG(mode):
        return "file"
    return "other"


def _collect_safety_warnings(
    planned: list[PlannedFile],
    destination: Path,
    *,
    overwrite: bool,
) -> list[str]:
    """Run path-safety and destination checks; collect *warnings*.

    A dry-run must not fail because a directory is currently
    unwritable — the user may fix that between the dry-run and the real
    create. But they must *know* about it, so every finding becomes a
    warning.

    Why the ``resolved is None`` guard is a real ``continue`` (not an
    ``assert``):
        ``safe_resolve`` returns ``(Path, None)`` on success and
        ``(None, Finding)`` on failure. The ``finding is not None``
        branch above handles the failure case and ``continue``s, so
        after that branch ``resolved`` must be the non-``None`` half.
        But mypy's flow analysis cannot narrow a union-of-tuples by
        element (the return type is ``tuple[Path, None] | tuple[None,
        Finding]``, not ``tuple[Path | None, Finding | None]``), so the
        explicit check is *both* a type narrowing for the subsequent
        ``validate_destination(resolved, ...)`` call *and* a runtime
        contract check. If ``safe_resolve`` ever returned
        ``(None, None)`` — a contract violation — this branch records
        that as a warning rather than passing ``None`` into
        ``validate_destination`` (which would fail with a confusing
        ``AttributeError`` deep inside the safety layer).

        Warning vs. raise: an unreachable ``(None, None)`` return would
        be a Forge bug, not a user condition. But a dry-run's job is to
        *report* problems, not to abort on them, so recording it as a
        warning keeps the plan useful while still surfacing the
        anomaly. The warning text names the caller's source path so the
        user can grep for it.
    """
    warnings: list[str] = []
    for p in planned:
        resolved, finding = safe_resolve(p.destination, destination)
        if finding is not None:
            warnings.append(f"{p.destination}: {finding.message} — {finding.hint}")
            continue
        if resolved is None:
            # Should not happen: ``safe_resolve``'s contract is
            # "exactly one of the tuple's elements is non-``None``."
            # If it does, record a warning rather than crash — the
            # dry-run's purpose is to report, and a bug in the safety
            # layer should not prevent the user from seeing the rest
            # of the plan.
            warnings.append(
                f"{p.destination}: internal error — safe_resolve "
                f"returned (None, None) for this destination; "
                f"please report this at "
                f"https://github.com/thapelomagqazana/forge-cli/issues"
            )
            continue
        for v in validate_destination(resolved, overwrite=overwrite):
            warnings.append(f"{p.destination}: {v.message} — {v.hint}")
    return warnings
