"""Human renderer for the dry-run plan.

Pure function: ``GenerationPlan -> str``. No I/O, no ANSI, no clock, no
environment inspection. Colouring and terminal detection are the caller's
job (see ``forge.cli.ui.emit``), which means:

    * snapshot tests can compare the rendered string byte-for-byte,
    * the same renderer works in a TTY, a pipe, a file, a test, and a
      future web UI that wraps the text in a ``<pre>``,
    * colour never leaks into machine paths (we had a bug class where
      a stray ANSI reset corrupted a JSON stream — this design makes it
      impossible by construction).

Layout principles
-----------------
1. **Summary first.** Every renderer that puts the interesting bits on
   line 40 wastes the reader's attention. The reader wants three
   numbers immediately: how many files, how many skips, how many
   collisions. Everything else is detail.

2. **Grouped by consequence, not by source.** Users care about
   "what will be created" and "what already exists." They do not care
   that the collision came from a conditional group vs. a static file.
   The renderer reflects consequence; the DTO retains provenance for
   whoever needs it.

3. **Skip reasons are inlined, not referenced.** A line that says
   ``- Dockerfile`` is useless. ``- Dockerfile  (skipped: include_docker == true evaluated false)``
   tells the user exactly what to change to include it. This is the
   single highest-leverage detail in the entire output.

4. **Trailing hint is conditional.** "Re-run without ``--dry-run`` to
   apply" appears **only** when there is nothing to flag. A user with
   collisions should not be told to apply — they should first fix the
   plan. The hint is a promise the renderer keeps.

5. **No trailing blank line.** Renderers in this codebase end with
   exactly one ``\\n`` (via ``_finalise``). Two blank lines at the end
   of a multi-line string break every "paste the plan into a ticket"
   workflow.

What this renderer does **not** do
----------------------------------
* It does not sort. The DTO's lists are already sorted by the service
  (see ``DryRunService.plan``); re-sorting here would risk drift between
  human and JSON output.
* It does not abbreviate paths. Users copy destinations into ``--destination``
  args and shell commands; truncation is a UX tax, not a feature.
* It does not localise. English-only for v1; a future ADR can
  introduce a translation layer without touching the DTO.
"""

from __future__ import annotations

from forge.dryrun.plan import (
    CollisionDTO,
    GenerationPlan,
    PlannedFileDTO,
    PlannedFileKind,
    SkippedFileDTO,
)

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def render_human(plan: GenerationPlan) -> str:
    """Render ``plan`` as a multi-line human-readable report.

    Deterministic: the same plan always produces the same string. Empty
    sections are omitted (not rendered as "Files to create: (none)") so
    the output stays dense when there is little to report — which is
    most of the time.
    """
    lines: list[str] = []

    lines.extend(_header(plan))
    lines.extend(_summary(plan))
    lines.extend(_files_to_create(plan.files_to_create))
    lines.extend(_files_to_skip(plan.files_to_skip))
    lines.extend(_collisions(plan.collisions))
    lines.extend(_warnings(plan.warnings))
    lines.extend(_closing_hint(plan))

    return _finalise(lines)


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------


def _header(plan: GenerationPlan) -> list[str]:
    """Title block: provenance and destination.

    Includes the blueprint *version* because reproducibility matters —
    if a user pastes a plan into a bug report, we need to know exactly
    which blueprint revision produced it.
    """
    mode = "overwrite (backups will be created)" if plan.overwrite else "no-overwrite"
    return [
        f"Dry-run: {plan.blueprint_id} v{plan.blueprint_version}",
        f"Destination: {plan.destination}",
        f"Mode: {mode}",
        "",
    ]


def _summary(plan: GenerationPlan) -> list[str]:
    """One line with all four counters. The reader's orientation."""
    s = plan.summary
    return [
        f"Would create {s['create']} file(s), "
        f"skip {s['skip']}, "
        f"find {s['collisions']} collision(s), "
        f"{s['warnings']} warning(s).",
        "",
    ]


def _files_to_create(files: list[PlannedFileDTO]) -> list[str]:
    """One line per file, annotated with the render kind and its source.

    ``+ destination  (kind; from source)`` is the format. The ``+``
    signals "this is added," symmetric with the ``-`` used for skips
    and the ``!`` used for collisions — a visual language a reader
    learns on the first pass.
    """
    if not files:
        return []
    lines = ["Files to create:"]
    for f in files:
        tag = "render" if f.kind is PlannedFileKind.RENDER else "copy"
        lines.append(f"  + {f.destination}  ({tag}; from {f.source})")
    lines.append("")
    return lines


def _files_to_skip(files: list[SkippedFileDTO]) -> list[str]:
    """Skips include the condition *and* the reason on the same line.

    Wrapping the reason onto a second line would halve the density; the
    reason is the whole point, so it is inlined. A user reading
    ``(condition: include_docker == true)` immediately knows which
    variable to flip.
    """
    if not files:
        return []
    lines = ["Files to skip (conditional):"]
    for f in files:
        lines.append(f"  - {f.destination}")
        lines.append(f"      condition: {f.condition}")
        lines.append(f"      reason: {f.reason}")
    lines.append("")
    return lines


def _collisions(collisions: list[CollisionDTO]) -> list[str]:
    """Collisions get their own block, with existing kind and sources.

    ``existing_kind`` is the single most useful field — a ``symlink``
    collision needs the user to *remove* it, not ``--overwrite`` (the
    safety layer refuses symlinks by default). Surfacing the kind
    shortcuts the "why can't I overwrite this?" support question.
    """
    if not collisions:
        return []
    lines = ["Collisions (destination already exists):"]
    for c in collisions:
        lines.append(f"  ! {c.destination}  (existing: {c.existing_kind})")
        if c.sources:
            lines.append(f"      would be produced by: {', '.join(c.sources)}")
    lines.append("")
    return lines


def _warnings(warnings: list[str]) -> list[str]:
    """Warnings are pre-formatted by the service; render verbatim.

    The service builds warning strings like
    ``"pyproject.toml: destination already exists — hint: …"``, so the
    renderer's job is indentation and grouping, not message assembly.
    That separation is what lets the JSON renderer emit the same strings
    in a ``warnings[]`` array without reimplementing the formatting.
    """
    if not warnings:
        return []
    lines = ["Warnings:"]
    for w in warnings:
        lines.append(f"  ? {w}")
    lines.append("")
    return lines


def _closing_hint(plan: GenerationPlan) -> list[str]:
    """The final line, chosen based on plan state.

    * No collisions, no warnings → "re-run without --dry-run."
    * Collisions present → "use --overwrite or choose a different destination."
    * Warnings only → "review warnings before re-running."

    This is the only piece of advice the renderer gives. It mirrors what
    the user *can do next*, not what the tool happens to check.
    """
    if plan.has_collisions:
        return [
            "Collisions detected. Re-run with --overwrite to replace "
            "existing files (backups will be created), or choose a "
            "different --destination.",
        ]
    if plan.has_warnings:
        return [
            "Review the warnings above before re-running without --dry-run.",
        ]
    return [
        "No collisions, no warnings. Re-run without --dry-run to apply.",
    ]


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _finalise(lines: list[str]) -> str:
    """Collapse blank-line runs and append exactly one trailing newline.

    Section builders append their own blank line for readability; when
    the last section is the closing hint, those trailing blanks would
    produce a double newline at the end of the report. This normalises
    the output so it *always* ends with exactly one ``\\n`` — a property
    callers can rely on without stripping.
    """
    # Drop trailing blanks, then join with newlines and add one final \n.
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines) + "\n"
