"""Staging discipline — writes never go directly to the destination.

The one rule
------------
Every file produced by generation is written into a temporary directory
on the *same filesystem* as the destination, then committed atomically
as a whole. If anything fails before commit, the destination is
untouched. If anything fails *during* commit, the files that have
already been moved stay moved, and the failures are reported — but no
partial file is ever visible in the destination.

Why this exists
---------------
Without staging, a rendering failure on file 47 of 50 leaves the
destination with 46 new files, one broken file, and no clear way to
know what was and was not written. Users then cannot simply re-run
`forge create` — some files exist, so collisions fire; some do not, so
the run is incomplete. Staging eliminates this entire class of
half-completed state.

The generation pipeline is *typed* to accept a `StagingArea`, not a
`Path`. Downstream code cannot accidentally write to the destination
directly: the only way to produce a file is `stage.write_*()`, and the
only way to reveal it is `stage.commit()`. Bypassing staging is a type
error, not a code-review note.

Why same-filesystem
-------------------
`os.replace` is atomic only within a single filesystem. Staging on the
same filesystem as the destination root guarantees that every commit
is a true rename — no copy, no cross-device link, no "the file exists
but is empty because we were interrupted mid-copy". `mkdtemp` under
the destination root is the simplest way to guarantee this; on any
normal system, the staging directory is on the same device as the
root.

Why a context manager
---------------------
Cleanup is not optional. If a render fails mid-pipeline, the staging
directory must be removed — otherwise every failed run leaks a hidden
`.forge-stage-XXXXXX/` directory next to the user's project. A context
manager makes cleanup automatic on *all* exit paths, including
exceptions the caller did not anticipate. The `finally` block runs
even if the caller's own `except` clause raises. That is the only way
to guarantee "no debris" as a property, not a promise.

Why not `TemporaryDirectory`
----------------------------
`tempfile.TemporaryDirectory` is close to what we want, but it hides
the lifecycle behind a `__del__` and a finalizer chain — cleanup can
be deferred to garbage collection, which is fine for tests and wrong
for CLI users who are watching their disk. An explicit context
manager with an explicit `finally` gives us deterministic cleanup on
the same tick as the exception. It also lets us own the directory
naming convention (`.forge-stage-`) so users can recognise our
staging directories in `ls -a`.

Duplicate writes: last-write-wins, one commit
---------------------------------------------
A caller may write the same `relpath` twice (a blueprint with two
`files[]` entries targeting the same destination, a re-render after a
correction, a pipeline that writes a placeholder and then overwrites
it). The staging area handles this without error:

    * The *content* is last-write-wins — ``open(target, "wb")``
      truncates, so the second write replaces the first.
    * The *entry* in ``_created`` is deduplicated by staged path —
      the second write reuses the existing entry rather than
      appending a duplicate.
    * ``commit`` therefore moves the file exactly once, and the
      destination receives the last write's content.

This matches the behaviour of writing directly to the destination:
two writes to the same path leave the second one. The staging area
is a *transparent* layer — it does not impose its own semantics on
duplicate writes, it inherits the filesystem's.

An earlier version of this module did *not* deduplicate
``_created``. It appended the same path twice, and ``commit``
crashed on the second ``os.replace`` with ``FileNotFoundError``
(the first move had already removed the source). That was a bug,
not a design choice — the module's contract ("staging is a
transparent layer") was violated, and the fix is the deduplication
described above.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

# The prefix for every staging directory we create. Leading dot keeps it
# out of plain `ls`; the `.forge-` prefix makes it recognisable in `ls -a`
# and easy for users to identify if they ever need to clean up manually.
_STAGING_PREFIX = ".forge-stage-"


@dataclass
class StagingArea:
    """A staging directory that lives next to the destination root.

    Construct via `StagingArea.for_root(root)`; do not instantiate
    directly. The dataclass is *stateful* — `_created` tracks every
    file placed under `path` so `commit()` knows exactly what to move.

    Attributes
    ----------
    path : Path
        The staging directory itself. Files are created inside it
        (never outside — `_prepare` enforces this).
    root : Path
        The destination root that staged files will be committed into.
        Must be an ancestor of every final destination path.
    _created : list[Path]
        Absolute paths to every file written into staging, in
        **first-write** order. Deduplicated by path: a second write
        to the same ``relpath`` reuses the existing entry rather than
        appending a new one. Cleared on successful (or failed)
        ``commit()`` so a second call is a no-op rather than a
        duplicate move. See the module docstring's "Duplicate
        writes" section for why deduplication-by-path is the
        correct semantics.
    """

    path: Path
    root: Path
    _created: list[Path] = field(default_factory=list)

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @classmethod
    @contextmanager
    def for_root(cls, root: Path) -> Iterator[StagingArea]:
        """Create a staging area anchored to `root`; clean up on exit.

        The `finally` clause runs on *every* exit path:

            * normal return (after `commit`),
            * any exception the caller raised,
            * the context manager protocol receiving `GeneratorExit`.

        `shutil.rmtree(ignore_errors=True)` is safe here because the
        staging directory is *our own scratch space*, created inside
        `root` with a private name. A failure to remove it cannot leak
        user data outside the staging tree, and any file we might miss
        belongs to us, not to the destination.
        """
        root.mkdir(parents=True, exist_ok=True)
        stage_dir = Path(tempfile.mkdtemp(prefix=_STAGING_PREFIX, dir=str(root)))
        area = cls(path=stage_dir, root=root, _created=[])
        try:
            yield area
        finally:
            # Best-effort removal. If this fails, the staging directory
            # remains — hidden, named, and safe to delete manually.
            # Raising from a `finally` block would mask the caller's
            # original exception, which is the more important failure.
            shutil.rmtree(stage_dir, ignore_errors=True)

    # ------------------------------------------------------------------
    # Write APIs
    # ------------------------------------------------------------------

    def write_bytes(self, relpath: str, data: bytes) -> Path:
        """Write bytes to `relpath` inside the staging directory.

        `relpath` is treated as *relative to the staging root*, not the
        process cwd. Any attempt to escape the staging tree (via `..`,
        absolute paths, or symlinks) raises `ValueError` — the staging
        area does not trust its caller, because one buggy caller
        bypassing safety is all it takes to break the guarantee.

        A second write to the same `relpath` replaces the staged
        *content* (via ``open(..., "wb")``'s truncation) and reuses
        the existing entry in ``_created``. ``commit()`` will move
        the file once, with the last write's content. See the module
        docstring's "Duplicate writes" section.

        Returns the absolute path of the staged file. Callers do not
        need this for `commit()` (which tracks staged files itself),
        but tests and dry-run renderers benefit from being able to read
        back exactly what they wrote.
        """
        target = self._prepare(relpath)
        with open(target, "wb") as fh:
            fh.write(data)
        # Deduplicate by staged path. ``_prepare`` resolves ``relpath``
        # against the staging root, so two different spellings of the
        # same destination (``"file.txt"`` and ``"./file.txt"``) produce
        # the same ``target`` — and are correctly recognised as one
        # file.
        #
        # The ``in`` check is O(n) on the current list, which is fine
        # for the scale this runs at (a few dozen files per scaffold
        # at most). A parallel set would be faster but would require
        # keeping two containers in sync, and the O(n) check is cheap
        # enough that the simpler code is worth the negligible cost.
        if target not in self._created:
            self._created.append(target)
        return target

    def write_text(self, relpath: str, text: str) -> Path:
        """UTF-8 convenience wrapper around `write_bytes`."""
        return self.write_bytes(relpath, text.encode("utf-8"))

    # ------------------------------------------------------------------
    # Commit
    # ------------------------------------------------------------------

    def commit(self) -> list[Path]:
        """Move every staged file to its final destination.

        Moves happen one file at a time, each via `os.replace` — atomic
        within the same filesystem. A failure mid-commit leaves the
        files that have already been moved in place (they were moved
        atomically, so they are complete) and raises before the failed
        move, so the files after it are not touched. The staging
        directory is still cleaned up by the context manager's
        `finally`; the caller can inspect the destination to see what
        succeeded and re-run for the rest.

        ``_created`` is deduplicated by path (see ``write_bytes``), so
        a duplicate write commits the file exactly once. The commit
        order matches the *first-write* order, which is what a caller
        that logs "wrote a, wrote b, wrote c" expects.

        Why not roll back on partial commit failure?
            Rolling back means undoing successful atomic renames,
            which means copying the *original* destination files back
            over the new ones. That is more likely to fail (the
            destination now has our files) and risks data loss if the
            original files were not preserved. Callers who want full
            transactional semantics use `--overwrite` (which creates
            backups before staging) and can restore from those. The
            per-file atomicity here is the guarantee; cross-file
            transactionality is not claimed.

        Backups of pre-existing destinations are the *caller's*
        responsibility (see `overwrite.backup_existing`). Keeping
        backups and staging in separate modules means each has one job,
        and the caller explicitly opts into preservation semantics for
        the files that need it.

        Returns
        -------
        list[Path]
            The final destination paths, in commit order. Empty when
            nothing was staged (harmless no-op).
        """
        committed: list[Path] = []
        try:
            for staged in self._created:
                rel = staged.relative_to(self.path)
                final = self.root / rel
                # Ensure the final directory exists. This is required
                # because a blueprint may create a nested path whose
                # parent directory was not itself a staged file.
                final.parent.mkdir(parents=True, exist_ok=True)
                os.replace(staged, final)
                committed.append(final)
        finally:
            # Clear the tracked set even on failure. A partial commit
            # means the moved files are no longer in staging; keeping
            # them in `_created` would cause a second `commit()` to
            # raise on the moved files. Clearing makes the object state
            # consistent with the filesystem state.
            self._created.clear()
        return committed

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _prepare(self, relpath: str) -> Path:
        """Resolve `relpath` inside staging; reject anything that escapes.

        This is the *only* place a path becomes a staging file. Every
        write method funnels through here, so path safety for staging
        is one function with one rule: the resolved target must be a
        descendant of the resolved staging root.

        Note that we do not delegate to `safety.paths.safe_resolve`
        here. The rules are similar, but the *question* is different:

            * `safe_resolve` asks "is this path safe relative to a
              user-chosen root?" — and returns a `Finding` for
              aggregation.
            * `_prepare` asks "is this a valid staging path?" — and
              raises `ValueError` because a buggy caller bypassing
              safety is a programming error, not a user-facing one.

        Using `safe_resolve` here would also create a circular
        dependency (`paths` imports nothing from staging, but
        conceptually staging depends on paths). Keeping `_prepare`
        self-contained means staging has zero dependencies on other
        safety modules, and its correctness is auditable in isolation.
        """
        if not relpath:
            raise ValueError("staging relpath must be non-empty")
        if "\x00" in relpath:
            raise ValueError(f"staging relpath contains NUL: {relpath!r}")

        resolved_root = self.path.resolve(strict=False)
        # `resolve(strict=False)` on the target handles `..` and any
        # symlinks the caller might have introduced *within* staging
        # (there should be none, but defence in depth).
        target = (resolved_root / relpath).resolve(strict=False)
        try:
            target.relative_to(resolved_root)
        except ValueError:
            raise ValueError(f"staging write escapes staging area: {relpath!r}") from None

        # Reject the staging root itself as a target — that would mean
        # writing "to the directory", which is never what a caller
        # wants and would produce confusing errors at commit time.
        if target == resolved_root:
            raise ValueError(f"staging relpath resolves to the staging root itself: {relpath!r}")

        target.parent.mkdir(parents=True, exist_ok=True)
        return target
