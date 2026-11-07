"""Verification handlers — the single validation funnel for CLI input.

Both the flag-driven and interactive paths of ``forge create`` call
``validate_blueprint_inputs``. That is the *only* place input rules
live (ADR-0006). Adding a rule means editing one function, not two
call sites — which is exactly why flag validation and prompt
validation cannot drift apart.

What this module is *not* responsible for
-----------------------------------------
This module validates *inputs*: the shape and content of what the
user typed (or what the wizard collected). It does **not** validate
the *filesystem state* that those inputs point at. A destination
that already exists, that the user cannot write to, or that escapes
the permitted root is a filesystem concern — the command's
``_generate`` step owns it, because only ``_generate`` is in a
position to know *what* would be written and whether the user asked
to overwrite it.

That separation is not pedantry. It is what lets a caller
distinguish:

- **"Your input is invalid — retry with different flags."** Exit 1,
  ``UserInputError``. The function in this module raises this.
- **"Your input is fine, but the target is occupied — retry with a
  different ``--output``."** Exit 3, ``FilesystemConflictError``.
  ``_generate`` raises this.

Two failure classes, two exit codes, two layers. A script that
branches on ``$? == 1`` (fix your flags) vs. ``$? == 3`` (fix your
target) gets the right answer without parsing a message.

Because this module does not touch the filesystem, it is a **pure
function** of its arguments. That makes it testable without
``tmp_path``, usable from a future SDK entry point without dragging
``pathlib`` semantics along, and unambiguous about what it does: it
normalizes inputs and refuses the ones that cannot be valid.

Why no blueprint lookup here
----------------------------
``validate_blueprint_inputs`` receives ``blueprint_id`` as already
resolved by the caller. Looking it up again (via
``registry.get_blueprint``) would create two sources of truth — the
caller already did the lookup to drive its own behavior, and a
second lookup here could theoretically disagree (e.g., if a
blueprint were added or removed between the two calls). Trusting
the caller's resolution is the right contract: this function
validates the *inputs*, not the *blueprint registry*.

The empty-string guard on ``blueprint_id`` is a defensive check for
direct callers (future SDK users, tests). It is not reachable from
``create.py``, which resolves the blueprint first and would have
raised ``BlueprintError`` (exit 2) before calling here.

Why variables are passed through unvalidated
--------------------------------------------
Per-variable rules belong to the blueprint, not to this function.
The blueprint declares which variables exist, their types, their
defaults, and their validators; a future "variable resolver" task
will apply those rules. Until that task lands, this function accepts
``variables`` as-is and returns them unchanged.

Passing them through (rather than refusing them outright) is the
forward-compatible choice: when the resolver lands, it will feed
validated variables into this function, and this function will
continue to do what it does today — normalize the shape, not
reinterpret the contents.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from forge.core.exceptions import UserInputError


# -----------------------------------------------------------------------------
# Result type
# -----------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ValidatedInputs:
    """The validated, mode-agnostic result of ``validate_blueprint_inputs``.

    Frozen and slotted so a caught result cannot be mutated by a
    downstream handler — the wizard's answers are a snapshot, not a
    scratchpad. Slots also shrink the per-instance cost, which matters
    for a type that is created once per command and passed to a
    generation pipeline that may spawn workers.

    Attributes:
        blueprint_id: The blueprint's canonical ID, exactly as passed
            by the caller. Not normalized (case, whitespace) — IDs
            are opaque identifiers and the registry owns their format.
        name: The project name, exactly as passed by the caller. The
            caller is responsible for whitespace normalization
            (``create.py`` strips the wizard's answer); doing it here
            would silently change what the user typed, which is a
            surprise, not a fix.
        destination: The destination directory, normalized to an
            absolute ``str``. ``str`` rather than ``Path`` so the
            result is JSON-serializable without an adapter — a
            future ``--json`` output can embed this field verbatim.
        variables: The blueprint-specific variables, passed through
            unchanged. ``dict[str, Any]`` because the eventual
            variable resolver may produce ints, bools, or lists;
            the funnel does not inspect them.
    """

    blueprint_id: str
    name: str
    destination: str
    variables: dict[str, Any] = field(default_factory=dict)


# -----------------------------------------------------------------------------
# Validation funnel
# -----------------------------------------------------------------------------
def validate_blueprint_inputs(
    *,
    blueprint_id: str,
    name: str,
    destination: str | Path | None,
    variables: dict[str, Any] | None = None,
) -> ValidatedInputs:
    """Apply input rules to collected values, mode-agnostically.

    Called by both the flag-driven and interactive paths of
    ``forge create``. This is the single funnel: adding a rule means
    editing this function, not two call sites.

    The function is **pure** — it does not read the filesystem, does
    not consult the blueprint registry, and does not render output.
    That is deliberate: purity is what lets it be tested without a
    ``tmp_path``, used from a future SDK, and reasoned about in
    isolation from the CLI.

    Args:
        blueprint_id: The blueprint's canonical ID. Already resolved
            by the caller (``create.py::_resolve_inputs``), so this
            function only guards against direct-caller mistakes.
        name: The project name. Must be non-empty, must not contain
            characters that cannot appear in a Python module name or
            a directory name (letters, digits, hyphens, underscores).
        destination: The resolved destination directory. ``None``
            means "not yet chosen" and falls back to ``cwd / name``.
            An absolute path is normalized to an absolute path; a
            relative path is resolved against the current working
            directory. The result is always an absolute ``str``.
        variables: Blueprint-specific variables. Passed through
            unchanged; per-variable rules are the blueprint's
            concern, not this function's.

    Returns:
        A ``ValidatedInputs`` with all fields normalized.

    Raises:
        UserInputError: If ``blueprint_id`` or ``name`` is empty, or
            if ``name`` contains characters that cannot appear in a
            directory name / Python module. (Exit code 1.)
    """
    if not blueprint_id:
        raise UserInputError(
            "Blueprint ID is required.",
            hint="Pass a blueprint ID (e.g., python.fastapi.api).",
        )

    if not name:
        raise UserInputError(
            "Project name is required.",
            hint="Pass --name <project-name>.",
        )

    # Same rule the interactive prompt's inline validator applies, so
    # the two cannot disagree about what constitutes a legal name. If
    # this check is ever changed, the inline validator in
    # ``create.py::_project_name_validator`` must change in lockstep —
    # the test suite asserts the subset relationship, but a change
    # that expands the rule here without expanding it there would
    # create a wizard that accepts what the funnel rejects.
    if not name.replace("-", "").replace("_", "").isalnum():
        raise UserInputError(
            f"Project name {name!r} contains invalid characters.",
            hint="Use only letters, digits, hyphens, and underscores.",
        )

    # Resolve the destination to an absolute path. ``expanduser``
    # handles ``~``; ``resolve`` normalizes ``..`` and symlinks.
    # Both are no-ops for an already-absolute, already-clean path,
    # so the cost on the common case is a stat call — cheap, and
    # worth the guarantee that the returned path is canonical.
    resolved_destination = (
        Path(destination).expanduser().resolve()
        if destination is not None
        else (Path.cwd() / name).expanduser().resolve()
    )

    return ValidatedInputs(
        blueprint_id=blueprint_id,
        name=name,
        destination=str(resolved_destination),
        variables=dict(variables or {}),
    )


# -----------------------------------------------------------------------------
# Public re-exports
# -----------------------------------------------------------------------------
# The public surface is intentionally small: one result type, one
# function. Adding to this list is a deliberate act — it means a new
# consumer (a new handler, a new entry point) needs the name — and
# should be paired with tests and a mention in the module docstring.
__all__ = ["ValidatedInputs", "validate_blueprint_inputs"]
