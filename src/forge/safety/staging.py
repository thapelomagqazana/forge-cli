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

Where the staging directory lives
---------------------------------
**In the destination's parent**, not inside the destination itself.

This was not always the case. Earlier versions staged *inside* the
destination, which made "same filesystem" trivially true but required
the destination to be created before staging could begin. That
requirement leaked an empty destination directory on any failed run —
a violation of the "no filesystem mutations during staging" guarantee
(ADR-0019) that only a property-based test (``test_destination_not_
created_when_absent``) would catch.

Staging in the parent fixes the leak without losing atomicity:

    * The parent must exist (the CLI resolves ``--output`` with
      ``resolve_path=True``, which requires the parent to exist), so
      we can create the staging directory there without touching
      anything the user did not ask for.
    * Creating a directory in a parent does not cross filesystems —
      the parent and any future child of it are on the same device
      by construction. ``os.replace`` from a child of the parent into
      another child of the parent is therefore atomic.
    * The engine now has *no code path* that can create the
      destination. It can only write into ``path``, which is a
      sibling of ``root``. The "no mutation before commit" guarantee
      is enforced structurally rather than by a defensive cleanup
      that a future refactor could forget to call.

The commit phase (``_commit_staged`` in ``cli/commands/create.py``)
creates the destination directory as a side effect of the first
``os.replace`` (which does ``mkdir -p`` on the target's parent as
needed). This makes the destination come into existence exactly when
the first file lands in it, and not a moment before — which is what
makes a failed run leave zero trace.

Read-only mode
--------------
`StagingArea.for_root(root, read_only=True)` produces a staging area
that **cannot write**:

    * entering the context manager does *not* create a directory
      (a read-only staging area is a zero-side-effect object),
    * `write_bytes`, `write_text`, and `commit` all raise
      `RuntimeError` before touching the filesystem.

This is the structural half of the "no filesystem mutations during
dry-run" guarantee (ADR-0018). The dry-run service does not construct
a staging area today — but the guarantee is only meaningful if a
future contributor *cannot* accidentally introduce a write inside the
plan code path without tripping a loud, deterministic error at the
call site. A fingerprint-based test (compare the destination tree
before and after) is necessary but not sufficient: a temporary file
that is created and removed within the same dry-run would slip past a
fingerprint taken after the run. The read-only staging area catches
that class, because the write is refused *when it is attempted* —
before it can be created and hidden.

Read-only mode does not weaken the type contract. Callers that take a
`StagingArea` still receive one; they still call the same methods; they
just get a `RuntimeError` from the write methods instead of a file on
disk. That is the correct failure mode: a bug, surfaced loudly.

Ownership transfer (``detach``)
-------------------------------
The staging engine (``forge.staging.engine``) needs to hand the
populated staging directory to the caller after the ``with`` block
exits — the verifier inspects it and the commit phase moves its
contents into the destination. But the ``for_root`` context manager
unconditionally removes the directory in its ``finally`` block. That
is the correct default (cleanup is not optional for failed runs), but
it is wrong for the success path of a caller that explicitly wants to
own the tree.

``StagingArea.detach()`` is the explicit hand-off. Calling it:

    * marks the area so the context manager's ``finally`` skips the
      ``rmtree`` (ownership has transferred — the caller will clean
      up via ``StagingResult.destroy()``),
    * returns the staging directory ``Path`` so the caller can rename
      it to a stable name (e.g. ``*-ready``) before the context
      manager exits,
    * is idempotent — calling it twice returns the same path without
      re-marking, so a caller in a ``finally`` block does not have to
      guard the call.

``detach()`` refuses in read-only mode: a read-only staging area
owns nothing to detach, and silently returning the sentinel path
would hide a caller-side logic error.

The gate is *opt-in at the call site*. The engine calls
``stage.detach()`` only on the success path, *after* structural
self-checks pass. Failure paths never reach it, so the context
manager's ``finally`` performs the cleanup as before. The default is
still "cleanup on exit"; ``detach`` is the one exception, and it is
explicit, typed, and named so that a reader of the caller's code can
see exactly where ownership transfers.

Why same-filesystem
-------------------
`os.replace` is atomic only within a single filesystem. Staging in the
destination's *parent* guarantees that every commit is a true rename:
the staging directory and the destination are both children of the
parent, and no filesystem boundary exists between a directory and its
children. There is no copy, no cross-device link, no "the file exists
but is empty because we were interrupted mid-copy".

The alternative — staging under the system temp directory — would be
simpler to write but is wrong on systems where ``/tmp`` is a separate
filesystem (tmpfs, a mounted volume, some CI runners). ``os.replace``
across devices is not atomic (it raises ``EXDEV`` on POSIX), so the
commit phase would either fail or silently degrade to a non-atomic
copy. Neither is acceptable. Staging in the parent makes the same-
filesystem guarantee true *by construction* rather than by convention.

Why a context manager
---------------------
Cleanup is not optional. If a render fails mid-pipeline, the staging
directory must be removed — otherwise every failed run leaks a hidden
`.forge-stage-XXXXXX/` directory next to the user's project. A context
manager makes cleanup automatic on *all* exit paths, including
exceptions the caller did not anticipate. The `finally` block runs
even if the caller's own `except` clause raises. That is the only way
to guarantee "no debris" as a property, not a promise.

Read-only mode is a special case of the same cleanup discipline:
because it never creates a directory, there is nothing to clean up. The
context manager still yields and returns normally, so a caller does not
need two code paths — `with StagingArea.for_root(root, read_only=True)`
and `with StagingArea.for_root(root)` differ only in what they permit,
not in how they are used.

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

# Sentinel used as the "path" of a read-only staging area. It is
# deliberately not a real filesystem path: no operation should ever
# reach the filesystem through this value, and any attempt to open it
# fails with an obvious error that names the sentinel.
_READ_ONLY_SENTINEL = Path("<read-only-staging>")


@dataclass
class StagingArea:
    """A staging directory that lives *next to* the destination root.

    Construct via ``StagingArea.for_root(root)`` (or with
    ``read_only=True`` for a no-write instance); do not instantiate
    directly. The dataclass is *stateful* — ``_created`` tracks every
    file placed under ``path`` so ``commit()`` knows exactly what to
    move, and ``_detached`` records whether ownership has been handed
    to the caller (so the context manager's cleanup knows to skip
    removal).

    Attributes
    ----------
    path : Path
        The staging directory itself. Files are created inside it
        (never outside — ``_prepare`` enforces this). For read-only
        areas, this is the sentinel ``_READ_ONLY_SENTINEL``; it exists
        so ``StagingArea`` remains a valid object, but no path
        operation on it will ever produce a real file.

        **Location invariant:** ``path`` is a *sibling* of ``root``,
        not a child of it. Both are children of ``root.parent``, so
        ``os.replace`` between them is atomic (same filesystem). The
        engine relies on this sibling relationship to guarantee that
        it cannot accidentally create ``root`` before the commit
        phase — see the module docstring's "Where the staging
        directory lives" section.
    root : Path
        The destination root that staged files will be committed into.
        May or may not exist at construction time; the engine does
        *not* create it. Only the commit phase creates it (via the
        first ``os.replace``, which does ``mkdir -p`` on the target's
        parent as needed).
    _created : list[Path]
        Absolute paths to every file written into staging, in
        **first-write** order. Deduplicated by path: a second write
        to the same ``relpath`` reuses the existing entry rather than
        appending a new one. Cleared on successful (or failed)
        ``commit()`` so a second call is a no-op rather than a
        duplicate move. See the module docstring's "Duplicate
        writes" section for why deduplication-by-path is the
        correct semantics.
    read_only : bool
        When ``True``, the staging area refuses every write and commit
        (raising ``RuntimeError``) and its construction does not create
        a directory. Used by dry-run code paths that must not mutate
        the filesystem under any circumstance. Defaults to ``False``,
        which is the write path.
    _detached : bool
        Internal flag recording whether ``detach()`` has transferred
        ownership of ``path`` to the caller. When ``True``, the
        ``for_root`` context manager skips its final ``rmtree`` —
        cleanup is now the caller's responsibility (via
        ``StagingResult.destroy()``). ``repr=False`` so the flag does
        not appear in logs and confuse a reader who sees a staging
        directory with ``_detached=True``. Not settable at
        construction: ``detach()`` is the only sanctioned way to set
        it, and it refuses on read-only areas (a read-only area owns
        nothing to detach).
    """

    path: Path
    root: Path
    _created: list[Path] = field(default_factory=list)
    read_only: bool = False
    _detached: bool = field(default=False, init=False, repr=False)

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @classmethod
    @contextmanager
    def for_root(cls, root: Path, *, read_only: bool = False) -> Iterator[StagingArea]:
        """Create a staging area anchored to `root`; clean up on exit.

        With ``read_only=False`` (the default), the `finally` clause
        runs on *every* exit path:

            * normal return (after `commit`),
            * normal return after ``detach()`` — except the removal is
              skipped, because the caller now owns ``path``,
            * any exception the caller raised,
            * the context manager protocol receiving `GeneratorExit`.

        `shutil.rmtree(ignore_errors=True)` is safe here because the
        staging directory is *our own scratch space*, created inside
        `root.parent` with a private name. A failure to remove it
        cannot leak user data outside the staging tree, and any file we
        might miss belongs to us, not to the destination.

        With ``read_only=True``, no directory is created and the
        `finally` clause is a no-op (there is nothing to remove). The
        yielded area's ``path`` is the sentinel ``_READ_ONLY_SENTINEL``;
        callers that log ``stage.path`` will see ``<read-only-staging>``
        rather than a real path — which is the correct signal that no
        staging occurred.

        See the module docstring's "Read-only mode" section for why the
        guarantee is enforced structurally rather than only tested
        behaviourally, "Ownership transfer" for the ``detach()``
        exception to the cleanup-on-exit rule, and "Where the staging
        directory lives" for why the staging tree is a sibling of
        ``root`` rather than a child of it.
        """
        # Read-only mode: no directory is created. Building the object
        # without touching the filesystem is the *whole point* — a
        # read-only staging area that still called `mkdtemp` would
        # violate its own contract at the first call.
        if read_only:
            area = cls(
                path=_READ_ONLY_SENTINEL,
                root=root,
                _created=[],
                read_only=True,
            )
            try:
                yield area
            finally:
                # Nothing to clean up. The `try/finally` still surrounds
                # the yield so callers can rely on cleanup-shaped
                # context management (any `__exit__` bookkeeping they
                # perform in *their* own finally runs identically in
                # both modes).
                pass
            return

        # Write mode: create the staging directory *next to* `root`,
        # in `root.parent`. See the module docstring's "Where the
        # staging directory lives" section for why this is not
        # `root` itself.
        #
        # The parent must exist; the CLI resolves `--output` with
        # `resolve_path=True` which requires the parent to exist, and
        # `mkdtemp(dir=parent)` requires it too. If the caller passed
        # a path whose parent does not exist, that is a caller bug —
        # silently creating the parent would compound the very class
        # of mutation this design exists to prevent.
        staging_parent = root.parent
        if not staging_parent.exists():
            raise FileNotFoundError(
                f"staging parent does not exist: {staging_parent}. "
                "The destination's parent directory must exist before "
                "staging."
            )
        stage_dir = Path(tempfile.mkdtemp(prefix=_STAGING_PREFIX, dir=str(staging_parent)))
        area = cls(path=stage_dir, root=root, _created=[], read_only=False)
        try:
            yield area
        finally:
            # Ownership transfer: if the caller has detached, the
            # staging directory no longer belongs to us. Skip the
            # removal — the caller's ``StagingResult.destroy()`` (or
            # equivalent) is now responsible for cleanup, and its
            # lifetime is the caller's to manage.
            #
            # This is the one exception to "cleanup is not optional."
            # It is opt-in via ``detach()``, which the engine calls
            # *only* on the success path — failure paths never reach
            # it, so the default behaviour (cleanup on any exit) is
            # preserved for the overwhelming majority of runs.
            if not area._detached:
                # Best-effort removal. If this fails, the staging
                # directory remains — hidden, named, and safe to
                # delete manually. Raising from a `finally` block
                # would mask the caller's original exception, which
                # is the more important failure.
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

        On a read-only staging area, this method raises ``RuntimeError``
        before touching the filesystem. That is the intended failure
        mode — a caller that reaches a write call in a read-only
        context has a bug, and the error names the staging area, not
        the (nonexistent) file.

        After ``detach()``, writes still work: ownership transfer
        changes who cleans up the directory, not who can write to it.
        This is deliberate — the engine may detach before a late
        write in a future refactor, and the write semantics should
        not depend on the cleanup ownership.

        Returns the absolute path of the staged file. Callers do not
        need this for `commit()` (which tracks staged files itself),
        but tests and dry-run renderers benefit from being able to read
        back exactly what they wrote.
        """
        self._refuse_if_read_only("write_bytes")
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
        """UTF-8 convenience wrapper around `write_bytes`.

        Guarded by the same read-only refusal as `write_bytes` —
        calling this on a read-only area raises ``RuntimeError``
        *before* delegating, so a future refactor of the delegation
        cannot accidentally bypass the guard.
        """
        self._refuse_if_read_only("write_text")
        return self.write_bytes(relpath, text.encode("utf-8"))

    # ------------------------------------------------------------------
    # Ownership
    # ------------------------------------------------------------------

    def detach(self) -> Path:
        """Transfer ownership of the staging directory to the caller.

        The caller must call this *before* the ``for_root`` context
        manager exits. Once detached, the context manager will skip
        its final ``rmtree`` — the caller is responsible for cleanup
        (typically via ``StagingResult.destroy()`` after commit).

        Returns the staging directory ``Path`` so the caller can
        rename it to a stable name (e.g. ``*-ready``) before the
        context manager exits. The engine uses this to hand a
        recognisable tree to the verifier and commit phases, which
        run *after* ``for_root`` has exited.

        Why the caller must rename before exit
        --------------------------------------
        If the caller simply holds onto ``path`` without renaming it,
        another invocation of ``StagingArea.for_root`` with the same
        root could in principle create a colliding directory name
        (``mkdtemp`` makes that practically impossible, but "in
        principle" is what invariants are for). Renaming to a
        distinguishable suffix makes the ownership transfer visible
        in ``ls -a`` and immune to any future ``mkdtemp`` behaviour
        change.

        Idempotence
        -----------
        Calling ``detach()`` twice returns the same path without
        re-marking. A caller in a ``finally`` block that has already
        detached on the success path can call ``detach()`` again
        without having to guard the call — same pattern as
        ``StagingResult.destroy()``.

        Read-only refusal
        -----------------
        A read-only staging area owns nothing to detach; the sentinel
        path is not a directory, so there is nothing for the caller
        to rename or clean up. Silently returning the sentinel would
        hide a caller-side logic error — the caller thinks it has
        taken ownership of a real tree, and later calls
        ``shutil.rmtree`` on a nonexistent path and fails with a
        confusing error far from the mistake. Raising ``RuntimeError``
        here names the problem at the call site, which is where a
        caller can fix it.

        After-commit behaviour
        ----------------------
        ``commit()`` clears ``_created`` and moves every staged file
        out of the staging directory. Detaching after a successful
        commit is *possible* but yields an empty tree with nothing
        for the caller to clean up beyond the directory itself. The
        engine does not do this — it detaches *before* the commit
        phase (which runs in a later module), and the commit phase
        owns the result. The method does not refuse after commit
        because there is no correctness issue: the caller may
        legitimately want to inspect the (now empty) staging
        directory's metadata for logging.

        Errors
        ------
        Raises ``RuntimeError`` if this staging area is read-only.
        """
        if self.read_only:
            raise RuntimeError(
                "StagingArea.detach() refused: this staging area is "
                "read-only. A read-only area owns no directory to "
                "detach; if you need to hand off a populated tree, "
                "use StagingArea.for_root(root) (read_only=False)."
            )
        # Idempotent: the second call is a no-op that returns the same
        # path. This makes `finally` blocks safe.
        self._detached = True
        return self.path

    @property
    def detached(self) -> bool:
        """True once ``detach()`` has transferred ownership.

        Read-only; used by the engine to assert that it never leaks a
        staging directory (the context manager's cleanup is skipped
        iff this is ``True``, and only the success path sets it).
        """
        return self._detached

    # ------------------------------------------------------------------
    # Commit
    # ------------------------------------------------------------------

    def commit(self) -> list[Path]:
        """Move every staged file to its final destination.

        Moves happen one file at a time, each via `os.replace` — atomic
        within the same filesystem (guaranteed by ``path`` and ``root``
        being siblings under ``root.parent``). A failure mid-commit
        leaves the files that have already been moved in place (they
        were moved atomically, so they are complete) and raises before
        the failed move, so the files after it are not touched. The
        staging directory is still cleaned up by the context manager's
        `finally`; the caller can inspect the destination to see what
        succeeded and re-run for the rest.

        ``_created`` is deduplicated by path (see ``write_bytes``), so
        a duplicate write commits the file exactly once. The commit
        order matches the *first-write* order, which is what a caller
        that logs "wrote a, wrote b, wrote c" expects.

        On a read-only staging area, this method raises
        ``RuntimeError`` — even though there is nothing to commit,
        a caller invoking ``commit()`` on a read-only area is
        expressing an intent that contradicts the area's contract.
        Silently returning ``[]`` would hide the bug; raising makes
        it visible.

        Detach interaction
        ------------------
        Committing a *detached* area is allowed and does not affect
        the ``_detached`` flag. The two operations answer different
        questions ("where do the files go?" vs. "who cleans up the
        directory?"), and a caller that has already committed may
        still want to keep the (now empty) staging directory around
        for logging or debugging. The engine does not exercise this
        combination — it detaches, hands the tree to the verifier and
        commit phases, and those phases own the ``StagingResult``
        rather than calling ``StagingArea.commit()`` directly. The
        method is nonetheless safe under either ordering because
        "commit detaches?" and "detach commits?" are not invariants,
        they are independent operations.

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
            nothing was staged (harmless no-op in write mode).
        """
        self._refuse_if_read_only("commit")
        committed: list[Path] = []
        try:
            for staged in self._created:
                rel = staged.relative_to(self.path)
                final = self.root / rel
                # Ensure the destination directory (and any
                # intermediate directories on the way to the target)
                # exists. This is the *only* place the staging layer
                # creates anything under ``root`` — and it does so
                # only for a target whose file is about to be
                # committed into it. If a run fails before reaching
                # this line, ``root`` was never created. If it fails
                # after, the directories that exist contain complete
                # files (each ``os.replace`` is atomic).
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

    def _refuse_if_read_only(self, method: str) -> None:
        """Raise a clear RuntimeError if this staging area is read-only.

        Centralised so every write path has identical refusal semantics
        and message shape. Adding a new write method in the future means
        calling this guard at the top of the method — one line, one
        place to review, one failure mode to test.

        The error message names the method (so a stack trace's *first*
        line says which entry point was reached) and the mode (so the
        reader does not have to look up which staging area this is).
        That combination is what makes a regression to the dry-run path
        obvious at the point of failure, rather than appearing as
        mysterious missing files later.
        """
        if self.read_only:
            raise RuntimeError(
                f"StagingArea.{method}() refused: this staging area is "
                "read-only. The dry-run path must not modify the "
                "filesystem; use StagingArea.for_root(root) "
                "(read_only=False) to obtain a writable staging area."
            )

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
