"""The planning output DTO — the contract between planner and writer.

Why this file exists
--------------------
Between "the blueprint says these files should exist" and "these files
exist on disk" lies a **plan**: a concrete, ordered, self-describing
manifest of what the writer is about to do. That plan is the only thing
downstream code consumes, and it is what makes generation auditable,
dry-runnable, and testable without touching the filesystem.

`PlannedFile` is that manifest's unit. It describes *intent*, not
*content*:

    source       — where the bytes come from (relative to blueprint root)
    destination  — where the bytes go (already substituted, already safe)
    kind         — how to produce the bytes (render as template, or copy)

File bodies are the writer's concern. The planner never opens a file for
reading or writing — its output is data.

Why a Pydantic model and not a plain dataclass
----------------------------------------------
1. **Consistency with the rest of the codebase.** Every other DTO
   (`ListOutputItem`, `ValidateError`, `DiscoveredBlueprint`, …) is a
   Pydantic model. Diverging here would force every consumer that
   already knows Pydantic idioms to special-case this type.
2. **Validation at construction.** `destination` is non-empty,
   `source` is non-empty, `kind` is one of a fixed set. If a bug in the
   planner ever produced an invalid `PlannedFile`, the model rejects it
   immediately rather than letting a corrupt plan reach the writer.
3. **`extra="forbid"`** — a typo in a field name (`destinaton=...`)
   becomes a loud error at construction, not a silently dropped field
   that surfaces as a write to the wrong path.
4. **`frozen=True`** — planned files are the *input* to the writer. The
   writer must not mutate them; any bug that tries will surface as a
   `ValidationError` at that exact line, not as a mystery 20 frames
   later.

Why a `str` enum for `kind` and not a Python literal or class hierarchy
----------------------------------------------------------------------
A `str` enum serialises to the JSON/`--dry-run` output without a custom
encoder (`{"source": …, "destination": …, "kind": "render"}`), and it
matches the pattern used elsewhere (`BlueprintStatus`, `PlannedFileKind`
is used identically). A class hierarchy (`RenderFile`, `CopyFile`) would
double the type surface for two cases that differ only in a single
behaviour flag; the writer already branches on `kind`, so a sealed
enum is the simplest correct choice.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

# Bound is generous for realistic paths but tight enough to catch
# template mistakes (a substitution that duplicates a directory a
# hundred times) before they reach the writer. Mirrors the constant in
# `destinations.py` where the safety check applies the same limit;
# keeping the number in the field constraint means a hand-constructed
# PlannedFile (in tests, in future tooling) cannot bypass it.
_MAX_PATH = 1024


class PlannedFileKind(str, Enum):
    """How the writer should produce the destination.

    Values are stable strings (not integers) so they survive
    serialisation round-trips without an enum-aware decoder, and so a
    `--json` consumer can read them without knowing the Python enum
    exists.
    """

    RENDER = "render"
    """Render the source as a Jinja2 template, passing the resolved
    variable set. Missing variables are errors (StrictUndefined)."""

    COPY = "copy"
    """Copy the source bytes verbatim. Used for binaries and any file
    the blueprint marks as literal."""


class PlannedFile(BaseModel):
    """One row in the generation manifest.

    Instances are immutable. Downstream code (dry-run renderers, the
    writer, future `forge plan` subcommands) consumes them; nothing
    downstream constructs one — only `FileMapper` does, and only after
    the destination and source have both been validated.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: str = Field(
        min_length=1,
        max_length=_MAX_PATH,
        description=(
            "Path to the source file, relative to the blueprint directory. "
            "Never absolute; the planner rejects that before this model is "
            "constructed."
        ),
    )
    destination: str = Field(
        min_length=1,
        max_length=_MAX_PATH,
        description=(
            "Resolved, validated, relative destination path inside the "
            "target project. Template substitution has already happened; "
            "the planner rejects traversal, absolute paths, and empty "
            "segments before this model is constructed."
        ),
    )
    kind: PlannedFileKind = Field(
        description=(
            "Whether the writer should render the source as a template "
            "(RENDER) or copy it verbatim (COPY)."
        ),
    )

    # ---------------------------------------------------------------------
    # Convenience
    # ---------------------------------------------------------------------

    @property
    def is_render(self) -> bool:
        """True when the writer must render, not copy.

        Kept as a property (rather than a method or a `kind == …` check
        at every call site) because the writer branches on this dozens
        of times in a loop and a property reads as English:

            if planned.is_render:
                ...
        """
        return self.kind is PlannedFileKind.RENDER

    @property
    def is_copy(self) -> bool:
        """True when the writer must copy, not render.

        Symmetry with `is_render` matters: negating `is_render` at call
        sites would be a one-character typo (`if not planned.is_render`)
        away from silently routing a binary through the template engine.
        An explicit `is_copy` makes the intent legible and the branch
        auditable in review.
        """
        return self.kind is PlannedFileKind.COPY
