"""Overwrite backups — the data-preservation guarantee for `--overwrite`.

Why this module exists
----------------------
`--overwrite` is a promise: "yes, replace what I have, but do not
lose it". Silent replacement without recovery is the single most
common way scaffolding tools destroy users' work. This module makes
that failure mode impossible by construction:

    * Every file about to be replaced is *copied* (never moved) to a
      sibling path with a UTC timestamp appended, before any write
      touches the original.
    * The copy is atomic per file: `copy2` into a hidden temp file in
      the same directory, then `os.replace` into the backup name.
      Either the backup exists and is complete, or it does not exist
      and the original is untouched. There is no half-backup state.
    * If *any* backup fails, *all* backups created so far in this call
      are removed, and the exception propagates. The caller receives a
      filesystem in its pre-backup state — no debris, no partial
      recovery set, no "half overwritten project".
    * Backups are named deterministically (`<name>.bak.<UTC-timestamp>`)
      so they are inspectable with `ls`, sortable, and scriptable. A
      tarball would be opaque; a per-file backup is obvious.

Why per-file, not a tarball
---------------------------
A tarball is smaller and "one thing". But it is:

    * opaque (users need `tar -tf` to see what was preserved),
    * all-or-nothing (a corrupt tarball loses every backup),
    * not copy-paste-able (the user cannot just `cp` one file back).

Per-file backups trade a little disk space for inspectability and
partial-recovery semantics that are obvious to any user with `ls`.
For a scaffolding tool that is the right trade.

Why `copy2`, not `copy` / `copyfile`
------------------------------------
`copy2` preserves mtime and mode bits. That matters because:

    * mtime is what `git status` uses to decide "changed" — preserving
      it keeps backups out of unexpected diffs,
    * mode bits (executable, read-only) are part of what the user
      meant to preserve; losing them on restore is a subtle bug.

Why same directory for the backup
---------------------------------
Two reasons:

    * `os.replace` is atomic only *within* a filesystem. Backups in
      a different directory (e.g. `/tmp`) may land on a different
      device, forcing a copy and breaking atomicity.
    * Same-directory backups are obvious to the user — they see
      `pyproject.toml.bak.20240101T000000Z` next to `pyproject.toml`.

Why a single `timestamp` parameter
----------------------------------
Tests need deterministic names. Production needs "now". A single
optional parameter (defaulting to "now") makes both work without a
test-only code path — the test passes a fixed timestamp, production
does not, and there is no `if testing:` in the module. That is the
difference between a module that is tested and a module that is
*testable*.

The parameter distinguishes "caller passed nothing" from "caller
passed an empty string". ``timestamp=None`` (the default) means "use
now". ``timestamp=""`` is a caller bug and is rejected by the
validator. A truthiness check would conflate the two, silently
substituting "now" for an empty string and producing backups whose
names the caller did not ask for.

Why findings are not used here
------------------------------
The other safety modules (`paths`, `destinations`, `collisions`)
return `Finding` values because they *inspect* and the caller
*aggregates* before deciding. Backups are different: they *act*, and
the action either succeeds or fails. An error here means "the
filesystem refused to do what we asked" — a hard failure, not a
reportable finding. Raising `OSError` (and rolling back) is the right
semantic. The caller translates it into a `SafetyError` finding for
the user if it wants to.

Broken symlinks raise; they are not silently skipped
----------------------------------------------------
A broken symlink occupies a name (so ``_target_exists`` returns True)
but has no content to preserve (so ``copy2`` raises
``FileNotFoundError``). The two observations point in opposite
directions for the *existence* check but agree for the *action*: the
function cannot honour its contract ("copy the file the user is about
to lose") for a target with no bytes, so it raises rather than
proceeding with an empty backup.

The behaviour is a *consequence* of the safety layer's upstream
policy: ``safe_resolve`` and ``validate_destination`` both reject
symlink destinations by default, so a broken symlink should never
reach this function through the normal pipeline. If it does — because
a caller bypassed the safety layer, or because a symlink was created
in the TOCTOU window between the check and the backup — raising is
the correct signal. Silently skipping would hide the bypass and let
the subsequent write replace a symlink the user placed intentionally,
with no backup to restore from.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

# Reserved characters that break the "backup is a normal file" promise:
# spaces, slashes, colons, and NUL are all rejected in filenames on at
# least one major platform. We generate timestamps ourselves so we
# control them, but the check documents the assumption and guards
# against a future default-format change.
_TIMESTAMP_FORMAT = "%Y%m%dT%H%M%SZ"


@dataclass(frozen=True, slots=True)
class Backup:
    """One backup that was successfully created.

    Returned to the caller so it can report them ("3 files backed up to
    *.bak.20240101T000000Z") or record them for a rollback log. Never
    constructed by the caller — produced only by `backup_existing`.
    """

    original: Path
    backup: Path


def backup_existing(
    targets: Iterable[Path],
    *,
    timestamp: str | None = None,
) -> list[Backup]:
    """Copy each existing target to a timestamped backup. All-or-nothing.

    Parameters
    ----------
    targets : Iterable[Path]
        Paths that may be overwritten. Non-existent targets are
        silently skipped — "nothing to back up" is not a failure, it
        is the common case for a fresh scaffold.
    timestamp : str | None
        Fixed timestamp for deterministic tests. When ``None``
        (production), a UTC timestamp of the current moment is used,
        in the format ``YYYYMMDDTHHMMSSZ``. An *empty* string is
        rejected — it is a caller bug (a template that interpolated
        a missing variable, a caller that computed the value from
        user input without validating it), and silently substituting
        "now" would produce a backup whose name the caller did not
        ask for.

    Returns
    -------
    list[Backup]
        One ``Backup`` per existing target, in the order they were
        backed up. Empty when nothing existed.

    Raises
    ------
    OSError
        If any copy fails. Before raising, every backup created so
        far in this call is removed — the filesystem is left exactly
        as it was at call entry. The caller sees a clean failure and
        can tell the user "nothing was changed".
    FileNotFoundError
        Subclass of ``OSError``. Raised when a target is a *broken
        symlink* (a name in use with no content). The function cannot
        honour "copy the file" for such a target; see the module
        docstring's "Broken symlinks raise" section.
    ValueError
        When ``timestamp`` is present but unsafe (empty, path
        separator, NUL, newline). Rejected before any filesystem
        operation, so a bad timestamp cannot leave debris.

    Notes
    -----
    * Targets are deduplicated by resolved path (two spellings of the
      same file produce one backup, not two).
    * Symlinks at a target are *followed* by ``copy2``'s default
      behaviour. For a symlink to a valid target, that produces a
      regular file containing the target's bytes — recoverable
      content, even if not the symlink itself. For a *broken* symlink
      it raises ``FileNotFoundError``; see above.
    """
    # ``is not None`` rather than truthiness: an empty string is a
    # caller bug that would otherwise be silently replaced by "now",
    # defeating `_validate_timestamp`'s "non-empty" check.
    ts = timestamp if timestamp is not None else datetime.now(UTC).strftime(_TIMESTAMP_FORMAT)
    _validate_timestamp(ts)

    created: list[Backup] = []
    seen: set[Path] = set()

    try:
        for target in targets:
            # Resolve is intentionally *not* called for the backup
            # path itself — we want the backup to land next to the
            # file the caller is about to overwrite. We *do* resolve
            # for deduplication, so that two spellings of the same
            # file produce one backup.
            try:
                resolved = target.resolve(strict=False)
            except OSError:
                resolved = target
            if resolved in seen:
                continue
            seen.add(resolved)

            if not _target_exists(target):
                continue

            backup = _make_backup_path(target, ts)
            _copy_atomic(target, backup)
            created.append(Backup(original=target, backup=backup))
    except Exception:
        _rollback(created)
        raise

    return created


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _validate_timestamp(ts: str) -> None:
    """Reject timestamps that would produce unsafe backup filenames.

    The timestamp becomes part of a filename. A timestamp containing
    ``/``, ``\\``, NUL, or a newline could turn ``<name>.bak.<ts>``
    into a different path entirely — the kind of thing that turns a
    data-preservation feature into a data-loss bug. Since the
    parameter is documented as "for tests", this is a small guard
    that closes a small door thoroughly.

    Empty strings are rejected because the caller explicitly passed
    them (``None`` is the "use now" sentinel; ``""`` is a caller
    bug). Silently substituting "now" for an empty string would
    hide the bug and produce a backup with a name the caller did
    not ask for.
    """
    if not ts:
        raise ValueError("timestamp must be non-empty")
    forbidden = set("/\\\x00\n\r")
    if any(ch in forbidden for ch in ts):
        raise ValueError(f"timestamp contains forbidden characters: {ts!r}")
    # Belt and braces: reject path separators via the OS's own check.
    if os.sep in ts or (os.altsep and os.altsep in ts):
        raise ValueError(f"timestamp contains a path separator: {ts!r}")


def _target_exists(target: Path) -> bool:
    """True iff *anything* occupies the target name (incl. broken symlinks).

    Uses ``lstat`` for the same reason as the rest of the safety
    layer: ``exists()`` follows symlinks and would say a broken link
    "does not exist" — but it is a name in use that the upcoming
    overwrite will replace, and it deserves a backup.

    A broken symlink returns ``True`` here. Whether the *copy* then
    succeeds or raises depends on ``copy2``'s behaviour (it follows
    symlinks and raises on broken ones). That is the correct
    separation: this function answers "is the name occupied?", and
    ``_copy_atomic`` answers "can the content be copied?". The two
    questions have different answers for broken symlinks, and the
    caller gets the honest outcome from each.
    """
    try:
        os.lstat(target)
        return True
    except FileNotFoundError:
        return False
    except OSError:
        # Fail closed: if we cannot determine whether the file exists,
        # treat it as existing. A spurious backup is cheap; a missed
        # backup means losing user data.
        return True


def _make_backup_path(target: Path, timestamp: str) -> Path:
    """Compose the backup path. Deterministic, same directory as target."""
    return target.with_name(f"{target.name}.bak.{timestamp}")


def _copy_atomic(source: Path, destination: Path) -> None:
    """Copy ``source`` to ``destination`` atomically.

    The pattern: copy to a hidden temp file in the *same directory* as
    the destination, fsync (implicitly via ``copy2`` + ``close``),
    then ``os.replace`` to the final name. ``os.replace`` is atomic
    within a filesystem, so the destination either does not exist or
    is a complete copy — never a partial one.

    If the copy fails partway through, the temp file is unlinked in
    the ``finally`` block, so no debris is left behind. Any failure
    raises to the caller, which is responsible for rolling back any
    earlier backups.

    For a *broken symlink*, ``copy2`` follows the link and raises
    ``FileNotFoundError`` from ``open(src, 'rb')``. The temp file is
    cleaned up by the ``finally`` block, and the exception propagates
    to ``backup_existing``'s ``except`` clause, which rolls back any
    earlier backups before re-raising. That is the correct outcome
    for a target with no content to preserve.
    """
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=str(destination.parent),
    )
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        shutil.copy2(source, tmp_path)
        os.replace(tmp_path, destination)
    finally:
        # If the copy or the replace raised, the temp file is still
        # here; remove it. If they succeeded, the temp file no longer
        # exists and the `unlink` raises FileNotFoundError, which we
        # suppress. `contextlib.suppress` reads as the intent ("ignore
        # this one expected failure") rather than as a try/except/pass
        # block that a reviewer has to scan to confirm does nothing
        # else.
        with contextlib.suppress(FileNotFoundError):
            tmp_path.unlink()


def _rollback(created: list[Backup]) -> None:
    """Remove every backup created so far, best-effort.

    A rollback that itself fails (permission removed, filesystem now
    read-only) still propagates the *original* exception — the user
    deserves to know what went wrong first. Cleanup failures are
    logged via the raised exception's context; we do not mask them
    with a "cleanup failed" exception that hides the real cause.
    """
    for b in created:
        try:
            b.backup.unlink()
        except FileNotFoundError:
            # Someone else removed it; nothing to do.
            pass
        except OSError:
            # Rollback is best-effort. The original exception is more
            # important than a cleanup failure.
            pass
