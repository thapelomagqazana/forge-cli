"""Collision detection — plan vs. disk, in one pass, with overwrite consent.

Where this fits
---------------
Three gates protect the filesystem:

    1. `safe_resolve`           — "is this path structurally safe?"
    2. `validate_destination`   — "is the leaf ready for a write?"
    3. `detect_collisions`      — "is there already a file at this
                                   destination, and did the user consent
                                   to overwrite it?"

This module is the third gate. It compares the plan (a set of
`PlannedFile` entries) against what is *actually on disk* right now,
in one pass, and reports everything that would be overwritten.

Why "in one pass" matters
-------------------------
A plan can contain hundreds of files. A naive implementation that
calls `os.lstat()` per file and appends findings as they come would
be correct but would produce findings out of order — which makes the
report harder to read and golden tests brittle. One pass over a
sorted list produces one sorted finding list, deterministically. Same
inputs, same report, always.

Two classes of collision
------------------------
1. **Exact collisions** — the destination already exists on disk.
   Prevented unless the user passed `--overwrite`.

2. **Case-insensitive duplicates within the plan** — the plan
   contains two destinations that differ only in case
   (`README.md` vs `readme.md`). On a case-insensitive filesystem
   (macOS, Windows, most Docker volumes) these are the same file;
   the plan is silently self-colliding. We report this regardless of
   overwrite consent, because it is a *plan defect* — the user does
   not have a specific file to preserve; they have a plan that will
   behave differently on different machines.

Why overwrite consent is not enough for case-duplicates
-------------------------------------------------------
`--overwrite` means "I accept replacing what is on disk". It does
not mean "I accept a plan that will behave differently on macOS
than on Linux". The two are orthogonal, and only the first is a
deliberate user choice. Case-duplicate findings are therefore *not*
suppressed by `overwrite=True`.

Why findings, not exceptions
----------------------------
Same discipline as `paths.py` and `destinations.py`. Callers
aggregate all findings into one report. `raise_if_collisions` is the
only function in this module that raises, and it does so only after
the caller has decided to fail — so the caller controls the
boundary between "report" and "abort", not this module.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path

from forge.planning.planned_file import PlannedFile
from forge.safety._policy import DEFAULT_POLICY, SafetyPolicy
from forge.safety.errors import CollisionError, Finding

# Findings whose codes indicate a collision, used by `raise_if_collisions`
# to distinguish a safety failure from a collision. Kept as a frozenset so
# a caller can extend the set (a future code) without editing conditionals
# in this module.
_COLLISION_CODES: frozenset[str] = frozenset(
    {
        "collision.exists",
        "plan.case_insensitive_duplicate",
    }
)


def detect_collisions(
    plan: Iterable[PlannedFile],
    destination_root: Path,
    *,
    overwrite: bool,
    policy: SafetyPolicy = DEFAULT_POLICY,
) -> list[Finding]:
    """Return findings for every destination that already exists on disk.

    Parameters
    ----------
    plan : Iterable[PlannedFile]
        The planned files, as produced by `FileMapper.map()`. Typically
        already sorted by destination; order does not affect correctness
        but does affect finding order (we sort findings at the end, so
        the caller can rely on determinism regardless).
    destination_root : Path
        The root into which destinations will be written. Callers must
        have already validated this root exists; a missing root simply
        produces no findings (nothing is on disk).
    overwrite : bool
        True when the user has explicitly consented to replacing existing
        files. When True, exact collisions are suppressed (backups are
        the caller's responsibility). Case-insensitive plan duplicates
        are *not* suppressed — see module docstring.
    policy : SafetyPolicy
        Rule set. Injected for tests and future per-operation overrides.

    Returns
    -------
    list[Finding]
        Empty when the plan is collision-free. Populated with one finding
        per collision or per case-duplicate. Sorted by `path` for stable
        output. Never raises.

    Notes
    -----
    * Only `os.lstat` is used — no following of symlinks. A symlink at
      the destination is a collision just like a regular file: writing
      through it would follow the link, which is precisely what the
      safety layer forbids.
    * A broken symlink counts as a collision. `Path.exists()` would say
      False; `lstat` says True. The name is taken; overwriting it needs
      consent.
    """
    findings: list[Finding] = []

    # Case-duplicate tracking: {casefolded_dest: first_seen_dest}. We
    # keep the first *seen* dest so the message can show both forms.
    seen_casefold: dict[str, str] = {}

    for planned in plan:
        target = destination_root / planned.destination

        # --- Case-insensitive duplicate detection within the plan ------
        # Enabled by policy; on by default. This is about *the plan*,
        # not about disk — it is the same on any filesystem, and it
        # catches mistakes before they become divergent behaviour.
        if policy.warn_on_case_insensitive_collisions:
            key = str(planned.destination).casefold()
            prior = seen_casefold.get(key)
            if prior is not None and prior != planned.destination:
                findings.append(
                    Finding(
                        code="plan.case_insensitive_duplicate",
                        path=planned.destination,
                        message=(
                            f"plan contains paths differing only in case: "
                            f"{prior!r} vs {planned.destination!r}"
                        ),
                        hint=(
                            "Rename one path. Case-insensitive filesystems "
                            "(macOS, Windows, many Docker volumes) will treat "
                            "these as the same file and one will silently "
                            "overwrite the other."
                        ),
                    )
                )
            else:
                # Only record the first form; subsequent forms that
                # differ in case will trigger the finding above. Equal
                # repeats (same exact string) are handled by the disk
                # check that follows — they would collide on disk too.
                seen_casefold[key] = planned.destination

        # --- On-disk collision ----------------------------------------
        # lstat, not stat / exists: we care about the entry, not its
        # target. A symlink at `target` is a collision (we must not
        # overwrite the link, and the caller must not follow it).
        exists, inspect_failed = _lstat_safe(target)

        if inspect_failed:
            # Fail closed. If we cannot inspect the destination, we
            # cannot prove it is free. A false positive is a soft error
            # the user can override with --overwrite; a false negative
            # is a silent overwrite.
            findings.append(
                Finding(
                    code="collision.inspect_failed",
                    path=str(target),
                    message="cannot inspect destination; treating as a collision",
                    hint=(
                        "Check permissions on the target directory, or "
                        "remove the file manually and re-run."
                    ),
                )
            )
            continue

        if not exists:
            continue  # destination is free; nothing to report

        if overwrite:
            # User consented. Backups are the caller's responsibility
            # (see overwrite.backup_existing). No finding emitted.
            continue

        findings.append(
            Finding(
                code="collision.exists",
                path=planned.destination,
                message="destination file already exists",
                hint=(
                    "Re-run with --overwrite to replace it (a timestamped "
                    "backup will be created automatically)."
                ),
            )
        )

    # Stable, deterministic ordering. The plan order is arbitrary (a
    # set of PlannedFile entries); the report order must not be.
    findings.sort(key=lambda f: f.path)
    return findings


def raise_if_collisions(findings: list[Finding], *, source: str) -> None:
    """Raise `CollisionError` when any collision-class finding exists.

    Why a separate function?
        Callers typically aggregate findings from *all three gates*
        (`safe_resolve`, `validate_destination`, `detect_collisions`)
        before deciding what to do. They need to distinguish "a
        collision happened" (exit code 6, offer --overwrite) from "a
        structural safety issue happened" (exit code 5, do not offer
        --overwrite, because --overwrite would not help).

        Raising here, from a shared function, keeps that distinction in
        one place: if a future gate adds a new collision-ish code, only
        `_COLLISION_CODES` needs updating, not every caller.

    Parameters
    ----------
    findings : list[Finding]
        The aggregated findings from whichever gates the caller ran.
    source : str
        A label for the operation (e.g. "create", "dry-run"). Included
        in the exception for logging and error rendering.
    """
    collisions = [f for f in findings if f.code in _COLLISION_CODES]
    if collisions:
        raise CollisionError(findings=tuple(collisions), operation=source)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _lstat_safe(path: Path) -> tuple[bool, bool]:
    """Return (exists, inspect_failed) without following symlinks.

    Three outcomes, all distinct:

        * Entry exists (file, dir, or symlink, broken or not) → (True, False)
        * Entry definitely does not exist                     → (False, False)
        * We could not determine (permission, I/O error)      → (False, True)

    The third case is the caller's cue to fail closed (which it does by
    emitting a `collision.inspect_failed` finding). Conflating it with
    "does not exist" would allow a silent overwrite in the presence of
    an I/O error, which is exactly the class of bug this module exists
    to prevent.
    """
    try:
        os.lstat(path)
        return True, False
    except FileNotFoundError:
        return False, False
    except OSError:
        return False, True
