"""The path-safety rule — shared by the loader and the planner.

Extracted from ``planning.destinations`` so that the *rule* can be
imported by the loader without dragging in the renderer (which
``destinations.resolve`` requires). The rule has no rendering
dependency; putting it in the same module as ``resolve`` created a
circular import that the loader could never satisfy:

    loading.fs_checks → planning.destinations → rendering
        → loading (façade) → loading.loader → loading.fs_checks

This module depends only on ``forge.planning.errors`` (a pure
dataclass module) and, transitively, on nothing else in the
project. That is the property the loader needs.
"""

from __future__ import annotations

from pathlib import PurePosixPath

from forge.planning.errors import Finding

# Bound chosen to stay well under every common OS path limit and to
# catch template mistakes (a substitution that duplicates a directory a
# hundred times) early and with a clear message. Kept here (not in
# ``destinations``) so the loader and the planner agree on the limit by
# construction, not by convention.
MAX_PATH_LENGTH = 400


def is_safe_relative(candidate: str) -> bool:
    """Boolean form of the safety rule, for callers that only need yes/no.

    The loader uses this for source paths; the planner uses
    ``destinations.resolve()`` for destinations (which calls
    ``_safety_check`` below). Both paths share this single definition
    of "safe" — ADR-0015's "one safety net".
    """
    return _safety_check(candidate) is None


def _safety_check(candidate: str) -> Finding | None:
    """The single definition of "is this path safe to write?".

    Returns ``None`` on success, or a ``Finding`` describing the first
    problem. Never raises.

    Rules, in check order:

        1. **Empty** — a resolved-empty path means a variable expanded
           to nothing; the fix is at the variable, not the path.
        2. **Illegal characters** — NUL byte, newline, carriage return.
           These break the writer's I/O at a level no hint can recover
           from; catching them here keeps them from becoming opaque
           ``OSError``s downstream.
        3. **Length** — bounded by ``MAX_PATH_LENGTH``, well under every
           common OS path limit.
        4. **Absolute** — leading ``/``, ``\\``, or a drive letter.
           Destinations are always relative to the target root.
        5. **Empty segment** — doubled slashes, or a variable that
           expanded to nothing between two slashes.
        6. **Traversal** — ``..`` or ``.`` segments. Rejected
           regardless of position; a path that *contains* traversal is
           not a destination we want to resolve, because the resolved
           path would not be under the target root.
        7. **Pure-syntax validity** — ``PurePosixPath`` sanity, which
           catches a small set of edge cases the explicit rules miss
           without ever touching the filesystem.
    """
    if not candidate:
        return Finding(
            destination="",
            message="destination resolved to an empty string",
            hint=(
                "A variable in the destination expanded to an empty value. "
                "Set a non-empty value, or give the variable a default."
            ),
            code="destination.empty",
        )

    if "\x00" in candidate or "\n" in candidate or "\r" in candidate:
        return Finding(
            destination=candidate,
            message="destination contains a NUL byte or newline",
            hint="Remove the offending character from the variable's value.",
            code="destination.illegal_chars",
        )

    if len(candidate) > MAX_PATH_LENGTH:
        return Finding(
            destination=candidate,
            message=f"destination exceeds {MAX_PATH_LENGTH} characters",
            hint="Shorten the destination template, or one of its variable values.",
            code="destination.too_long",
        )

    if candidate.startswith(("/", "\\")):
        return Finding(
            destination=candidate,
            message="destination must be relative, not absolute",
            hint="Destinations are written inside the target directory; use a relative path.",
            code="destination.absolute",
        )
    if len(candidate) > 1 and candidate[1] == ":":
        return Finding(
            destination=candidate,
            message="destination must be relative, not absolute",
            hint="Remove the drive letter; destinations are inside the target directory.",
            code="destination.absolute",
        )

    parts = candidate.replace("\\", "/").split("/")

    if any(part == "" for part in parts):
        return Finding(
            destination=candidate,
            message="destination contains an empty path segment",
            hint=(
                "Check for doubled slashes or a variable that resolved to "
                "nothing between two slashes."
            ),
            code="destination.empty_segment",
        )

    if any(part in {"..", "."} for part in parts):
        return Finding(
            destination=candidate,
            message="destination may not contain '..' or '.' segments",
            hint="Use a plain relative path inside the target directory.",
            code="destination.traversal",
        )

    try:
        PurePosixPath(*parts)
    except ValueError:
        return Finding(
            destination=candidate,
            message="destination is not a valid path",
            hint="Simplify the path; avoid unusual characters.",
            code="destination.invalid",
        )

    return None
