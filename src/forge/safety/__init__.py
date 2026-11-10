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

    from forge.safety.paths import safe_resolve    # ❌ never this
    from forge.safety._policy import DEFAULT_POLICY  # ❌ never this

Why? Because:

    * the façcade is a *compatibility surface* we control — we can
      rename `paths.safe_resolve` to `paths.resolve_safe` without
      touching a single caller,
    * the internal modules (`paths`, `destinations`, `collisions`,
      `staging`, `overwrite`) are free to evolve, split, or merge as
      long as the façade's names keep working,
    * a caller that imports from `forge.safety` gets a *coherent*
      mental model — six names for six operations — instead of a
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

What is exported *and why*
--------------------------
Everything in `__all__` is either:

    * a caller-facing operation (the six above),
    * a caller-facing type (`Finding`, `SafetyError`, `CollisionError`,
      `Backup`),
    * a caller-facing policy (`SafetyPolicy`, `DEFAULT_POLICY`),
    * a delegating helper (`is_safe_relative`, `raise_if_collisions`)
      that exists *specifically* to prevent two other modules from
      reimplementing the same logic.

The policy is exported because callers legitimately need it:

    * the CLI passes `overwrite=True` policies into `create`,
    * a future `forge doctor` may want a strict policy to diagnose
      a filesystem's capabilities,
    * tests construct policies to prove behaviour under different
      rules.

`STRICT_POLICY` is *not* exported — it exists in `_policy.py` for
tests only. If a future caller needs a strict policy, it constructs
one; the export would invite accidental use in production, which is
the opposite of what "strict" is for.

What is *not* exported
----------------------
    * `_policy` — the module name starts with `_`; `SafetyPolicy` is
      exported, `_policy.SafetyPolicy` is not. Underscore prefix is a
      Python convention for "private"; the façade respects it.
    * `paths`, `destinations`, `collisions`, `overwrite`, `staging` —
      the implementation modules. Their functions are exported
      through the façade under the names callers actually need
      (`safe_resolve`, not `paths.safe_resolve`).
    * Anything under `_` inside the implementation modules (e.g.
      `_check_no_symlink_components`, `_copy_atomic`) — internal
      helpers, not part of any contract.

The façade's own contract
-------------------------
Everything in `__all__` is guaranteed to be importable from
`forge.safety` and to keep working across minor versions. Adding a
name is additive (safe); removing or renaming a name is a breaking
change and requires an ADR amendment. This is the discipline that
makes "the safety layer is one import away" a sustainable claim
rather than an aspiration.
"""

from pathlib import Path as _Path

from forge.safety._policy import DEFAULT_POLICY, SafetyPolicy
from forge.safety.collisions import detect_collisions, raise_if_collisions
from forge.safety.destinations import validate_destination
from forge.safety.errors import CollisionError, Finding, SafetyError
from forge.safety.overwrite import Backup, backup_existing
from forge.safety.paths import safe_resolve

# The delegation helper from `planning.destinations` is exported here
# so callers that want a quick boolean check (e.g. the loader deciding
# whether to even attempt to read a referenced template) do not have
# to call `safe_resolve` and then inspect the finding. This is a
# *convenience* export, not a *replacement* — the boolean form loses
# the finding details, so callers that need to report *why* a path is
# unsafe must use `safe_resolve` directly.
#
# Importing it from `planning.destinations` (rather than re-exporting
# `paths.is_safe_relative`, which does not exist as a first-class
# function) keeps a single boolean-safety implementation. The
# `planning.destinations.is_safe_relative` is a thin wrapper around
# `safe_resolve`, so there is exactly one path checker in the
# codebase.
from forge.safety.paths import safe_resolve as _safe_resolve
from forge.safety.staging import StagingArea


def _is_safe_relative(candidate: str) -> bool:
    """Boolean form of `safe_resolve` — re-exported for callers that
    only need the yes/no answer.

    Preserved here (rather than delegating to
    `planning.destinations.is_safe_relative`) so `forge.safety` has no
    dependency on `forge.planning`. Dependency direction matters: the
    safety layer is a primitive; the planner is a consumer. A
    primitive must not depend on its consumers.
    """
    _, finding = _safe_resolve(candidate, _Path.cwd())
    return finding is None


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
    # Delegation helpers (see docstrings for why each exists)
    "raise_if_collisions",
    "is_safe_relative",
]

# The boolean helper is defined as a wrapper above; assign it to the
# public name after `__all__` so the ordered list documents intent
# before implementation details. This is a stylistic choice: readers
# scanning `__all__` see every public name in the intended reading
# order, then find the definitions below in the same order.
is_safe_relative = _is_safe_relative
