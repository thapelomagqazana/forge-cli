"""StagingResult — the ownership handoff between staging and commit.

Purpose
-------
`StagingEngine.stage()` produces a fully-rendered staging tree. The
verifier inspects it; the commit phase renames its contents into place.
Both phases need:

    * the tree's location on disk,
    * the destination root that will receive the files,
    * a stable manifest (destination path, kind) so downstream code can
      cross-check what was staged against what the plan promised,
    * a way to give up cleanly (destroy) when commit cannot complete.

`StagingResult` is that bundle. It is a small, explicit ownership
record — nothing more.

Why a class and not just a `NamedTuple` or a Pydantic model?
------------------------------------------------------------
Three reasons:

1. **Lifetime.** The result owns a filesystem resource (the staging
   directory). Ownership implies responsibility: someone must clean up
   if the tree cannot be committed. A `NamedTuple` communicates data
   shape, not ownership, and Pydantic models are meant for value
   objects (immutable, serialisable), not resource handles.

2. **Idempotent destruction.** `destroy()` must be safe to call twice
   (success path after commit, then a `finally` block) and must be a
   no-op after the first call. That behaviour is stateful — a value
   type cannot express "am I already destroyed?" without a mutable
   field anyway.

3. **Future evolution.** If commit later needs to record *which* files
   were committed (for `forge rollback`, for example), the result can
   grow a `committed` list without changing its type. A tuple that
   gains fields breaks every positional unpacking; a class that gains
   fields does not.

Why not context-manager-only?
-----------------------------
`StagingResult` is a context manager (`__enter__`/`__exit__`), but its
`__exit__` deliberately does *not* destroy the tree. Reason: the
caller's commit phase runs *between* entering and exiting — auto-
destroy would delete the tree before commit could use it. Ownership is
explicit so the caller decides when the tree stops being useful, and
`destroy()` is a plain method they can call from a `finally` block on
any error path.

Callers must always destroy
---------------------------
The engine has already given up its cleanup responsibility (it renamed
the staging directory out from under the `StagingArea` context
manager). If the caller never calls `destroy()` and never commits, the
tree leaks. Callers should either:

    with StagingEngine().stage(...) as result:
        try:
            verifier.check(result)
            committer.commit(result)   # commits consume the tree
        finally:
            result.destroy()           # idempotent; safe in `finally`

or the `try/finally` form without `with`. Either way, exactly one
`destroy()` call must reach every code path that does not successfully
commit.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType


@dataclass(slots=True)
class StagingResult:
    """The fully-rendered staging tree and everything downstream needs.

    Attributes
    ----------
    root
        Absolute path to the staging directory (already renamed to a
        `*-ready` suffix so it is distinguishable from any in-progress
        staging area that might exist concurrently).
    destination_root
        Absolute path to the target project directory. Stored here so
        the commit phase does not need to re-resolve it; a caller that
        stages and commits in the same session passes the same value
        twice, and this field eliminates the risk of drift between the
        two calls.
    manifest
        Tuple of `(destination_relative_path, kind)` pairs, sorted by
        destination path. `kind` is the string form of `PlannedFileKind`
        (`"render"` or `"copy"`). Downstream code uses this for:

            * verifying that every planned file was produced,
            * choosing whether to re-render (never — the tree is already
              rendered) or copy,
            * producing deterministic reports and diffs.

        The tuple is sorted so downstream sorting is unnecessary and
        logs are stable across runs.
    """

    root: Path
    destination_root: Path
    manifest: tuple[tuple[str, str], ...]

    # Private state: idempotent destroy. `init=False` so callers cannot
    # construct a "pre-destroyed" result by accident; `repr=False` so
    # the flag does not leak into logs and confuse readers who see a
    # staging tree with `_destroyed=True`.
    _destroyed: bool = field(default=False, init=False, repr=False)

    # -- lifecycle --------------------------------------------------------

    def destroy(self) -> None:
        """Remove the staging tree. Idempotent; safe in `finally` blocks.

        Best-effort: `ignore_errors=True` because a failing rmtree here
        must not mask the *real* error the caller is trying to surface.
        The alternative — raising from `destroy()` — turns a commit
        failure into a confusing "commit failed, and then destroy also
        failed, and here is the traceback for the second problem" report
        that hides the first one.

        If the tree survives `destroy()` (e.g. permissions on the parent
        changed after staging), the user will see the hidden directory
        on the next `ls -a` and can remove it manually. That is a worse
        experience than a clean destroy, but a *safer* one than throwing
        from a cleanup path.
        """
        if self._destroyed:
            return
        shutil.rmtree(self.root, ignore_errors=True)
        self._destroyed = True

    @property
    def destroyed(self) -> bool:
        """True once `destroy()` has run. Read-only; used by tests."""
        return self._destroyed

    def __enter__(self) -> StagingResult:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Do nothing on exit — the caller owns the tree's lifetime.

        Deliberately empty *and* deliberately annotated. The three
        parameters match the `contextlib.AbstractContextManager.__exit__`
        protocol exactly:

            * ``exc_type`` — the exception's class, or ``None`` on
              success.
            * ``exc_val`` — the exception instance, or ``None``.
            * ``exc_tb`` — the traceback, or ``None``.

        Returning ``None`` (rather than a truthy value) means "do not
        suppress the exception" — the correct behaviour for a context
        manager that is purely a lifetime marker with no cleanup to
        perform. If a future change *did* want to suppress an
        exception, returning ``True`` would say so; returning ``None``
        is the typed equivalent of the previous unannotated `return
        None` in the old `__exit__`.

        Why ``None`` return, not ``bool``: mypy strict accepts either,
        but ``None`` is what a caller relying on the standard
        context-manager protocol expects — a truthy return value is a
        claim that the exception was *handled*, which this method does
        not make. The annotation ``-> None`` documents that claim.
        """
        return None

    # -- convenience ------------------------------------------------------

    def __len__(self) -> int:
        """Number of staged files. Mirrors `len(result.manifest)`."""
        return len(self.manifest)

    def destinations(self) -> tuple[str, ...]:
        """Sorted destination paths. Convenience for iteration and tests."""
        return tuple(path for path, _kind in self.manifest)

    def find(self, destination: str) -> Path | None:
        """Return the absolute path to a staged file, or None if absent.

        Uses `manifest` as the source of truth so this method cannot
        accidentally return a path that was not in the plan. That
        property matters for commit: a bug where commit iterates the
        filesystem instead of the manifest would let unexpected files
        slip into the destination.
        """
        if not any(path == destination for path, _ in self.manifest):
            return None
        return self.root / destination
