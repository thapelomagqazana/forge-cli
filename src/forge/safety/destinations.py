"""Destination validation — existence, writability, symlink policy.

Where this fits
---------------
`safe_resolve` (paths.py) answers: *"Is this path structurally safe to
write to?"* — no traversal, no absolute paths, no symlink components.
That is a *path-shape* question.

This module answers: *"Is the destination *now* ready to receive the
write?"* — three distinct concerns:

    1. **Existence.** A file already at the destination is a collision
       unless the user explicitly asked to overwrite. Silent overwrite
       is how scaffolders lose work.
    2. **Symlink at the leaf.** A symlink pointing *to* the destination
       is not a destination — it is a redirect. We refuse to write
       through it, even if `safe_resolve` allowed the parent chain,
       because the leaf itself is what the OS will follow.
    3. **Parent writability.** The directory the OS will actually
       create the file in must be writable. If the immediate parent
       does not exist yet, walk up to the nearest existing ancestor —
       that is what the OS will do at write time.

Why `lstat` and not `stat` / `exists()`
---------------------------------------
`Path.exists()` follows symlinks and lies:

    * A broken symlink at `dest` → `exists()` returns False, but a
      write through it will fail or, worse, succeed at the target.
    * A symlink at `dest` → `exists()` returns True and reports "a
      file exists", not "a symlink exists". We need to distinguish.

`os.lstat` reports the *entry* on disk, not its target. Every disk
inspection in this module uses `lstat` for that reason.

Why findings, not exceptions
----------------------------
Same discipline as every other safety check: **return all problems at
once**. If a destination exists *and* its parent is read-only, the user
should see both, fix both, and re-run once. Fixing one error at a time
is the punishment we are avoiding.

The batch-reporting promise applies *within* a single destination too.
A destination whose leaf is a symlink *and* whose parent is unwritable
is one destination with two problems, and the caller sees both. The
implementation enumerates every applicable check rather than
short-circuiting on the first — see ``_leaf_symlink_finding`` for the
one place where the checks interact, and the "Symlink shadows exists"
section below for why the interaction is documented rather than
hidden.

Why `overwrite` is a parameter, not a global
--------------------------------------------
Overwrite consent is *per-operation*: `forge create` needs it, but a
future `forge generate --dry-run` never does. Making it a required
parameter forces every caller to make the choice explicitly — a silent
default of `False` is safe, a silent default of `True` is a bug
waiting to happen.

Symlink shadows exists (the interaction that needed documenting)
----------------------------------------------------------------
The leaf-symlink check runs *before* the existence check, and under
the *default* policy a leaf symlink produces ``destination.symlink``
without a subsequent ``destination.exists`` — for two independent
reasons:

    * **The findings describe the same problem.** "The leaf is a
      symlink" and "something occupies the leaf name" are two true
      statements about the same on-disk entry. Emitting both for the
      same destination would look like two problems and invite the
      user to fix one.

    * **The remediations conflict.** ``destination.symlink``'s hint
      says "remove the symlink". ``destination.exists``'s hint says
      "use ``--overwrite``". A user who follows the second hint
      without reading the first would pass ``--overwrite`` — which
      consents to replacing a *file*, not to removing a *symlink*.
      The write would either follow the (possibly broken) link, or
      fail at open time. Either way, the user's intent ("overwrite
      this file") is not served. Emitting both hints is worse than
      emitting the one that names the actual fix.

So under the *default* policy, the answer to "does a broken symlink
produce ``destination.exists``?" is **no, it produces
``destination.symlink``**. That is correct: a broken symlink is a
symlink first and an occupied name second, and the symlink is the
more actionable framing.

Under ``allow_symlinks_in_destination=True``, the leaf-symlink check
is skipped entirely (by the policy), so the existence check fires —
because ``_lstat_exists`` correctly treats a broken symlink as an
entry on disk. Both behaviours are covered by tests:

    * ``test_broken_symlink_rejected_as_symlink_by_default`` — the
      default policy's finding.
    * ``test_broken_symlink_counts_as_existing_when_symlinks_allowed``
      — the opt-in policy's finding, which proves ``_lstat_exists``
      does what its docstring says.

Keeping the two behaviours pinned *separately* is the correct way to
document an interaction. A test that expected both codes in one call
would be asserting a shape the function does not produce, and the
"fix" would be to make the function produce a confusing report.
"""

from __future__ import annotations

import os
from pathlib import Path

from forge.safety._policy import DEFAULT_POLICY, SafetyPolicy
from forge.safety.errors import Finding


def validate_destination(
    resolved: Path,
    *,
    overwrite: bool,
    policy: SafetyPolicy = DEFAULT_POLICY,
) -> list[Finding]:
    """Validate a resolved destination path. Returns *all* findings.

    Parameters
    ----------
    resolved : Path
        A path that has already passed `safe_resolve`. Passing an
        un-resolved path here bypasses the structural checks — this
        module assumes the structural question is answered.
    overwrite : bool
        True when the user has explicitly consented to replacing
        existing files (e.g. `--overwrite`). When True, the "already
        exists" check is skipped; the caller is responsible for
        creating backups (see `overwrite.backup_existing`).
    policy : SafetyPolicy
        Rule set. Injected for tests and future per-operation overrides.

    Returns
    -------
    list[Finding]
        Empty when the destination is safe to write. Populated with one
        finding per problem otherwise. Never raises.

    Notes
    -----
    Checks are *enumerated*, not short-circuited: the function runs
    every applicable check and returns every applicable finding. The
    one deliberate exception is documented in the module docstring's
    "Symlink shadows exists" section — under the default policy, a
    leaf symlink suppresses the existence finding because both
    findings describe the same problem and their hints conflict.

    A caller who wants the existence check to run despite a leaf
    symlink (for diagnostics, or because they have a legitimate
    symlink-based workflow) passes
    ``policy=SafetyPolicy(allow_symlinks_in_destination=True)``. The
    existence check then reports the occupied name, which is what
    they asked for.
    """
    findings: list[Finding] = []

    # --- 1. Leaf-symlink check -----------------------------------------
    # `safe_resolve` already walked components and rejected symlinks in
    # *existing* components. This re-check is defence in depth, and it
    # covers one case the walk cannot: a symlink *created between*
    # `safe_resolve` and this call (a TOCTOU window). Cheap to re-check;
    # expensive to get wrong.
    #
    # When the leaf *is* a symlink, we do *not* immediately return —
    # the parent-writability check below still applies, and a caller
    # who has explicitly opted into symlinks deserves to see the
    # existence finding too. The `_leaf_symlink_finding` helper
    # encapsulates the "did the leaf trigger?" decision so the two
    # checks below can be written as independent conditionals (the
    # batch-reporting discipline made concrete).
    leaf_symlink = _leaf_symlink_finding(resolved, policy)
    if leaf_symlink is not None:
        findings.append(leaf_symlink)

    # --- 2. Existence (overwrite consent) ------------------------------
    # Skip the existence check when:
    #   * the caller consented to overwrite (they know it exists), or
    #   * the leaf-symlink check already fired (the finding would
    #     describe the same on-disk entry — see the module docstring
    #     for the interaction rationale).
    #
    # We still *return* findings for other problems (e.g. unwritable
    # parent), because overwrite consent does not grant permission to
    # write into a directory the OS will refuse.
    if leaf_symlink is None and not overwrite and _lstat_exists(resolved):
        findings.append(
            Finding(
                code="destination.exists",
                path=str(resolved),
                message="destination already exists",
                hint=(
                    "Use --overwrite to replace it (a timestamped backup "
                    "will be created automatically)."
                ),
            )
        )

    # --- 3. Parent writability -----------------------------------------
    # The OS creates the file in the *immediate* parent. If that parent
    # does not exist, the OS will create it — but only if the nearest
    # *existing* ancestor is writable. So we walk up until we find
    # something that exists, and check writability there. This mirrors
    # exactly what `mkdir(parents=True)` will do at write time.
    findings.extend(_check_parent_writable(resolved))

    return findings


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _leaf_symlink_finding(
    resolved: Path,
    policy: SafetyPolicy,
) -> Finding | None:
    """Return the ``destination.symlink`` finding, or ``None``.

    Extracted so the leaf-symlink check has one definition and the
    caller can ask "did it fire?" without duplicating the condition.
    The helper performs the *policy* gate and the *filesystem* check
    in one place, so a future change to either (say, adding a new
    policy knob for symlinks-of-directories) has exactly one edit
    site.

    Why a helper rather than an inline check in ``validate_destination``:
        The caller needs to know *whether* the check fired to decide
        whether to run the existence check. Inline, that would require
        either a ``had_symlink`` boolean or duplicating the condition
        before the existence block — both worse than a helper whose
        return value is the answer.
    """
    if policy.allow_symlinks_in_destination:
        return None
    if not _is_symlink(resolved):
        return None
    return Finding(
        code="destination.symlink",
        path=str(resolved),
        message="destination is a symlink",
        hint=(
            "Writing through a symlink can write outside the "
            "intended directory. Remove the symlink, or choose "
            "a different destination."
        ),
    )


def _is_symlink(path: Path) -> bool:
    """True iff `path` is a symlink (broken or not). Never follows.

    Wrapped so a race between "check" and "open" cannot turn an OSError
    into an exception: any OSError from lstat is treated as "unknown",
    and the caller's fail-closed policy decides. In practice, if we
    cannot even `lstat` the leaf, the write is going to fail anyway —
    returning True here yields a clearer error.
    """
    try:
        return os.path.islink(path)
    except OSError:
        return True


def _lstat_exists(path: Path) -> bool:
    """True if any entry exists at `path`, symlink or not.

    Crucially different from `Path.exists()`:

        * broken symlink      → Path.exists() False; lstat True  ✅
        * symlink to file     → Path.exists() True;  lstat True  (both)
        * regular file        → Path.exists() True;  lstat True  (both)

    We want "is there something occupying this name", because that
    something would be overwritten. A broken symlink is still a name
    in use; overwriting it silently would surprise the user.
    """
    try:
        os.lstat(path)
        return True
    except FileNotFoundError:
        return False
    except OSError:
        # Fail closed: if we cannot determine existence, treat it as
        # existing. A false "already exists" is a soft error the user
        # can override with --overwrite; a false "does not exist" is a
        # silent overwrite, which is the bug we exist to prevent.
        return True


def _check_parent_writable(resolved: Path) -> list[Finding]:
    """Walk up to the nearest existing ancestor and check W_OK | X_OK.

    Why W_OK *and* X_OK?
        Writing a file requires the parent to be writable (W_OK) to
        create the directory entry, and executable (X_OK) to traverse
        into it. Either check alone is incomplete on POSIX.

    Why walk up?
        A destination like `src/new/pkg/file.py` may have no existing
        ancestor below `src/`. The OS will create `new/`, `pkg/` and
        then the file — provided `src/` is writable and executable.
        Checking `src/` is checking the right thing.
    """
    parent = resolved.parent

    # Walk up while the parent does not exist. Stop at filesystem root
    # (parent == parent.parent) to avoid an infinite loop.
    while not _lstat_exists(parent) and parent != parent.parent:
        parent = parent.parent

    if not _lstat_exists(parent):
        # Reached the filesystem root and it does not exist — impossible
        # on a healthy system, but fail closed rather than guess.
        return [
            Finding(
                code="destination.no_ancestor",
                path=str(resolved),
                message="no existing ancestor directory to write into",
                hint="Ensure the root path exists and is writable.",
            )
        ]

    # Non-directory ancestors are fatal: you cannot create a file under
    # a path component that is a regular file.
    if not _is_directory(parent):
        return [
            Finding(
                code="destination.parent_not_directory",
                path=str(parent),
                message="destination parent is not a directory",
                hint="A file exists where a directory is required. Remove it or choose another destination.",
            )
        ]

    if not _is_writable_and_traversable(parent):
        return [
            Finding(
                code="destination.parent_not_writable",
                path=str(parent),
                message="parent directory is not writable",
                hint=(
                    "Grant write and execute permission on the parent "
                    "directory, or choose a different destination."
                ),
            )
        ]

    return []


def _is_directory(path: Path) -> bool:
    """True iff `path` is a directory (does not follow symlinks).

    Symlinks were already rejected by `safe_resolve` and by the leaf
    check above. Using `lstat` here means we are always looking at the
    real entry — which is what the write is about to interact with.
    """
    try:
        return os.path.isdir(path) and not os.path.islink(path)
    except OSError:
        return False


def _is_writable_and_traversable(path: Path) -> bool:
    """POSIX-correct writability check: W_OK *and* X_OK.

    `os.access` uses the *effective* UID/GID, matching what `open()`
    will actually be permitted to do. Any OSError is treated as "not
    writable" — fail closed, as everywhere in this module.
    """
    try:
        return os.access(path, os.W_OK | os.X_OK)
    except OSError:
        return False
