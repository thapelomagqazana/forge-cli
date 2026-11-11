"""Public façade for the filesystem safety layer.

The module boundary is the enforcement
--------------------------------------
Every filesystem-touching caller in the generation pipeline imports
from *here*, and only here. The rule is simple and enforceable in
review:

    from forge.safety import (          # ✅ always this
        CollisionError, Finding, SafetyError,
        StagingArea, backup_existing,
        detect_collisions, safe_resolve, validate_destination,
    )

    from forge.safety.paths import safe_resolve       # ❌ never this
    from forge.safety._policy import DEFAULT_POLICY   # ❌ never this

Why? Because:

    * the façade is a *compatibility surface* we control — we can
      rename `paths.safe_resolve` to `paths.resolve_safe` without
      touching a single caller,
    * the internal modules (`paths`, `destinations`, `collisions`,
      `staging`, `overwrite`) are free to evolve, split, or merge as
      long as the façade's names keep working,
    * a caller that imports from `forge.safety` gets a *coherent*
      mental model — six operations and their types — instead of a
      tree of implementation modules,
    * a future extraction of `forge.safety` into its own package (for
      reuse in other tools) is a move of the directory and a rewrite
      of one import line, not a search-and-replace across the
      codebase.

The six operations
------------------
The public surface is deliberately small. Every caller needs at most
these six things, and no more:

    1. `safe_resolve`          — is this path structurally safe?
    2. `validate_destination`  — is this safe path ready to write?
    3. `detect_collisions`     — does the plan overwrite anything on disk?
    4. `backup_existing`       — preserve files before an overwrite
    5. `StagingArea`           — where writes happen (the only place)
    6. errors — `Finding`, `SafetyError`, `CollisionError`

Anything else a caller wants — a boolean `is_safe_relative`, a
`normalise` helper, the details of a policy — is either an internal
concern, exposed through a re-export that is *intended* for a specific
caller, or does not exist on purpose. Small façade, small surface to
maintain, small surface to break.

What is exported, and why
-------------------------
Everything in ``__all__`` is either:

    * a caller-facing operation (the six above),
    * a caller-facing type (`Finding`, `SafetyError`, `CollisionError`,
      `Backup`),
    * a caller-facing policy (`SafetyPolicy`, `DEFAULT_POLICY`),
    * a delegating helper (`is_safe_relative`, `raise_if_collisions`)
      that exists *specifically* to prevent two other modules from
      reimplementing the same logic.

The policy is exported because callers legitimately need it:

    * the CLI passes `overwrite=True` policies into `create`,
    * a future `forge doctor` may want a strict policy to diagnose a
      filesystem's capabilities,
    * tests construct policies to prove behaviour under different
      rules.

A `STRICT_POLICY` constant is *not* exported — if a future caller
needs a strict policy, it constructs one. Exporting the constant would
invite accidental use in production, which is the opposite of what
"strict" is for.

What is *not* exported
----------------------
    * `_policy` — the module name starts with `_`; `SafetyPolicy` is
      exported, `_policy.SafetyPolicy` is not. The underscore prefix is
      a Python convention for "private"; the façade respects it.
    * `paths`, `destinations`, `collisions`, `overwrite`, `staging` —
      the implementation modules. Their functions are exported through
      the façade under the names callers actually need
      (`safe_resolve`, not `paths.safe_resolve`).
    * Anything under `_` inside the implementation modules (e.g.
      `_check_no_symlink_components`, `_copy_atomic`) — internal
      helpers, not part of any contract.

The façade's own contract
-------------------------
Everything in ``__all__`` is guaranteed to be importable from
``forge.safety`` and to keep working across minor versions. Adding a
name is additive (safe); removing or renaming a name is a breaking
change and requires an ADR amendment. This is the discipline that makes
"the safety layer is one import away" a sustainable claim rather than
an aspiration.
"""

from __future__ import annotations

from pathlib import Path as _Path

from forge.safety._policy import DEFAULT_POLICY, SafetyPolicy
from forge.safety.collisions import detect_collisions, raise_if_collisions
from forge.safety.destinations import validate_destination
from forge.safety.errors import CollisionError, Finding, SafetyError
from forge.safety.overwrite import Backup, backup_existing
from forge.safety.paths import safe_resolve
from forge.safety.staging import StagingArea


# ---------------------------------------------------------------------------
# Boolean convenience helper
# ---------------------------------------------------------------------------
#
# The loader, the planner, and a few defensive checks want a fast
# yes/no answer ("is this path even worth opening?") without inspecting
# a `Finding`. A boolean form of `safe_resolve` serves that need
# without inviting those callers to reimplement the safety rules.
#
# Two choices worth flagging:
#
#   1. **Root defaults to the current working directory.**
#      Callers that need to check a path against a *specific* root
#      (the loader checking templates against a blueprint directory,
#      for instance) must use `safe_resolve(path, root)` directly.
#      The boolean form is a *quick* check, not a *complete* one — the
#      docstring says so, and the loader's own use of `safe_resolve`
#      with an explicit root is the reason this limitation is
#      acceptable.
#
#   2. **Defined here, not delegated to `planning.destinations`.**
#      The safety layer is a primitive; `planning` is a consumer of the
#      safety layer. A primitive must not depend on its consumers, or
#      we invert the dependency graph and invite future cycles. The
#      `planning.destinations.is_safe_relative` name continues to exist
#      (it was already published in a previous revision), and it can be
#      implemented by calling into this helper — that's a caller-side
#      dependency, in the correct direction.
#
def _is_safe_relative(candidate: str, *, root: _Path | None = None) -> bool:
    """Boolean form of :func:`safe_resolve` — re-exported for callers
    that only need the yes/no answer.

    Parameters
    ----------
    candidate
        The candidate path. May be relative or absolute; absolute
        paths are always rejected (see :func:`safe_resolve`).
    root
        The directory the path is checked *against*. Defaults to the
        current working directory so that the common
        "is this path even safe to consider?" check works without
        ceremony. Callers that need to check against a specific root
        (e.g. the loader checking a template against the blueprint
        directory) should call :func:`safe_resolve` directly with that
        root — that's the point of the primitive.

    Returns
    -------
    bool
        ``True`` if `safe_resolve(candidate, root)` produced no finding.
        The finding's *reason* is intentionally discarded: callers that
        need to explain *why* a path is unsafe must call
        :func:`safe_resolve` themselves and render the finding. The
        boolean form is not a replacement for that.
    """
    effective_root = root if root is not None else _Path.cwd()
    _, finding = safe_resolve(candidate, effective_root)
    return finding is None


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------

__all__ = [
    # Operations
    "safe_resolve",
    "validate_destination",
    "detect_collisions",
    "backup_existing",
    # Staging
    "StagingArea",
    # Errors
    "Finding",
    "SafetyError",
    "CollisionError",
    # Types returned by operations
    "Backup",
    # Policy
    "SafetyPolicy",
    "DEFAULT_POLICY",
    # Delegating helpers (see docstrings for why each exists)
    "raise_if_collisions",
    "is_safe_relative",
]


# The boolean helper is exposed under its public name *after* `__all__`
# so that readers scanning the export list see every public name in the
# intended reading order, then find the definitions below in the same
# order. This is a stylistic choice, not a functional one: `__all__`
# only controls `from forge.safety import *` and static-analysis tools,
# both of which read the final bindings regardless of where they were
# assigned.
is_safe_relative = _is_safe_relative
