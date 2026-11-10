r"""Render a destination template and validate the result.

This module owns exactly one thing: turning a destination *template*
(``src/{{ name }}/main.py``) plus a variable mapping into a resolved,
validated destination string. It composes:

    * ``TemplateRenderer.render_inline`` for the substitution — the
      same sandbox and allowlist as file bodies, so path substitution
      gains no capability that body rendering denies.
    * ``planning.paths._safety_check`` for the safety rules — the
      same rules the loader applies to source paths (ADR-0015).

The safety rules themselves live in ``planning.paths`` rather than
here so that the *loader* can import them without pulling in the
renderer. This module is the only place in the codebase that imports
both the renderer and the safety rule, because it is the only place
that *needs* both: substitution (renderer) and validation (rule). The
loader needs only the rule; the planner needs only ``resolve()``.

Why the rule is *not* defined here
----------------------------------
Putting ``_safety_check`` and ``is_safe_relative`` in this module
created a circular import:

    loading.fs_checks → planning.destinations → rendering
        → loading (façade) → loading.loader → loading.fs_checks

The cycle existed because ``destinations`` imports the renderer, and
the renderer's façade imports ``forge.loading.result``, and
``forge.loading.__init__`` imports the loader, which imports
``fs_checks``. The rule itself has no rendering dependency, so it
belongs in a module that has no rendering dependency. That module is
``planning.paths``.

What this module still owns
---------------------------
* ``resolve`` — the composed substitution + validation operation.
* ``normalise`` — the canonical form used for collision detection
  (see ``collisions.py``). Kept here, not in ``paths``, because it is
  a planner concern (only the planner compares destinations); the
  loader has no reason to normalise and moving it to ``paths`` would
  put planner logic in a module the loader imports.
* Re-exports of ``is_safe_relative`` and ``MAX_PATH_LENGTH`` — for
  callers (tests, future tools) that currently import them from
  ``planning.destinations``. The re-exports are thin; the definitions
  live in ``planning.paths``.
"""

from __future__ import annotations

from collections.abc import Mapping

from forge.planning.errors import Finding
from forge.planning.paths import MAX_PATH_LENGTH, _safety_check, is_safe_relative
from forge.rendering import TemplateRenderer
from forge.rendering.errors import TemplateRenderError

__all__ = ["MAX_PATH_LENGTH", "is_safe_relative", "normalise", "resolve"]


def resolve(
    template: str,
    variables: Mapping[str, object],
) -> tuple[str, Finding | None]:
    """Render and validate a destination template.

    Returns
    -------
    (resolved, None) on success
    ("", finding)    on failure

    Never raises: the planner aggregates findings into one report, so a
    blueprint with three broken destinations surfaces all three at once.

    The empty string is used as the failure sentinel because a valid
    destination can never be empty (that case is itself a finding), so
    callers have no ambiguity to reason about.
    """
    rendered = TemplateRenderer().render_inline(template, variables)
    if not rendered.is_ok():
        return "", _render_error_to_finding(rendered.error)

    # ``.strip()`` is deliberate and narrow: it removes accidental
    # leading or trailing whitespace that a template variable might
    # carry (``--name "  my-service  "``). Interior whitespace is not
    # stripped because a filename with a space is legal on every
    # supported OS; the safety check does not reject spaces in segment
    # bodies.
    candidate = rendered.value.strip()

    finding = _safety_check(candidate)
    if finding is not None:
        return "", finding
    return candidate, None


def normalise(candidate: str) -> str:
    """Return the canonical form used for collision detection.

    Rules
    -----
    * Forward slashes only (POSIX form; Windows accepts them).
    * No empty segments (``a//b`` → ``a/b``).
    * No ``.`` segments (``./a`` → ``a``).
    * No trailing slash.
    * Case is preserved. Collision detection is therefore
      case-sensitive; a case-only collision on a case-insensitive
      filesystem is caught at write time with a clear error from the
      writer, not silently here.

    ``..`` is *not* handled here because ``_safety_check`` rejects it
    before normalisation ever runs; if it slipped through, this
    function would still produce a string, and the writer would catch
    the escape. Two layers, one authoritative message.
    """
    posix = candidate.replace("\\", "/")
    parts = [p for p in posix.split("/") if p and p != "."]
    return "/".join(parts)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _render_error_to_finding(err: TemplateRenderError) -> Finding:
    """Translate a renderer error into a planner finding.

    The message keeps the renderer's wording (users see the same
    terminology across ``forge validate``, ``forge create``, and
    rendering errors) but the ``path`` field is empty because we do not
    yet have a resolved destination to report — the template itself is
    the problem.
    """
    return Finding(
        destination="",
        message=f"could not resolve destination: {err.message}",
        hint=err.hint,
        code="destination.render_failed",
    )
