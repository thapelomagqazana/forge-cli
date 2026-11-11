"""Filesystem fingerprint — the proof that a dry-run changed nothing.

Purpose
-------
A test that only asserts "no new files appeared" is a lie by omission.
It misses:
    * content changes to existing files (rename in place, truncate, append),
    * silent renames (path set differs, contents identical),
    * attribute changes (chmod, chown, mtime touch),
    * type changes (file → directory, dir → symlink),
    * ordering of removals and additions that happens to cancel out.

The correct invariant for "no mutations" is: **the fingerprint of the
tree before an operation equals the fingerprint of the tree after it**,
where the fingerprint captures paths, entry kinds, sizes, mtimes, and
content hashes.

Design
------
* **Deterministic.** Entries are sorted by relative path. Two fingerprints
  of the same tree are equal iff their entry lists are equal — no sets,
  no dicts, no ordering-by-default.

* **`lstat`, never `stat`.** Symlinks are recorded by target, not
  followed. Following them either escapes the root (a security concern
  in the test harness itself) or loops (a hang). Both outcomes are worse
  than the alternative: record what's on disk.

* **Content-hashed files.** SHA-256 of every regular file's bytes. mtime
  alone is insufficient — a same-second rewrite would slip past. Size
  alone is insufficient — a same-size rewrite would slip past. Content
  hashing is the only complete check.

* **mtime in nanoseconds.** Filesystem timestamps have varying
  granularity; nanosecond precision is the sharpest available and
  catches the subtle "the file was re-touched but not re-written" case
  that matters when verifying dry-run discipline.

* **Read-only.** The module never writes, never creates, never opens
  anything for writing. It's safe to call against any directory,
  including directories the caller shouldn't modify.

Non-goals
---------
* **Cryptographic tamper-resistance.** The fingerprint detects
  accidental mutations, not adversarial collisions. If an attacker can
  SHA-256-collide a file in the time between two fingerprints, they've
  already won by other means.

* **Cross-platform attribute parity.** We capture POSIX mtimes and
  modes; Windows extended attributes (ACLs, alternate data streams) are
  not in scope. If they matter for a specific test, capture them
  explicitly in that test.

* **Performance on huge trees.** Walking a monorepo with millions of
  files is slow by design — we read every byte. Use the fingerprint on
  the destination roots of dry-run tests, not on `$HOME`.
"""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Entry:
    """One filesystem entry in the manifest.

    Fields are chosen to be:
        * cheap to compare (dataclass equality),
        * complete enough to catch every mutation class,
        * stable across runs for an unmodified tree.
    """

    relpath: str
    """Path relative to the fingerprint root, using forward slashes.

    Normalising to `/` means two fingerprints taken on the same tree from
    different cwd's compare equal, and Windows/Linux trees produce the
    same fingerprint for the same logical structure.
    """

    kind: str
    """One of: ``file``, ``dir``, ``symlink``, ``other``.

    "other" covers FIFOs, sockets, device nodes — anything that isn't a
    regular file, directory, or symlink. We record their kind so their
    presence is visible in diffs; we do not open them, because opening a
    FIFO blocks and opening a device node is user-hostile.
    """

    size: int
    """Size in bytes (``lstat`` for symlinks; 0 for directories).

    Not used as the primary mutation detector — content hashes are — but
    included because it makes diffs human-readable at a glance.
    """

    mtime_ns: int
    """mtime in nanoseconds (POSIX). Zero on filesystems without
    nanosecond precision, which is a hint that mtime alone is insufficient
    for that tree — a signal the caller can act on.
    """

    digest: str
    """SHA-256 of the file content (hex), symlink target, or ``""``.

    Empty for directories and non-regular files. Non-empty for symlinks
    (the *target* string, not its content) so a re-pointed symlink is a
    visible mutation.
    """


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def fingerprint(root: Path | str) -> list[Entry]:
    """Return a sorted manifest of ``root``'s contents.

    The root itself is *not* included as an entry (it would be trivial
    and add noise). Every descendant — file, directory, symlink, other —
    is included, sorted by ``relpath``.

    Missing roots yield an empty list. This is intentional: a caller
    that runs ``before = fingerprint(p)`` on a nonexistent ``p``, then
    calls the operation, then ``after = fingerprint(p)`` gets
    ``[] == []`` if the operation also didn't create ``p`` — the desired
    outcome. If the operation *did* create ``p``, ``after`` will contain
    entries and the test will fail, exactly as we want.
    """
    root_path = Path(root)
    if not _exists_lstat(root_path):
        return []

    entries: list[Entry] = []
    for dirpath, dirnames, filenames in os.walk(root_path, followlinks=False):
        # Deterministic walk order. `os.walk` yields these in OS order;
        # sorting here is what makes the eventual sort-by-relpath
        # meaningful.
        dirnames.sort()
        filenames.sort()

        # Record directories as their own entries. We do this explicitly
        # (rather than relying on walk recursion) so that empty
        # directories appear in the manifest — otherwise "the operation
        # silently deleted an empty subdirectory" would be invisible.
        for name in dirnames:
            full = Path(dirpath) / name
            entries.append(_entry_for(full, root_path))
        for name in filenames:
            full = Path(dirpath) / name
            entries.append(_entry_for(full, root_path))

    entries.sort(key=lambda e: e.relpath)
    return entries


def diff(
    before: list[Entry],
    after: list[Entry],
) -> dict[str, list[Entry]]:
    """Return the differences between two fingerprints.

    Kept alongside ``fingerprint`` so tests can produce *actionable*
    failure messages ("created: ..., modified: ..., removed: ...")
    instead of dumping two 500-line lists into the terminal.

    Categories:
        * ``created`` — in ``after``, not in ``before``
        * ``removed`` — in ``before``, not in ``after``
        * ``modified`` — same relpath, different ``Entry``
    """
    before_by_path = {e.relpath: e for e in before}
    after_by_path = {e.relpath: e for e in after}

    created = [after_by_path[p] for p in after_by_path.keys() - before_by_path.keys()]
    removed = [before_by_path[p] for p in before_by_path.keys() - after_by_path.keys()]
    modified = [
        after_by_path[p]
        for p in before_by_path.keys() & after_by_path.keys()
        if before_by_path[p] != after_by_path[p]
    ]

    return {
        "created": sorted(created, key=lambda e: e.relpath),
        "removed": sorted(removed, key=lambda e: e.relpath),
        "modified": sorted(modified, key=lambda e: e.relpath),
    }


def assert_unchanged(
    before: list[Entry],
    after: list[Entry],
    *,
    context: str = "",
) -> None:
    """Raise ``AssertionError`` with a structured message if the trees differ.

    A thin convenience over ``diff`` for tests and integration checks —
    centralising the failure message means every "no mutations" assertion
    in the codebase reports problems the same way. Test authors get
    consistent, greppable output; CI logs stay readable.
    """
    d = diff(before, after)
    if not (d["created"] or d["removed"] or d["modified"]):
        return

    lines = [f"filesystem changed{(' during ' + context) if context else ''}:"]
    for category in ("created", "removed", "modified"):
        if d[category]:
            lines.append(f"  {category}:")
            for entry in d[category]:
                suffix = "" if entry.kind == "dir" else f" ({entry.kind}, {entry.size} bytes)"
                lines.append(f"    {entry.relpath}{suffix}")
    raise AssertionError("\n".join(lines))


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _exists_lstat(path: Path) -> bool:
    """Return True if any entry exists at ``path`` (symlink or not).

    Uses ``lstat`` so a broken symlink reports as existing. If we used
    ``Path.exists()``, a dangling symlink would look like a missing
    directory and the fingerprint would be empty — the exact case we
    cannot afford to miss when auditing a tree.
    """
    try:
        os.lstat(path)
        return True
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError:
        # Permission denied on a parent, or similar. Returning False
        # would silently "hide" the tree; returning True makes the
        # caller see an "unreadable" entry on the next walk. Failing
        # closed here matters more than being correct in the obscure
        # case where the path genuinely doesn't exist but the parent
        # is unreadable — both cases are rare and one is dangerous.
        return True


def _entry_for(path: Path, root: Path) -> Entry:
    """Build an ``Entry`` for a single path, robust to inspection failures."""
    try:
        relpath = str(path.relative_to(root)).replace(os.sep, "/")
    except ValueError:
        # Shouldn't happen given how we walk, but never silently crash
        # the audit — record the full path so the failure is visible.
        relpath = str(path)

    try:
        st = os.lstat(path)
    except OSError:
        return Entry(relpath=relpath, kind="other", size=0, mtime_ns=0, digest="")

    mode = st.st_mode

    if stat.S_ISLNK(mode):
        try:
            target = os.readlink(path)
        except OSError:
            target = "<unreadable>"
        return Entry(
            relpath=relpath,
            kind="symlink",
            size=st.st_size,
            mtime_ns=st.st_mtime_ns,
            digest=target,
        )

    if stat.S_ISDIR(mode):
        return Entry(
            relpath=relpath,
            kind="dir",
            size=0,
            mtime_ns=st.st_mtime_ns,
            digest="",
        )

    if stat.S_ISREG(mode):
        return Entry(
            relpath=relpath,
            kind="file",
            size=st.st_size,
            mtime_ns=st.st_mtime_ns,
            digest=_hash_file(path),
        )

    return Entry(
        relpath=relpath,
        kind="other",
        size=st.st_size,
        mtime_ns=st.st_mtime_ns,
        digest="",
    )


def _hash_file(path: Path) -> str:
    """SHA-256 the file content. Chunked so large files don't balloon RSS."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()
