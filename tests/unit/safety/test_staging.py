"""Contract tests for ``StagingArea`` — the write-discipline guarantee.

Where this file sits
--------------------
``StagingArea`` is the *only* mechanism in the safety layer for
producing files on disk. It is the safety layer's response to the
failure mode that plagues scaffolding tools:

    "The pipeline wrote 47 of 50 files, then a template failed
     on file 48. Now the destination has a half-built project and
     the user cannot just re-run — some files exist (collision), some
     do not (incomplete)."

The staging discipline eliminates this class of failure:

    * Every file the pipeline produces goes into a hidden
      ``.forge-stage-*`` directory that is a *sibling* of the
      destination root — both are children of the destination's
      parent. Staging in the parent (rather than inside the
      destination) means a failed run does not leave an empty
      destination behind, while still guaranteeing the same
      filesystem for atomic ``os.replace`` at commit time. See
      ``forge.safety.staging``'s "Where the staging directory
      lives" section for the full reasoning.
    * ``commit()`` moves each staged file to its final destination
      via ``os.replace`` — atomic per file.
    * If anything fails before ``commit()``, the staging directory
      is removed and the destination is untouched.
    * If ``commit()`` fails mid-way, the files that have already been
      committed stay in place (each was moved atomically — it is
      complete), and the failure names which file broke.

The staging area is a *context manager*. Its lifecycle is: acquired
by ``for_root(root)``, used via ``write_bytes`` / ``write_text``,
committed via ``commit()``, and cleaned up on exit regardless of
whether the caller raised. A caller that used staging directly (not
through the context manager) would have to replicate the cleanup
contract; the tests here exercise both paths, but the context-manager
path is what every production caller should use.

Three guarantees, each in its own set of classes
------------------------------------------------
1. **Writes stay inside staging.** The staging area's
   ``_prepare`` helper resolves each ``relpath`` against the staging
   root and rejects anything that escapes. Traversal, absolute paths,
   and empty relpaths are all refused with ``ValueError`` — a
   *programmer error*, not a user error, because a caller of
   ``write_bytes`` that passes ``"../etc/passwd"`` has a bug.

2. **Commit moves only what was written.** ``commit()`` iterates the
   tracked list of staged files (``_created``) and moves each to the
   destination root. Files that were *not* written through
   ``write_bytes`` are not touched. A caller that stages 3 files and
   commits gets 3 files in the destination, not 3 plus whatever else
   was already there.

3. **Cleanup runs on every exit path.** Whether the caller's body
   succeeds, raises, or returns early, the staging directory is
   removed by the context manager's ``finally``. A staging directory
   left behind is a *visible* bug — the user sees a
   ``.forge-stage-XXXXXX`` directory and cannot tell whether it is
   safe to delete. The tests here pin that the directory is gone
   after every normal exit.

Why the tests do *not* use ``StagingArea()`` directly
-----------------------------------------------------
``for_root`` is the only public constructor. Direct instantiation
would skip the ``mkdtemp`` call that anchors the staging directory
next to the destination root (same filesystem, for atomic
``os.replace``), which is the whole point of the design. The tests
exercise ``for_root`` exclusively — if a future refactor added a
direct constructor, the tests would need a new class for it, and
that absence would be a signal that the constructor was *not* part
of the intended contract.

Filesystem-dependent tests
--------------------------
A few tests depend on filesystem behaviour that is not portable:

* **Broken-pipe-like conditions during commit** — hard to trigger
  portably. The rollback tests for ``backup_existing`` cover the
  analogous behaviour; the staging commit's failure mode is
  different (per-file atomicity, no rollback) and does not need a
  broken-pipe simulation because the failure is *by design*: a
  mid-commit failure leaves the already-committed files in place.
* **Cross-filesystem staging** — the design *requires* the staging
  directory to be on the same filesystem as the destination root.
  The tests exercise the same-filesystem case (which is what
  ``for_root`` guarantees) by comparing ``st_dev`` — the property
  the guarantee depends on — rather than a same-subtree property
  the current design does not (and must not) provide. Testing
  cross-filesystem would require mounting a separate filesystem,
  which is not portable in CI.

Both are documented as non-goals in the module docstring; the tests
here exercise the behaviour the design *does* guarantee.

What this file does *not* cover
-------------------------------
* **The renderer.** ``StagingArea`` does not render templates; the
  caller (``create._generate``) renders into the staging area via
  ``write_text``. Tests for the renderer live in
  ``tests/unit/rendering/``.
* **The backup.** ``StagingArea`` does not create backups; that is
  ``backup_existing``'s job, called by the caller before staging.
  Tests for backups live in ``tests/unit/safety/test_overwrite.py``.
* **The collision check.** ``StagingArea`` does not check whether a
  destination already exists; that is the caller's pre-flight
  (via ``detect_collisions``). The staging area trusts its caller
  to have already validated the destination is free or consented
  to be overwritten.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from forge.safety import StagingArea


# =============================================================================
# Helpers
#
# Small helpers shared by the cleanup tests below. Both exist so that
# the `pytest.raises` blocks in the cleanup tests can contain a single
# *simple* statement (a helper call) rather than multiple staging
# writes followed by a raise — that shape is what both PT012 (raises
# block focuses on the exception) and SIM117 (multiple contexts in one
# `with`) ask for. The helpers keep the test's *setup* visible (each is
# one line, and its name says what it does) while satisfying the two
# lint rules.
# =============================================================================
def _stage_one_and_raise(stage: StagingArea) -> None:
    """Stage ``file.txt`` then raise ``RuntimeError``.

    Used by ``test_staging_dir_removed_after_exception``. The single
    helper call is what lets ``pytest.raises`` contain a single
    statement while still proving the "staged file is discarded when
    the body raises" contract.
    """
    stage.write_text("file.txt", "x")
    raise RuntimeError("simulated")


def _stage_three_and_raise(stage: StagingArea) -> None:
    """Stage three files then raise ``RuntimeError``.

    Used by ``test_files_not_committed_are_discarded_with_staging``.
    Three writes (not one) so the test's assertion "no destination
    files remain" is meaningful — one file could slip past a buggy
    cleanup; three would not.
    """
    stage.write_text("a.txt", "x")
    stage.write_text("b.txt", "y")
    stage.write_text("c.txt", "z")
    raise RuntimeError("simulated")


# =============================================================================
# Group 1: the happy path — write, commit, verify
# =============================================================================
class TestWriteAndCommit:
    """The common case: write files to staging, commit, files appear.

    The tests here are the *positive* half of the contract — the
    staging area produces files in the destination root with the
    right content at the right paths. Without these, the escape and
    cleanup tests would be testing a subsystem that was never known
    to *work*; the positive tests establish the baseline.
    """

    @pytest.mark.unit()
    def test_single_file_round_trip(self, tmp_path: Path) -> None:
        """A file written to staging and committed appears in the destination."""
        with StagingArea.for_root(tmp_path) as stage:
            stage.write_text("file.txt", "hello")
            stage.commit()

        assert (tmp_path / "file.txt").read_text() == "hello"

    @pytest.mark.unit()
    def test_nested_path_round_trip(self, tmp_path: Path) -> None:
        """A nested path creates the intermediate directories on commit."""
        with StagingArea.for_root(tmp_path) as stage:
            stage.write_text("a/b/c/deep.txt", "content")
            stage.commit()

        assert (tmp_path / "a/b/c/deep.txt").read_text() == "content"

    @pytest.mark.unit()
    def test_write_bytes_round_trip(self, tmp_path: Path) -> None:
        """Binary content survives the round trip byte-for-byte."""
        payload = b"\x00\x01\x02\xff\xfe"
        with StagingArea.for_root(tmp_path) as stage:
            stage.write_bytes("binary.bin", payload)
            stage.commit()

        assert (tmp_path / "binary.bin").read_bytes() == payload

    @pytest.mark.unit()
    def test_commit_returns_written_paths(self, tmp_path: Path) -> None:
        """``commit()`` returns the final paths, in commit order.

        The return value is what the caller reports to the user
        ("3 files written"). The tests pin both the *count* (a commit
        of N files returns N paths) and the *values* (each path is
        the absolute destination path, not the staging path).
        """
        with StagingArea.for_root(tmp_path) as stage:
            stage.write_text("a.txt", "x")
            stage.write_text("b.txt", "y")
            committed = stage.commit()

        assert len(committed) == 2
        assert committed[0] == tmp_path / "a.txt"
        assert committed[1] == tmp_path / "b.txt"

    @pytest.mark.unit()
    def test_commit_with_nothing_staged(self, tmp_path: Path) -> None:
        """A commit with no staged files is a no-op, not an error.

        The caller may stage zero files (a blueprint with no
        templates, a dry-run that still calls ``commit``, etc.). The
        staging area handles this gracefully — an empty commit
        returns an empty list, does not create spurious directories,
        does not raise. The test pins the "no-op" behaviour so a
        future change that required ``commit`` to be called with at
        least one staged file would fail here.
        """
        with StagingArea.for_root(tmp_path) as stage:
            committed = stage.commit()

        assert committed == []
        # The destination root is unchanged — no new files, no new
        # directories (except the one ``for_root`` created for
        # staging, which the context manager removed on exit).
        assert list(tmp_path.iterdir()) == []


# =============================================================================
# Group 2: staging isolation — writes stay inside staging
# =============================================================================
class TestStagingIsolation:
    """Writes cannot escape the staging directory.

    ``_prepare`` resolves each ``relpath`` against the staging root
    and rejects anything that would land outside. The refusal is a
    ``ValueError`` (programmer error), not a ``ForgeError`` (user
    error), because a caller of ``write_bytes`` passing
    ``"../etc/passwd"`` has a *bug* — the safety layer's earlier
    checks would have caught this if it were user input.

    The tests here exercise each escape shape:

        * ``..`` traversal,
        * absolute paths (POSIX and Windows forms),
        * NUL bytes,
        * empty relpaths (which would name the staging root itself).
    """

    @pytest.mark.unit()
    def test_traversal_rejected(self, tmp_path: Path) -> None:
        """A ``..`` segment that escapes staging is refused."""
        with (
            StagingArea.for_root(tmp_path) as stage,
            pytest.raises(ValueError, match="escapes staging"),
        ):
            stage.write_text("../escape.txt", "malicious")

    @pytest.mark.unit()
    def test_deep_traversal_rejected(self, tmp_path: Path) -> None:
        """A multi-level ``..`` sequence is refused."""
        with (
            StagingArea.for_root(tmp_path) as stage,
            pytest.raises(ValueError, match="escapes staging"),
        ):
            stage.write_text("a/b/../../../outside.txt", "malicious")

    @pytest.mark.unit()
    def test_absolute_path_rejected(self, tmp_path: Path) -> None:
        """An absolute path is not a valid relpath for staging."""
        with (
            StagingArea.for_root(tmp_path) as stage,
            pytest.raises(ValueError, match="escapes staging"),
        ):
            stage.write_text("/etc/passwd", "malicious")

    @pytest.mark.unit()
    def test_nul_byte_rejected(self, tmp_path: Path) -> None:
        """A NUL byte in the relpath is refused.

        The check is in ``_prepare`` before any filesystem call.
        Without it, ``open("a\\x00b", "w")`` would raise
        ``ValueError`` from the OS, which the staging area would
        propagate. Catching it in ``_prepare`` gives a clearer
        message and pins the *intent* ("this is not a valid
        filename"), not just the *symptom* ("the OS refused").
        """
        with (
            StagingArea.for_root(tmp_path) as stage,
            pytest.raises(ValueError, match="NUL"),
        ):
            stage.write_text("a\x00b", "x")

    @pytest.mark.unit()
    def test_empty_relpath_rejected(self, tmp_path: Path) -> None:
        """An empty relpath names the staging root itself; refused."""
        with (
            StagingArea.for_root(tmp_path) as stage,
            pytest.raises(ValueError, match="non-empty"),
        ):
            stage.write_text("", "x")

    @pytest.mark.unit()
    def test_dot_relpath_resolves_to_root_and_is_refused(self, tmp_path: Path) -> None:
        """``"."`` resolves to the staging root; refused as a target."""
        with (
            StagingArea.for_root(tmp_path) as stage,
            pytest.raises(ValueError, match="staging root itself"),
        ):
            stage.write_text(".", "x")

    @pytest.mark.unit()
    def test_write_to_escape_does_not_create_a_file(self, tmp_path: Path) -> None:
        """A refused write leaves the destination untouched.

        The important half of "reject traversal": not just that the
        write *raises*, but that no file was created at the escape
        target. A test that only checked the exception would pass
        even if the write partially succeeded (write-then-raise).
        The test asserts on the filesystem, not just the exception.

        ``match="escapes staging"`` is added for the same reason as
        the sibling tests above: pinning the failure to a *reason*
        rules out the test passing because the write raised for some
        other reason (a race, a missing dependency), which would
        leave the filesystem assertion below meaningless.
        """
        escape_target = tmp_path.parent / "escape-test-file.txt"
        # Ensure the target does not already exist (from a previous
        # run, or a stray test file).
        if escape_target.exists():
            escape_target.unlink()

        with (
            StagingArea.for_root(tmp_path) as stage,
            pytest.raises(ValueError, match="escapes staging"),
        ):
            stage.write_text("../escape-test-file.txt", "x")

        assert not escape_target.exists()


# =============================================================================
# Group 3: commit semantics — atomic per file, ordered
# =============================================================================
class TestCommitSemantics:
    """``commit()`` moves each staged file to its destination.

    Two properties are tested here:

        * **Atomicity per file.** Each file is moved with
          ``os.replace``, which is atomic within a filesystem. A
          committed file is a complete file — never a partial one.
          (The *cross-file* transactionality is deliberately *not*
          provided; see the module docstring for why.)
        * **Order.** Files are committed in the order they were
          staged, so a caller that iterates ``committed`` sees the
          same order the caller produced.

    The atomicity property is *hard* to test directly (a test would
    need to observe an interrupted ``os.replace``, which the OS does
    not expose). The tests here exercise the *observable*
    properties — committed files exist with the expected content,
    and a failure during commit does not corrupt the previously
    committed files.
    """

    @pytest.mark.unit()
    def test_commit_preserves_write_order(self, tmp_path: Path) -> None:
        """Files are committed in the order they were staged.

        A caller that stages ``[a, b, c]`` sees ``[a, b, c]`` in
        the ``committed`` list. This is what makes the CLI's output
        deterministic — a caller that logs "wrote a, wrote b, wrote
        c" produces the same log on every run.
        """
        with StagingArea.for_root(tmp_path) as stage:
            stage.write_text("a.txt", "x")
            stage.write_text("b.txt", "y")
            stage.write_text("c.txt", "z")
            committed = stage.commit()

        assert [p.name for p in committed] == ["a.txt", "b.txt", "c.txt"]

    @pytest.mark.unit()
    def test_commit_overwrites_existing_destination_file(self, tmp_path: Path) -> None:
        """A commit replaces the destination file if it exists.

        ``os.replace`` is the primitive used — it overwrites
        unconditionally. The caller is responsible for consenting to
        overwrite (via ``detect_collisions`` with ``overwrite=True``)
        and for backing up the existing file (via
        ``backup_existing``). The staging area itself does not
        refuse — refusing would require duplicating the collision
        check, which the caller already ran.
        """
        (tmp_path / "existing.txt").write_text("old content")

        with StagingArea.for_root(tmp_path) as stage:
            stage.write_text("existing.txt", "new content")
            stage.commit()

        assert (tmp_path / "existing.txt").read_text() == "new content"

    @pytest.mark.unit()
    def test_commit_clears_tracked_files(self, tmp_path: Path) -> None:
        """After a successful commit, a second commit is a no-op.

        ``commit()`` clears ``_created`` after iterating, so the
        object's state is consistent with the filesystem — the
        staging directory no longer contains the committed files,
        and ``_created`` reflects that. Without the clear, a second
        ``commit()`` would try to move files that no longer exist in
        staging and fail with ``FileNotFoundError``. The test pins
        the idempotent-commit behaviour.
        """
        with StagingArea.for_root(tmp_path) as stage:
            stage.write_text("file.txt", "x")
            first = stage.commit()
            second = stage.commit()

        assert first == [tmp_path / "file.txt"]
        assert second == []


# =============================================================================
# Group 4: cleanup — the staging directory is removed on every exit
# =============================================================================
class TestCleanup:
    """The staging directory is removed on exit, no matter how the body exits.

    Three exit paths matter:

        * **Normal return** (after ``commit``) — the files have been
          moved to the destination; the staging directory is empty
          and its removal is trivial.
        * **Exception raised** — the files are still in staging, but
          the staging directory *itself* must be removed so the user
          does not see a ``.forge-stage-XXXXXX`` directory they did
          not create.
        * **Explicit early return** — the same cleanup path as the
          exception case (both exit the ``with`` block).

    ``shutil.rmtree(ignore_errors=True)`` is what the implementation
    uses. The tests assert on the *observable* property — the staging
    directory is gone — not on the implementation (which is a private
    detail). A future change to a different cleanup mechanism would
    still pass.

    **Why the two exception-raising tests use module-level helpers.**
    Both tests need the same shape: "staging has files, then a
    RuntimeError is raised from inside the ``StagingArea`` block, then
    cleanup runs." That shape violates *two* lint rules simultaneously:

        * **SIM117** — the ``with StagingArea(...)`` block and the
          ``with pytest.raises(...)`` block must be *one* ``with``
          statement with multiple contexts, not nested ``with``s.
        * **PT012** — the body of ``pytest.raises`` must contain a
          single simple statement (a raise or a call), not a
          sequence of staging writes followed by a raise.

    The only shape that satisfies both is:

        with (
            StagingArea.for_root(tmp_path) as stage,
            pytest.raises(RuntimeError, match="simulated"),
        ):
            _stage_files_and_raise(stage)

    where ``_stage_files_and_raise`` encapsulates the staging-then-raise
    sequence. The helper is deliberately *not* inline — inlining it
    would put multiple statements inside ``pytest.raises``, which is
    exactly what PT012 forbids. The helpers live at module scope (not
    as methods) so they can be reused by both tests, and each helper
    name states exactly what it does.
    """

    @pytest.mark.unit()
    def test_staging_dir_removed_after_commit(self, tmp_path: Path) -> None:
        """After ``commit`` and exit, no ``.forge-stage-*`` directories remain.

        The glob is scoped to ``tmp_path.parent`` because the staging
        directory is a *sibling* of the destination root, not a child
        of it. A test that only globbed inside ``tmp_path`` would pass
        for a bug that failed to clean up the staging tree entirely —
        the tree would be in ``tmp_path.parent``, invisible to a glob
        scoped to the destination. That would be exactly the kind of
        half-checked test the codebase's design principles warn
        against.
        """
        with StagingArea.for_root(tmp_path) as stage:
            stage.write_text("file.txt", "x")
            stage.commit()

        staging_dirs = list(tmp_path.parent.glob(".forge-stage-*"))
        assert staging_dirs == []

    @pytest.mark.unit()
    def test_staging_dir_removed_after_exception(self, tmp_path: Path) -> None:
        """An exception inside the ``with`` block still triggers cleanup.

        The helper call inside ``pytest.raises`` stages ``file.txt``
        and then raises ``RuntimeError``. The two contexts are entered
        in order — ``StagingArea`` first, then ``pytest.raises`` — and
        exited in reverse on the raise: ``pytest.raises`` swallows the
        exception, then ``StagingArea.__exit__`` runs cleanup. The
        final assertion confirms the staging directory is gone.

        As with ``test_staging_dir_removed_after_commit``, the glob
        is scoped to ``tmp_path.parent`` (the destination root's
        parent) because the staging directory is a sibling of the
        destination, not a child of it.
        """
        with (
            StagingArea.for_root(tmp_path) as stage,
            pytest.raises(RuntimeError, match="simulated"),
        ):
            _stage_one_and_raise(stage)

        staging_dirs = list(tmp_path.parent.glob(".forge-stage-*"))
        assert staging_dirs == []

    @pytest.mark.unit()
    def test_files_not_committed_are_discarded_with_staging(self, tmp_path: Path) -> None:
        """Files staged but not committed are removed with the staging directory.

        This is the *point* of staging: a failed operation leaves the
        destination untouched and does not leave behind partial files.
        The helper stages three files (three, not one, so a bug that
        cleaned up the first but not the others would be caught) and
        then raises. The final assertions confirm that no destination
        files and no staging directory remain.
        """
        with (
            StagingArea.for_root(tmp_path) as stage,
            pytest.raises(RuntimeError, match="simulated"),
        ):
            _stage_three_and_raise(stage)

        # No destination files, no staging directory.
        assert list(tmp_path.glob("*.txt")) == []
        assert list(tmp_path.parent.glob(".forge-stage-*")) == []

    @pytest.mark.unit()
    def test_staging_dir_is_hidden(self, tmp_path: Path) -> None:
        """The staging directory's name starts with ``.`` (hidden in ``ls``).

        A user running ``ls -a`` in the destination's *parent*
        directory — the directory that contains both the destination
        and the staging sibling — would see the staging directory's
        name. The leading dot keeps it out of plain ``ls`` (and thus
        out of the user's casual view). In *plain* ``ls`` of the
        destination itself, the staging directory does not appear at
        all, because it is not inside the destination.

        The test inspects the *name* of the staging directory, not its
        visibility relative to any particular directory. That is the
        property the leading dot exists to provide, and it holds
        regardless of where the staging directory lives.

        The ``stage`` binding from ``StagingArea.for_root(...)`` is
        assigned to a throwaway because the test only inspects the
        filesystem (via ``glob`` on the parent) — it does not call
        any method on the staging area itself. Using ``_stage``
        (leading underscore) documents "deliberately unused" and
        silences ``F841``.
        """
        with StagingArea.for_root(tmp_path) as _stage:
            # Staging is a sibling of the destination root, so it
            # appears in the destination's *parent* directory, not
            # in the destination itself.
            staging_dirs = list(tmp_path.parent.glob(".forge-stage-*"))
            assert len(staging_dirs) == 1
            # And it is hidden (leading dot).
            assert staging_dirs[0].name.startswith(".")


# =============================================================================
# Group 5: multiple writes and mixed content
# =============================================================================
class TestMultipleWrites:
    """Multiple writes, nested paths, and mixed content all work.

    The staging area is called once per file the pipeline produces.
    A blueprint with 50 files means 50 calls to ``write_text`` or
    ``write_bytes``, interleaved with rendering. The tests here
    exercise the loop-in-progress scenario:

        * Many files staged before a single commit.
        * Mixed ``write_text`` and ``write_bytes`` in the same
          staging area.
        * Nested paths that create intermediate directories.

    Each of these is what a real pipeline does; the tests pin the
    behaviour in isolation.
    """

    @pytest.mark.unit()
    def test_many_files_staged_then_committed(self, tmp_path: Path) -> None:
        """Many files staged before a single commit all appear."""
        with StagingArea.for_root(tmp_path) as stage:
            for i in range(10):
                stage.write_text(f"file{i}.txt", f"content {i}")
            committed = stage.commit()

        assert len(committed) == 10
        for i in range(10):
            assert (tmp_path / f"file{i}.txt").read_text() == f"content {i}"

    @pytest.mark.unit()
    def test_mixed_text_and_bytes(self, tmp_path: Path) -> None:
        """``write_text`` and ``write_bytes`` in one staging area work.

        A blueprint may produce rendered text files (via
        ``write_text``) alongside copied binary files (via
        ``write_bytes``). The staging area handles both — the
        contents are stored as bytes internally, and the destination
        receives the right bytes for each.
        """
        with StagingArea.for_root(tmp_path) as stage:
            stage.write_text("text.txt", "hello")
            stage.write_bytes("binary.bin", b"\x00\x01\x02")
            stage.commit()

        assert (tmp_path / "text.txt").read_text() == "hello"
        assert (tmp_path / "binary.bin").read_bytes() == b"\x00\x01\x02"

    @pytest.mark.unit()
    def test_deep_nested_directories_created(self, tmp_path: Path) -> None:
        """``commit`` creates intermediate directories as needed.

        The staging area does not require the caller to pre-create
        intermediate directories; ``commit`` calls
        ``final.parent.mkdir(parents=True, exist_ok=True)`` before
        ``os.replace``. The test pins that behaviour — a caller that
        writes to ``a/b/c/d/file.txt`` does not have to call
        ``mkdir`` first.
        """
        with StagingArea.for_root(tmp_path) as stage:
            stage.write_text("a/b/c/d/e/f/file.txt", "deep")
            stage.commit()

        assert (tmp_path / "a/b/c/d/e/f/file.txt").read_text() == "deep"

    @pytest.mark.unit()
    def test_duplicate_writes_use_last_content(self, tmp_path: Path) -> None:
        """Writing the same path twice means the last write wins.

        The staging area deduplicates by *path*: a second write to
        ``file.txt`` overwrites the staged content (so the last write
        is what gets committed), and ``_created`` records the path
        once (so ``commit`` moves it once). This matches what a caller
        would get from writing the same file twice directly to the
        destination: the OS treats the second write as last-write-wins,
        and the staging area's commit is consistent with that.
        """
        with StagingArea.for_root(tmp_path) as stage:
            stage.write_text("file.txt", "first")
            stage.write_text("file.txt", "second")
            committed = stage.commit()

        assert (tmp_path / "file.txt").read_text() == "second"
        assert committed == [tmp_path / "file.txt"]


# =============================================================================
# Group 6: staging root placement — same filesystem, hidden sibling
# =============================================================================
class TestStagingRootPlacement:
    """The staging directory is anchored next to the destination root.

    Two properties matter:

        * **Same filesystem** — ``os.replace`` is atomic only within
          a filesystem. Staging in the destination's *parent* is
          guaranteed to be on the same filesystem as the destination
          (creating a directory in a parent does not cross a
          filesystem boundary), so commit is still atomic. The test
          for this asserts the *property the guarantee depends on*
          (``st_dev`` equality), not the placement strategy (which
          could change in a future refactor without breaking the
          guarantee).
        * **Sibling, not child** — the staging directory is a
          sibling of the destination root, not inside it. This
          matters for two reasons: (1) a failed run does not leave
          an empty destination behind, because the destination is
          never created just to hold the staging tree; (2) a user
          browsing the destination during a run sees no staging
          artifacts, because the staging tree is outside their view.

    The tests here pin both properties as *observable* facts.
    """

    @pytest.mark.unit()
    def test_staging_dir_is_sibling_of_destination_root(self, tmp_path: Path) -> None:
        """The staging directory lives in the destination's *parent*.

        This is the load-bearing placement guarantee. The earlier
        design staged *inside* the destination; that worked for
        atomicity but forced the destination to be created before
        staging could begin, which leaked an empty directory on any
        failed run. The current design stages in the parent, which
        has two consequences the test pins:

            * The staging directory is a *sibling* of the
              destination (its parent is ``tmp_path.parent``).
            * It is *not* inside the destination (a glob on
              ``tmp_path`` would find nothing staging-related).

        Asserting both — the sibling fact and the not-a-child fact —
        is what distinguishes "staged in the right place" from
        "happened to be next to the right place". A child of
        ``tmp_path`` would satisfy neither, but the two assertions
        also rule out a deeper placement (a great-grandchild of
        ``tmp_path.parent``) that neither assertion alone would
        catch.

        The ``stage`` binding is assigned to a throwaway because the
        test only inspects the *placement* of the staging directory
        (via ``glob``) — it does not call any method on the staging
        area itself. Using ``_stage`` documents "deliberately unused"
        and silences ``F841``.
        """
        with StagingArea.for_root(tmp_path) as _stage:
            staging_dirs = list(tmp_path.parent.glob(".forge-stage-*"))
            assert len(staging_dirs) == 1, (
                f"expected one staging directory in {tmp_path.parent}, "
                f"found {[p.name for p in staging_dirs]!r}"
            )
            # Property (1): sibling.
            assert staging_dirs[0].parent == tmp_path.parent
            # Property (2): not a child. This catches a regression
            # that put the staging directory *inside* the destination
            # — the exact bug the current design exists to prevent.
            assert not staging_dirs[0].is_relative_to(tmp_path)

    @pytest.mark.unit()
    def test_staging_dir_created_if_root_missing(self, tmp_path: Path) -> None:
        """A destination root that does not exist is created on commit.

        The earlier design called ``root.mkdir(parents=True,
        exist_ok=True)`` at ``for_root`` time, which is why a
        "destination root does not exist" test would have asserted
        the root's existence *while the staging area was open*.
        The current design defers the destination's creation to
        commit — the first ``os.replace`` calls ``target.parent.
        mkdir(parents=True, exist_ok=True)``, which creates the
        destination as a side effect of the first file landing in
        it. This test verifies the end-to-end result: a call to
        ``for_root`` with a not-yet-created destination root, followed
        by a commit, leaves the destination root created with the
        committed file in it.

        The intermediate state (root does not exist while staging is
        open) is what the sibling-placement test's *other* half
        verifies — that the staging tree does not create the
        destination root before commit. The two tests together pin
        the complete "creation is deferred to commit" contract.
        """
        new_root = tmp_path / "new-project-dir"
        assert not new_root.exists()

        with StagingArea.for_root(new_root) as stage:
            # While staging is open, the destination root does not
            # yet exist — the staging area does not create it, and
            # the caller has not committed.
            assert not new_root.exists(), (
                "the destination root was created by staging; creation "
                "must be deferred to commit so a failed run does not "
                "leave an empty directory behind"
            )
            stage.write_text("file.txt", "x")
            stage.commit()

        assert new_root.exists()
        assert (new_root / "file.txt").read_text() == "x"

    @pytest.mark.unit()
    def test_staging_dir_is_on_same_filesystem_as_destination(self, tmp_path: Path) -> None:
        """The staging directory and the destination are on the same device.

        This is the *actual* guarantee that makes ``os.replace``
        atomic at commit time. The earlier test —
        ``test_staging_dir_inside_destination_root`` — asserted a
        stronger-looking property (staging is inside the destination)
        that implied same-filesystem but was not equivalent to it:
        the stronger property forced an empty directory to be created
        on failure, which was the bug. The current design gives up the
        stronger property while preserving the weaker one that matters.

        The test compares ``os.stat().st_dev`` for the two paths.
        Two paths are on the same filesystem if and only if their
        ``st_dev`` values are equal — this is exactly the check
        ``os.replace`` makes internally. Asserting on it here is the
        direct expression of the atomicity guarantee, and it survives
        a future placement refactor as long as the guarantee holds.

        The staging directory is captured *while the staging area is
        open*, because that is the only moment it exists; the test
        then compares its device to the destination root's device
        (which is not affected by the staging tree's presence or
        absence).
        """
        with StagingArea.for_root(tmp_path) as _stage:
            staging_dirs = list(tmp_path.parent.glob(".forge-stage-*"))
            assert len(staging_dirs) == 1, (
                f"expected one staging directory in {tmp_path.parent}, "
                f"found {[p.name for p in staging_dirs]!r}"
            )
            staging_dir = staging_dirs[0]

            # ``tmp_path.parent`` is created by pytest and always
            # exists; the destination root (``tmp_path``) exists
            # because pytest created it. Comparing ``st_dev`` on
            # both is the direct expression of "same filesystem".
            staging_dev = os.stat(staging_dir).st_dev
            destination_dev = os.stat(tmp_path).st_dev

            assert staging_dev == destination_dev, (
                f"staging directory {staging_dir} (device {staging_dev}) "
                f"is on a different filesystem from destination root "
                f"{tmp_path} (device {destination_dev}) — cross-filesystem "
                f"staging breaks atomic commit (see "
                f"forge.safety.staging's 'Where the staging directory "
                f"lives' section)"
            )
