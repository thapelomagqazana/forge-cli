"""The GenerationPlan DTO — the contract for dry-run output.

Design choices, each deliberate:

* **Frozen Pydantic model.** Any consumer that mutates a plan is a bug,
  and we want the type system to say so. Frozen models are also hashable,
  which makes them safe to cache and compare in tests.

* **`schema_version` on the envelope.** This is a public contract (see
  ADR-0012): additive evolution only. Consumers that pin to ``1.0.0``
  are safe when we add new fields; removing or renaming a field requires
  a version bump and an ADR amendment.

* **Independent enums, not imports.** ``PlannedFileKind`` is defined
  here rather than imported from ``forge.planning``. The planning module
  is an implementation detail and may rename or restructure its enums;
  the JSON contract must not silently follow. If planning adds a kind,
  we add it here explicitly, as a visible change to the public surface.

* **`files_to_skip` carries the cause.** A skip without the condition
  and reason is useless: the user cannot tell whether the tool honoured
  their blueprint or silently dropped files. Every ``SkippedFileDTO``
  records the condition text and a human-readable reason.

* **Collisions are data, not exceptions.** A dry-run *reports*; an
  execute *fails*. Collisions live in the DTO so the user can inspect
  them, decide between ``--overwrite`` and a different destination, and
  re-run — without ever having mutated anything on the first attempt.

* **All lists are ordered.** ``files_to_create``, ``files_to_skip``,
  and ``collisions`` are sorted by destination; ``warnings`` are sorted
  lexically. Determinism is a feature: JSON output is byte-identical
  across runs for the same inputs, which makes golden tests and CI diffs
  reliable.

Everything in this module is data. There is no I/O, no environment
inspection, and no filesystem access — those all live in the service
that builds a plan (``forge.dryrun.service``).
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class PlannedFileKind(str, Enum):
    """How the destination file will be produced.

    Defined *here* (not imported from ``forge.planning``) so the JSON
    contract has an independent, versioned home. Internal refactors of
    the planner cannot break automation that consumes ``--dry-run`` JSON.

    Values
    ------
    RENDER
        Render the source as a Jinja template with the resolved variables.
    COPY
        Copy the source bytes verbatim (binaries, fixtures, or files that
        opted out of templating by extension).
    """

    RENDER = "render"
    COPY = "copy"


# ---------------------------------------------------------------------------
# Item DTOs
# ---------------------------------------------------------------------------


class PlannedFileDTO(BaseModel):
    """A file the dry-run would create.

    Mirrors the shape of ``forge.planning.PlannedFile`` but with an
    independent enum (see module docstring) and stricter field bounds.
    These bounds are not redundant with planning's — they are the
    contract; planning's are implementation.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: str = Field(
        min_length=1,
        max_length=1024,
        description="Path to the template or literal file, relative to the blueprint root.",
    )
    destination: str = Field(
        min_length=1,
        max_length=1024,
        description="Resolved destination path, relative to the project root.",
    )
    kind: PlannedFileKind = Field(
        description="Whether the source is rendered or copied verbatim.",
    )


class SkippedFileDTO(BaseModel):
    """A file excluded because a conditional group evaluated false.

    The *reason* and *condition* fields exist so a user can verify the
    tool honoured their blueprint. A skip without a cause is
    indistinguishable from a bug — that is the entire point of this DTO.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: str = Field(min_length=1, max_length=1024)
    destination: str = Field(min_length=1, max_length=1024)
    condition: str = Field(
        min_length=1,
        description="The conditional expression, verbatim, as written in the blueprint.",
    )
    reason: str = Field(
        min_length=1,
        description="Human-readable explanation of why the file was skipped.",
    )


class CollisionDTO(BaseModel):
    """A destination that already exists and would be overwritten.

    ``existing_kind`` describes the current entry on disk (``file``,
    ``dir``, ``symlink``, ``other``). It comes from ``lstat``, never
    ``stat`` — a symlink must be reported as a symlink, not as its
    target. The renderer uses this to give the user an actionable
    message ("destination is a symlink") rather than a generic one.

    ``sources`` lists every source file in the plan that would produce
    this destination. Normally one; multiple means the *plan* itself has
    a collision (caught earlier by the mapper), so this list is also a
    diagnostic signal.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    destination: str = Field(min_length=1, max_length=1024)
    existing_kind: str = Field(
        min_length=1,
        description="Kind of entry currently on disk: file, dir, symlink, other, unreadable.",
    )
    sources: list[str] = Field(
        default_factory=list,
        description="Sources in the plan that would produce this destination.",
    )


# ---------------------------------------------------------------------------
# Root plan
# ---------------------------------------------------------------------------


class GenerationPlan(BaseModel):
    """A complete, inspectable description of what ``create`` would do.

    Instances are immutable. Build them with
    ``forge.dryrun.DryRunService.plan()``; do not construct them from
    ad-hoc dictionaries in downstream code — the service guarantees
    ordering, completeness, and the read-only invariant.

    Fields
    ------
    schema_version
        Envelope version. ``1.0.0`` for this revision; additive changes
        keep this value; removals or semantic changes bump it.
    blueprint_id, blueprint_version
        Provenance — the user needs to know what the plan was built from.
    destination
        Absolute path of the target root.
    overwrite
        The ``--overwrite`` flag as it was passed. Recorded in the plan
        so the human renderer can explain collision semantics correctly.
    files_to_create
        Every file the operation would create, sorted by destination.
    files_to_skip
        Every conditional file excluded because its group's condition
        evaluated false, sorted by destination.
    collisions
        Destinations that already exist on disk, sorted by destination.
    warnings
        Non-fatal findings (unresolvable intermediate paths, unreadable
        parents, etc.), sorted lexically. Warnings do not change exit
        semantics; they help users spot environment problems early.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = Field(
        default="1.0.0",
        description="Version of the plan contract.",
    )

    blueprint_id: str = Field(min_length=1, max_length=128)
    blueprint_version: str = Field(min_length=1, max_length=64)
    destination: str = Field(min_length=1, max_length=1024)
    overwrite: bool = Field(default=False)

    files_to_create: list[PlannedFileDTO] = Field(default_factory=list)
    files_to_skip: list[SkippedFileDTO] = Field(default_factory=list)
    collisions: list[CollisionDTO] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    # ------------------------------------------------------------------
    # Convenience accessors (pure; no I/O, no mutation)
    # ------------------------------------------------------------------

    @property
    def has_collisions(self) -> bool:
        """True when at least one destination already exists on disk."""
        return bool(self.collisions)

    @property
    def has_warnings(self) -> bool:
        """True when at least one non-fatal finding was recorded."""
        return bool(self.warnings)

    @property
    def summary(self) -> dict[str, int]:
        """Stable counter map for renderers and dashboards.

        Keys are fixed for v1.0.0. Consumers should treat them as an
        immutable dictionary, not as an extensible one; new counters
        require a schema_version bump.
        """
        return {
            "create": len(self.files_to_create),
            "skip": len(self.files_to_skip),
            "collisions": len(self.collisions),
            "warnings": len(self.warnings),
        }
