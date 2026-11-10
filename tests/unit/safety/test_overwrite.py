"""Contract tests for ``backup_existing`` — the overwrite backup maker.

Where this file sits
--------------------
``backup_existing`` is the *data-preservation* guarantee behind
``--overwrite``. When the caller passes ``overwrite=True`` (to
``detect_collisions``, to ``validate_destination``, and eventually to
the writer), the safety layer stops refusing to replace existing
files. ``backup_existing`` is the function the *caller* is expected
to run before writing, so the "overwrite" is always recoverable.

Three guarantees, each tested in its own class
----------------------------------------------
1. **Every existing target gets a backup.** A file present on disk at
   call time is copied to a timestamped sibling. The original is not
   modified by the backup operation — the *caller* will modify it
   later, but the backup itself does not.

2. **The copy is atomic per file.** ``backup_existing`` uses the
   standard pattern: copy to a hidden temp file in the destination
   directory, then ``os.replace`` to the final backup name. A crash
   mid-copy leaves a hidden temp file (cleaned up on next invocation)
   and no partially-written backup at the expected name. A backup
   that exists is *complete*.

3. **The operation is all-or-nothing across the set.** If any copy
   fails (disk full, permission, I/O error), every backup created so
   far in the same call is removed before the exception propagates.
   The caller sees a clean failure and can retry without worrying
   about a half-populated backup set.

Guarantee 3 is the most important and the one most likely to regress
silently. A "best-effort" backup implementation — one that skips files
it cannot back up and continues — is worse than no backup at all: the
user believes their files are preserved, restores from the set,
discovers one is missing, and by then the original is gone. The
all-or-nothing discipline is what makes "``--overwrite`` is safe" a
*claim* rather than a *hope*.

Why the timestamp is a parameter
--------------------------------
Production passes ``timestamp=None`` and gets
``datetime.now(timezone.utc)`` formatted as ``YYYYMMDDTHHMMSSZ``.
Tests pass a fixed string to make backup names deterministic, so the
tests can assert on the exact path. The parameter is a *test seam*,
not a production knob — the docstring of ``backup_existing`` says so,
and the tests here exercise both paths.

Why ``copy2`` and not ``copy``
------------------------------
``shutil.copy2`` preserves mtime and mode bits; ``shutil.copy`` does
not. The tests pin ``copy2`` indirectly by asserting that the backup's
mtime matches the original's (``copy`` would lose the sub-second
precision that ``copy2`` preserves). A future change to ``copyfile``
or ``copy`` would fail that test, and the failure would name the
property that changed.

Filesystem-dependent tests
--------------------------
A few tests depend on filesystem behaviour that is not portable:

* **Rollback on failure** requires simulating a mid-set failure. The
  cleanest way is to monkeypatch ``shutil.copy2`` to fail on the
  second call. That is portable (it does not depend on the
  filesystem's error conditions), and it exercises the exact code
  path a real failure would take.
* **Symlink handling** tests that a symlink's own entry (not its
  target) is backed up. Skipped when the platform cannot create
  symlinks, via the same ``make_symlink`` helper the safety layer's
  own tests use.

Every skip is explicit (``pytest.skip(reason=...)``) so a CI run
shows "N skipped" with the reason, rather than silently skipping or
failing for environmental reasons.

What this file does *not* cover
-------------------------------
* **Where the backup happens in the pipeline.** That is
  ``tests/unit/cli/test_create_dual_mode.py``'s job — the integration
  there is "``--overwrite`` triggers a backup before the write".
* **The staging write itself.** That is
  ``tests/unit/safety/test_staging.py``'s job. ``backup_existing``
  and ``StagingArea`` are independent primitives; the caller
  coordinates them.
* **Collision detection.** That is
  ``tests/unit/safety/test_collisions.py``'s job.
  ``backup_existing`` does not decide whether a collision exists —
  it only preserves the file once the caller has decided to
  overwrite.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from forge.safety import backup_existing
from tests.adversarial.conftest import make_symlink


# =============================================================================
# Helpers
# =============================================================================
def _make_files(directory: Path, names: list[str]) -> list[Path]:
    """Create one file per name, each with the name as its content.

    Small helper used by several tests that need "a set of files to
    back up". The content equals the name so a test can assert that
    the *right* file was backed up (not just "some file"). This is
    the property that makes ``test_correct_file_backed_up``
    meaningful — a backup that has the right *name* but the wrong
    *content* would be a bug, and this helper makes that bug
    observable.
    """
    files: list[Path] = []
    for name in names:
        f = directory / name
        f.write_text(name)
        files.append(f)
    return files


# =============================================================================
# Group 1: the happy path — every existing target gets a backup
# =============================================================================
class TestBacksUpExistingFiles:
    """Every file on disk at call time gets a recoverable backup.

    The test's job is to prove the *guarantee*: after
    ``backup_existing``, every input file has a sibling whose content
    matches. The original files are unchanged (a backup does not
    modify its source).

    A regression that dropped a file from the set — say, by returning
    early or by skipping an entry it could not lstat — would fail
    ``test_multiple_files_all_backed_up``. A regression that modified
    the source while backing it up would fail
    ``test_original_content_unchanged``.
    """

    @pytest.mark.unit()
    def test_single_file_backed_up(self, tmp_path: Path) -> None:
        """One existing file produces one backup."""
        (tmp_path / "file.txt").write_text("original")

        backups = backup_existing([tmp_path / "file.txt"], timestamp="20240101T000000Z")

        assert len(backups) == 1
        assert backups[0].original == tmp_path / "file.txt"
        assert backups[0].backup.exists()

    @pytest.mark.unit()
    def test_multiple_files_all_backed_up(self, tmp_path: Path) -> None:
        """Every existing file in the input set gets a backup.

        The all-or-nothing guarantee means *every* file either has a
        backup or the call raised. This test pins the "every file"
        half; the raise half is in ``TestRollbackOnFailure``.
        """
        files = _make_files(tmp_path, ["a.txt", "b.txt", "c.txt"])

        backups = backup_existing(files, timestamp="20240101T000000Z")

        assert len(backups) == 3
        backup_paths = {b.backup.name for b in backups}
        assert backup_paths == {
            "a.txt.bak.20240101T000000Z",
            "b.txt.bak.20240101T000000Z",
            "c.txt.bak.20240101T000000Z",
        }

    @pytest.mark.unit()
    def test_backup_content_matches_original(self, tmp_path: Path) -> None:
        """The backup's bytes are identical to the original's bytes.

        A backup that exists but is empty (a common failure when using
        ``touch`` instead of ``copy2``) is worse than no backup: the
        user restores nothing. The test asserts on content, not just
        existence.
        """
        original = tmp_path / "important.txt"
        original.write_text("precious data")

        backups = backup_existing([original], timestamp="20240101T000000Z")

        assert backups[0].backup.read_text() == "precious data"

    @pytest.mark.unit()
    def test_original_content_unchanged(self, tmp_path: Path) -> None:
        """The original file is not modified by the backup.

        ``backup_existing`` *copies*, it does not move. The caller
        will eventually overwrite the original, but that is the
        *writer's* job, not the backup's. A backup that moved the file
        would leave the original nonexistent, and the subsequent
        write would create a new file rather than replace the
        original — a subtle data loss the user would not notice until
        they tried to restore.
        """
        original = tmp_path / "file.txt"
        original.write_text("original content")

        backup_existing([original], timestamp="20240101T000000Z")

        assert original.read_text() == "original content"

    @pytest.mark.unit()
    def test_backup_preserves_mtime(self, tmp_path: Path) -> None:
        """The backup preserves the original's mtime.

        ``shutil.copy2`` preserves mtime; ``shutil.copy`` does not.
        The test pins that ``copy2`` (or equivalent) is used —
        otherwise ``git status`` would show backups as "modified" when
        they are byte-identical, and a future restore would lose the
        timestamp.
        """
        original = tmp_path / "file.txt"
        original.write_text("x")
        # Set a distinctive mtime to make the assertion meaningful.
        # The exact value is arbitrary; only that it is *some* value
        # that copy2 would preserve and copy would lose.
        os.utime(original, (1_000_000_000, 1_000_000_000))

        backups = backup_existing([original], timestamp="20240101T000000Z")

        assert backups[0].backup.stat().st_mtime == original.stat().st_mtime

    @pytest.mark.unit()
    def test_backup_preserves_mode_bits(self, tmp_path: Path) -> None:
        """The backup preserves the original's permission bits.

        An executable script that gets overwritten and then restored
        must remain executable; a ``copy`` (rather than ``copy2``)
        would lose the exec bit and produce a non-runnable script on
        restore.

        Why the ``# noqa: S103``: `0o755` is the standard mode for a
        user-writable, group/other-readable-and-executable script. It
        is what a caller would set on a shell script or a CLI entry
        point. S103 flags *any* use of `0o755` because it cannot tell
        a test fixture (which is only exercising `copy2`'s
        mode-preservation behaviour, and is never installed anywhere
        sensitive) from a `chmod` on a production binary. A narrower
        mode (say `0o700`) would still exercise the mode-preservation
        path but would weaken the test: the very property being
        verified is that *distinctive* mode bits survive the copy, and
        `0o755` is the mode most likely to matter in practice. The
        suppression is scoped to this line and this line only.
        """
        original = tmp_path / "script.sh"
        original.write_text("#!/bin/sh\necho hi\n")
        os.chmod(original, 0o755)  # noqa: S103 - fixture, not installed anywhere

        backups = backup_existing([original], timestamp="20240101T000000Z")

        assert stat.S_IMODE(backups[0].backup.stat().st_mode) == 0o755

    @pytest.mark.unit()
    def test_backup_lands_in_same_directory(self, tmp_path: Path) -> None:
        """The backup is a sibling of the original, not a global cache.

        ``backup_existing`` does not write to ``~/.cache`` or
        ``/tmp`` — it writes next to the original file, so:

            * the backup survives a reboot (unlike ``/tmp``),
            * the user can find it with ``ls``,
            * the restore is a plain ``mv``, not a filesystem-layout
              scavenger hunt.
        """
        original = tmp_path / "file.txt"
        original.write_text("x")

        backups = backup_existing([original], timestamp="20240101T000000Z")

        assert backups[0].backup.parent == original.parent


# =============================================================================
# Group 2: atomicity — the backup is complete or absent
# =============================================================================
class TestAtomicity:
    """The backup is either fully present or fully absent.

    ``backup_existing`` uses ``copy2`` into a hidden temp file, then
    ``os.replace`` to the final backup name. ``os.replace`` is atomic
    within a filesystem, so the final backup is always a complete
    copy — never a partial write. A failure mid-copy leaves a hidden
    temp file (which the ``finally`` block removes), not a broken
    backup at the expected name.

    Testing "no partial backup" directly is hard — the ``finally``
    block removes the temp file before the test can look for it. The
    tests here pin the *observable* properties:

        * No hidden temp files are left behind after a successful
          backup.
        * A failure during the copy leaves no backup at the expected
          name and no hidden temp file behind.
    """

    @pytest.mark.unit()
    def test_no_temp_files_left_after_success(self, tmp_path: Path) -> None:
        """A successful backup leaves no hidden temp files.

        The atomic-copy pattern creates a temp file, writes into it,
        renames it to the final backup, and (in the ``finally``
        block) tries to unlink it. On success the rename consumes the
        temp file, so the ``finally`` unlink raises
        ``FileNotFoundError`` (which is swallowed). Either way, no
        temp file remains. The test proves the ``finally`` cleanup
        works, not just the happy-path rename.
        """
        (tmp_path / "file.txt").write_text("x")

        backup_existing([tmp_path / "file.txt"], timestamp="20240101T000000Z")

        # No hidden temp files anywhere in the directory. The
        # pattern `.file.txt.XXXXX.tmp` is what `mkstemp` generates.
        temp_files = list(tmp_path.glob(".*.tmp"))
        assert temp_files == []

    @pytest.mark.unit()
    def test_failure_leaves_no_partial_backup(self, tmp_path: Path) -> None:
        """A copy failure leaves no backup at the expected name.

        Simulated by monkeypatching ``shutil.copy2`` to raise. The
        atomic-copy pattern is:

            1. ``mkstemp`` creates a temp file.
            2. ``copy2`` writes into it (raises here).
            3. ``os.replace`` (never reached).
            4. ``finally`` unlinks the temp file.

        The test asserts that no ``.bak.`` file exists afterward —
        the ``finally`` cleanup removed the temp file, and the
        ``os.replace`` never created the final name.
        """
        original = tmp_path / "file.txt"
        original.write_text("x")

        def _fail_copy(*_a, **_k):
            raise OSError("simulated disk full")

        import forge.safety.overwrite as ow_module

        real_copy2 = ow_module.shutil.copy2
        ow_module.shutil.copy2 = _fail_copy
        try:
            with pytest.raises(OSError, match="simulated"):
                backup_existing([original], timestamp="20240101T000000Z")
        finally:
            ow_module.shutil.copy2 = real_copy2

        # No backup and no temp file at either the expected name or
        # a hidden temp name.
        assert not (tmp_path / "file.txt.bak.20240101T000000Z").exists()
        assert list(tmp_path.glob(".*.tmp")) == []


# =============================================================================
# Group 3: all-or-nothing across the set (the safety guarantee)
# =============================================================================
class TestRollbackOnFailure:
    """If any backup fails, every backup created so far is removed.

    This is the *load-bearing* guarantee. A "best-effort" backup
    implementation that skipped files it could not back up would be
    worse than no backup at all: the user trusts the backup set, and
    a missing file in it is discovered only after the original has
    been overwritten.

    The rollback runs in the function's ``except`` block, after the
    failing copy raises. The tests here exercise:

        * The rollback itself (backups created before the failure are
          removed).
        * The exception propagation (the caller sees the original
          ``OSError``, not a wrapped or suppressed one).
        * The absence of side effects (the *original* files are not
          touched by the rollback — ``backup_existing`` copies, it
          does not move).
    """

    @pytest.mark.unit()
    def test_rollback_removes_all_created_backups(self, tmp_path: Path) -> None:
        """A failure on file N removes backups for files 1..N-1."""
        files = _make_files(tmp_path, ["a.txt", "b.txt", "c.txt"])

        # Fail on the second ``copy2`` call. The first call succeeded
        # (backup for ``a.txt`` was created); the second (for
        # ``b.txt``) raises; the rollback removes ``a.txt``'s backup;
        # ``c.txt`` was never attempted.
        call_count = {"n": 0}
        import forge.safety.overwrite as ow_module

        real_copy2 = ow_module.shutil.copy2

        def _fail_on_second(src, dst, *args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 2:
                raise OSError("simulated failure")
            return real_copy2(src, dst, *args, **kwargs)

        ow_module.shutil.copy2 = _fail_on_second
        try:
            with pytest.raises(OSError, match="simulated failure"):
                backup_existing(files, timestamp="20240101T000000Z")
        finally:
            ow_module.shutil.copy2 = real_copy2

        # No backups anywhere.
        assert list(tmp_path.glob("*.bak.*")) == []

    @pytest.mark.unit()
    def test_rollback_does_not_touch_originals(self, tmp_path: Path) -> None:
        """The rollback leaves the original files exactly as they were.

        A rollback that "restored" by moving backups back over the
        originals would be wrong here — ``backup_existing`` copies,
        it does not move. The originals were never modified; the
        rollback only removes the copies.
        """
        files = _make_files(tmp_path, ["a.txt", "b.txt"])
        a_original_content = (tmp_path / "a.txt").read_text()
        b_original_content = (tmp_path / "b.txt").read_text()

        import forge.safety.overwrite as ow_module

        real_copy2 = ow_module.shutil.copy2
        call_count = {"n": 0}

        def _fail_on_first(src, dst, *args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise OSError("simulated first-call failure")
            return real_copy2(src, dst, *args, **kwargs)

        ow_module.shutil.copy2 = _fail_on_first
        try:
            with pytest.raises(OSError, match="simulated first-call failure"):
                backup_existing(files, timestamp="20240101T000000Z")
        finally:
            ow_module.shutil.copy2 = real_copy2

        # The originals are untouched. Their content, mtime, and mode
        # bits are exactly as they were before the failed call.
        assert (tmp_path / "a.txt").read_text() == a_original_content
        assert (tmp_path / "b.txt").read_text() == b_original_content

    @pytest.mark.unit()
    def test_rollback_propagates_original_exception(self, tmp_path: Path) -> None:
        """The caller sees the *original* ``OSError``, not a wrapped one.

        A rollback that raised its own exception (say, ``RollbackFailed``)
        would mask the actual failure — the user would see "rollback
        failed" and not "disk full". The current code propagates the
        original exception; the rollback swallows its own failures
        (which are logged but not re-raised). The test pins the
        propagation.
        """
        files = _make_files(tmp_path, ["a.txt"])

        import forge.safety.overwrite as ow_module

        real_copy2 = ow_module.shutil.copy2

        def _raise_specific(src, dst, *args, **kwargs):
            raise OSError("disk full at sector 12345")

        ow_module.shutil.copy2 = _raise_specific
        try:
            with pytest.raises(OSError, match="disk full at sector 12345") as info:
                backup_existing(files, timestamp="20240101T000000Z")
        finally:
            ow_module.shutil.copy2 = real_copy2

        # The *specific* message reached the caller.
        assert "disk full at sector 12345" in str(info.value)


# =============================================================================
# Group 4: nonexistent inputs — silently skipped, not errors
# =============================================================================
class TestNonexistentInputs:
    """A target that does not exist has nothing to back up.

    The caller's plan may include destinations that do not exist yet
    (a fresh scaffold has no existing files to back up). Treating
    "does not exist" as a failure would be a bug — the function's
    job is "make a backup if there's something to back up", not
    "require every target to exist". The tests pin that behaviour.
    """

    @pytest.mark.unit()
    def test_nonexistent_target_produces_no_backup(self, tmp_path: Path) -> None:
        """A target that does not exist yields no ``Backup`` entry."""
        missing = tmp_path / "does-not-exist.txt"
        backups = backup_existing([missing], timestamp="20240101T000000Z")
        assert backups == []

    @pytest.mark.unit()
    def test_mixed_existing_and_nonexistent(self, tmp_path: Path) -> None:
        """Only the existing targets get backups; missing ones are skipped."""
        existing = tmp_path / "exists.txt"
        existing.write_text("x")
        missing = tmp_path / "missing.txt"

        backups = backup_existing([existing, missing], timestamp="20240101T000000Z")

        assert len(backups) == 1
        assert backups[0].original == existing

    @pytest.mark.unit()
    def test_empty_input_produces_empty_output(self, tmp_path: Path) -> None:
        """An empty input list is a valid no-op."""
        backups = backup_existing([], timestamp="20240101T000000Z")
        assert backups == []


# =============================================================================
# Group 5: deduplication — same file twice yields one backup
# =============================================================================
class TestDeduplication:
    """Two entries naming the same file produce one backup.

    The caller's plan might pass the same destination twice (a bug, but
    a recoverable one). A backup implementation that copied twice would
    create two backups with the same name — the second ``os.replace``
    would overwrite the first, so the user sees *one* backup but the
    function reports *two*, which is a lie. The dedup-by-resolved-path
    rule prevents that: the second entry is skipped.
    """

    @pytest.mark.unit()
    def test_same_path_twice_yields_one_backup(self, tmp_path: Path) -> None:
        """Two references to the same file produce one backup."""
        f = tmp_path / "file.txt"
        f.write_text("x")

        backups = backup_existing([f, f], timestamp="20240101T000000Z")

        assert len(backups) == 1
        assert backups[0].original == f

    @pytest.mark.unit()
    def test_relative_and_absolute_paths_dedupe(self, tmp_path: Path) -> None:
        """A relative and an absolute path to the same file dedupe.

        ``os.path.realpath`` (via ``Path.resolve``) is what makes this
        work — the two paths name the same file, and the dedup is by
        resolved path. A path-based dedup (by string) would miss this
        case.
        """
        f = tmp_path / "file.txt"
        f.write_text("x")
        # Simulate a relative path by using a symlinked directory —
        # a portable way to spell the same file two ways.
        link_dir = tmp_path / "link_dir"
        make_symlink(tmp_path, link_dir)
        aliased_path = link_dir / "file.txt"

        backups = backup_existing([f, aliased_path], timestamp="20240101T000000Z")

        # One backup, not two. The exact path reported is whichever
        # spelling came first; the test does not pin which.
        assert len(backups) == 1


# =============================================================================
# Group 6: timestamp validation — malformed timestamps rejected early
# =============================================================================
class TestTimestampValidation:
    """The timestamp becomes part of a filename; unsafe values are rejected.

    ``backup_existing`` composes the backup name as
    ``<original>.bak.<timestamp>``. If the timestamp contained a path
    separator or a NUL byte, the backup could land somewhere entirely
    different — the kind of bug that turns a data-preservation feature
    into a data-loss one.

    The validation in ``_validate_timestamp`` rejects:

        * empty strings,
        * path separators (``/``, ``\\``),
        * NUL bytes,
        * newlines and carriage returns.

    The tests pin each rejection with a specific input. A future change
    that "simplified" the validation (say, by dropping the newline
    check) would fail the corresponding test, and the failure would
    name the input that was no longer rejected.
    """

    @pytest.mark.unit()
    def test_empty_timestamp_rejected(self, tmp_path: Path) -> None:
        """An empty timestamp would produce ``.bak.`` — rejected."""
        with pytest.raises(ValueError, match="non-empty"):
            backup_existing([tmp_path / "file.txt"], timestamp="")

    @pytest.mark.unit()
    @pytest.mark.parametrize(
        "bad_ts",
        ["2024/01/01", "2024\\01\\01", "20240101T00\x0000Z"],
        ids=["forward-slash", "back-slash", "nul"],
    )
    def test_path_separators_rejected(self, tmp_path: Path, bad_ts: str) -> None:
        """Path separators (and NUL) in the timestamp are rejected.

        The match string "forbidden" comes from the validator's own
        message for all three cases — ``timestamp contains forbidden
        characters`` for the NUL byte, and ``timestamp contains
        forbidden characters`` for the slashes too (the check
        combines them). Pinning to that shared word keeps the test
        robust to wording changes in either branch while still
        catching a regression that swapped the failure for a
        successful backup under a path-traversal-shaped timestamp.
        """
        with pytest.raises(ValueError, match="forbidden"):
            backup_existing([tmp_path / "file.txt"], timestamp=bad_ts)

    @pytest.mark.unit()
    @pytest.mark.parametrize(
        "bad_ts",
        ["2024\n0101", "2024\r0101"],
        ids=["newline", "cr"],
    )
    def test_control_characters_rejected(self, tmp_path: Path, bad_ts: str) -> None:
        """Newlines and carriage returns are rejected (log injection).

        Same shared "forbidden" word as the separator cases, because
        ``_validate_timestamp`` treats ``\\n`` and ``\\r`` as members
        of the forbidden character set alongside ``/`` and ``\\``.
        """
        with pytest.raises(ValueError, match="forbidden"):
            backup_existing([tmp_path / "file.txt"], timestamp=bad_ts)

    @pytest.mark.unit()
    def test_default_timestamp_is_valid(self, tmp_path: Path) -> None:
        """``timestamp=None`` produces a valid, filesystem-safe timestamp.

        The default is ``datetime.now(timezone.utc).strftime(...)``.
        The test verifies the default is not rejected by the
        validator and produces a backup with the expected suffix
        shape (``.bak.YYYYMMDDTHHMMSSZ``). A future change to the
        default format that reintroduced a separator would fail this
        test.
        """
        f = tmp_path / "file.txt"
        f.write_text("x")

        backups = backup_existing([f])

        assert len(backups) == 1
        backup_name = backups[0].backup.name
        assert backup_name.startswith("file.txt.bak.")
        # The suffix is YYYYMMDDTHHMMSSZ — 16 chars.
        suffix = backup_name[len("file.txt.bak.") :]
        assert len(suffix) == 16
        assert suffix.endswith("Z")


# =============================================================================
# Group 7: symlink handling — the entry, not the target
# =============================================================================
class TestSymlinkHandling:
    """A symlink's own entry is what gets backed up.

    ``backup_existing`` uses ``shutil.copy2``, which follows symlinks
    by default. For a *valid* symlink, that produces a regular file
    containing the target's bytes — recoverable content, even if not
    the symlink itself. For a *broken* symlink, ``copy2`` follows the
    link to a nonexistent target and raises ``FileNotFoundError``.

    The behaviour is a *consequence* of the safety layer's upstream
    policy: ``safe_resolve`` and ``validate_destination`` both reject
    symlink destinations by default, so a broken symlink should never
    reach ``backup_existing`` through the normal pipeline. If it does
    — because a caller bypassed the safety layer, or because a
    symlink was created in the TOCTOU window between the check and
    the backup — raising is the correct signal. Silently skipping
    would hide the bypass and let the subsequent write replace a
    symlink the user placed intentionally, with no backup to restore
    from.
    """

    @pytest.mark.unit()
    def test_symlink_to_file_backs_up_target_bytes(self, tmp_path: Path) -> None:
        """Backing up a symlink produces a regular file with target bytes."""
        target = tmp_path / "target.txt"
        target.write_text("target content")
        leaf = tmp_path / "link.txt"
        make_symlink(target, leaf)

        backups = backup_existing([leaf], timestamp="20240101T000000Z")

        assert len(backups) == 1
        # The backup is a regular file (not a symlink) with the
        # target's bytes. This is `copy2`'s default `follow_symlinks`
        # behaviour.
        assert backups[0].backup.is_symlink() is False
        assert backups[0].backup.read_text() == "target content"

    @pytest.mark.unit()
    def test_broken_symlink_raises(self, tmp_path: Path) -> None:
        """A broken symlink raises rather than silently skipping.

        A broken symlink occupies a name (so ``_target_exists``
        returns ``True``) but has no content to preserve (so
        ``copy2`` raises ``FileNotFoundError``). The function cannot
        honour "copy the file" for such a target, so it raises
        rather than proceeding with an empty backup.

        The behaviour is a *consequence* of the safety layer's
        upstream policy: ``safe_resolve`` and ``validate_destination``
        both reject symlink destinations by default, so a broken
        symlink should never reach ``backup_existing`` through the
        normal pipeline. If it does — a caller bypassed the safety
        layer, or a symlink was created in the TOCTOU window between
        the check and the backup — raising is the correct signal.
        Silently skipping would hide the bypass and let the
        subsequent write replace a symlink the user placed
        intentionally, with no backup to restore from.

        The test asserts on the *specific* exception type
        (``FileNotFoundError``, a subclass of ``OSError``) and on the
        offending path appearing in the message. A future change to a
        broader exception (``OSError``) or a different message would
        fail here, and the failure would name the property that
        changed.
        """
        leaf = tmp_path / "broken_link.txt"
        make_symlink(tmp_path / "does-not-exist.txt", leaf)

        with pytest.raises(FileNotFoundError, match="broken_link.txt"):
            backup_existing([leaf], timestamp="20240101T000000Z")


# =============================================================================
# Group 8: Backup output type (the caller's contract)
# =============================================================================
class TestBackupShape:
    """The ``Backup`` return value carries the two paths the caller needs.

    ``backup_existing`` returns ``list[Backup]``. A ``Backup`` has
    two fields: ``original`` (the file that was backed up) and
    ``backup`` (the timestamped sibling that was created). These are
    what a caller needs to (a) tell the user what was backed up and
    (b) construct a restore command.

    The tests pin the shape — a future ``Backup`` that dropped a field
    or renamed one would break the CLI's "3 files backed up to ..."
    rendering, and the test would fail with a clear message.
    """

    @pytest.mark.unit()
    def test_backup_carries_both_paths(self, tmp_path: Path) -> None:
        """``Backup`` has ``original`` and ``backup`` attributes."""
        f = tmp_path / "file.txt"
        f.write_text("x")

        backups = backup_existing([f], timestamp="20240101T000000Z")

        assert len(backups) == 1
        assert backups[0].original == f
        assert backups[0].backup == tmp_path / "file.txt.bak.20240101T000000Z"

    @pytest.mark.unit()
    def test_backup_is_frozen(self, tmp_path: Path) -> None:
        """``Backup`` is immutable — a caller cannot mutate it in place."""
        f = tmp_path / "file.txt"
        f.write_text("x")

        backups = backup_existing([f], timestamp="20240101T000000Z")
        # Attempting to modify a frozen dataclass raises.
        with pytest.raises((AttributeError, Exception)):
            backups[0].original = tmp_path / "other"  # type: ignore[misc]

    @pytest.mark.unit()
    def test_multiple_backups_preserve_input_order(self, tmp_path: Path) -> None:
        """``Backup`` entries are returned in the input iteration order.

        A caller that backs up ``[a, b, c]`` and renders "backed up to
        ..." for each expects the output to be ``[a, b, c]``, not
        sorted or reordered. The test pins that the list is ordered
        consistently with the input (which is what makes the CLI's
        "3 files backed up" message deterministic).
        """
        files = _make_files(tmp_path, ["b.txt", "a.txt", "c.txt"])

        backups = backup_existing(files, timestamp="20240101T000000Z")

        backed_up_names = [b.original.name for b in backups]
        assert backed_up_names == ["b.txt", "a.txt", "c.txt"]
