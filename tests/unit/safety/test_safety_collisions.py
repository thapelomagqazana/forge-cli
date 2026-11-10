"""Contract tests for ``detect_collisions`` and ``raise_if_collisions``.

Where this file sits
--------------------
``detect_collisions`` is the *third* gate in the safety layer, after
``safe_resolve`` (path structure) and ``validate_destination`` (single
destination readiness). It answers a *plan-level* question that neither
of the previous gates can:

    ``safe_resolve``          — "is this path structurally safe?"
    ``validate_destination``  — "is this single destination ready?"
    ``detect_collisions``     — "does the plan, as a whole, overwrite
                                 anything already on disk?"

The scope difference matters:

    * ``validate_destination`` takes *one* ``Path``. It answers "can
      I write here?".
    * ``detect_collisions`` takes a ``plan`` — a list of
      ``PlannedFile`` entries — and the ``destination_root`` those
      entries are relative to. It answers "does the plan collide with
      what is already on disk?".

Two collision classes
---------------------
The module detects two distinct collisions:

1. **Exact disk collisions** (``collision.exists``). The plan's
   destination matches an entry already on disk. The remediation is
   ``--overwrite`` (which the caller pairs with ``backup_existing``
   for data preservation).

2. **Case-insensitive plan duplicates**
   (``plan.case_insensitive_duplicate``). The plan itself contains
   two destinations that differ only in case (``README.md`` vs.
   ``readme.md``). On a case-insensitive filesystem (macOS, Windows,
   many Docker volumes) these are the *same* file, and one will
   silently overwrite the other at write time. This is a *plan
   defect*, not a disk state, and it is *not* suppressed by
   ``overwrite=True``: the flag consents to replacing *disk* files,
   not to a plan that behaves differently on different filesystems.

A third finding class — ``collision.inspect_failed`` — is emitted
when ``lstat`` on a destination raises an unexpected ``OSError``. The
detector fails closed (treats the destination as existing) so a
permission error or I/O failure cannot silently allow a write.

Why ``overwrite`` is a required parameter
-----------------------------------------
Same reasoning as ``validate_destination``: a silent default would
hide the choice at the call site. The tests exercise both values, and
each test's name says which policy it is testing.

Why ``overwrite=True`` does *not* suppress case duplicates
----------------------------------------------------------
``--overwrite`` consents to replacing files that already exist. It
does *not* consent to a plan that will behave differently on macOS
than on Linux — those are orthogonal concerns, and conflating them
would let a blueprint author accidentally ship a plan that works on
one filesystem and destroys data on another. The tests pin the
distinction explicitly:

    * ``test_overwrite_true_suppresses_disk_collision`` — an exact
      disk collision *is* suppressed by ``overwrite=True``.
    * ``test_case_duplicate_not_suppressed_by_overwrite`` — a plan
      duplicate is *not* suppressed.

A future change that folded these together would fail both tests,
and the failure would name the property that changed.

Which ``CollisionError`` these tests catch
------------------------------------------
There are *two* classes named ``CollisionError`` in the codebase:

    * ``forge.safety.errors.CollisionError`` — the safety-layer
      primitive. This is what ``raise_if_collisions`` raises, and
      what the tests below catch. Its ``findings`` attribute is a
      tuple of ``Finding`` objects (the safety layer's own type).
    * ``forge.core.exceptions.CollisionError`` — the CLI-bridge
      subclass. This is what ``forge.cli.errors.handle_exception``
      dispatches on, and what the exit-code contract (ADR-0017)
      documents. Its ``findings`` attribute is a tuple of
      ``(code, path, message, hint)`` tuples — a lossy projection
      of ``Finding`` — because the CLI boundary does not need the
      ``root`` field.

The translation happens in ``create._generate``'s
``_raise_safety_error`` helper: it catches
``forge.safety.errors.SafetyError`` / ``CollisionError``, projects
the findings into the four-tuple shape, and re-raises the CLI-bridge
type. The tests for *that* translation live in
``tests/unit/cli/test_create_dual_mode.py``, not here — this file
tests the safety-layer primitives in isolation.

Importing the safety-layer ``CollisionError`` here (rather than the
CLI-bridge one) is not just a mechanical fix; it is the correct
boundary. ``forge.safety`` must not depend on ``forge.core`` (the
dependency direction documented in ``safety/__init__.py``), so the
tests for its raising behaviour must exercise the type it actually
raises. A test that imported the CLI-bridge type would pass only by
accident (via ``pytest.raises``'s behaviour of catching non-matching
exceptions during traceback reattachment) and would break the moment
the CLI-bridge type gained a distinguishing field.

Filesystem-dependent tests
--------------------------
The disk-collision tests need real files on disk. ``tmp_path``
provides them — per-test, uniquely owned, cleaned up automatically.
No hardcoded ``/tmp`` (``S108``).

The case-duplicate tests are *filesystem-independent*: they only
inspect the *plan*, not disk. They run on every platform and prove
the check is about the plan, not the underlying filesystem's case
sensitivity.

Why ``raise_if_collisions`` is tested here
------------------------------------------
``raise_if_collisions`` is the exception-raising companion to
``detect_collisions``. It exists because the caller (typically the
``create`` command) aggregates findings from all three gates before
deciding to fail. When it decides to fail, it needs the correct
exception type (``CollisionError``), not a generic ``SafetyError`` —
because ``CollisionError`` maps to exit code 6 (retry with
``--overwrite``) and ``SafetyError`` maps to exit code 3 (fix the
plan). The tests here pin that distinction.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from forge.planning import PlannedFile, PlannedFileKind
from forge.safety import (
    detect_collisions,
    raise_if_collisions,
)
from forge.safety.errors import CollisionError


# =============================================================================
# Helpers
# =============================================================================
def _planned(dest: str, *, source: str = "src.j2") -> PlannedFile:
    """Build a minimal ``PlannedFile`` for collision tests.

    The ``source`` and ``kind`` are irrelevant to collision detection —
    the detector only reads ``destination``. But ``PlannedFile``
    requires all three fields (it is a frozen Pydantic model with
    ``extra="forbid"``), so every test must supply them. The defaults
    keep the test bodies focused on the destination, which is what the
    test is actually exercising.
    """
    return PlannedFile(source=source, destination=dest, kind=PlannedFileKind.RENDER)


# =============================================================================
# Group 1: the happy path — a plan with no collisions
# =============================================================================
class TestAcceptsCollisionFreePlan:
    """Plans that do not collide with disk produce no findings.

    The negative cases are as important as the positive ones: a
    detector that fired findings unconditionally would pass every
    "collision is detected" test and be useless in practice.
    """

    @pytest.mark.unit()
    def test_empty_plan_produces_no_findings(self, tmp_path: Path) -> None:
        """An empty plan has nothing to collide with."""
        findings = detect_collisions([], tmp_path, overwrite=False)
        assert findings == []

    @pytest.mark.unit()
    def test_plan_with_no_disk_entries_produces_no_findings(self, tmp_path: Path) -> None:
        """A plan targeting files that do not exist is collision-free."""
        plan = [_planned("a.txt"), _planned("b/c.txt"), _planned("d/e/f.txt")]
        findings = detect_collisions(plan, tmp_path, overwrite=False)
        assert findings == []

    @pytest.mark.unit()
    def test_empty_destination_root_is_accepted(self, tmp_path: Path) -> None:
        """A destination root with no entries is a valid starting state.

        The detector does not require the destination root to exist —
        it iterates the plan and ``lstat``-s each destination. If the
        root does not exist, all ``lstat``-s raise
        ``FileNotFoundError``, which the detector treats as "no
        collision". That is the correct behaviour: a plan to write
        into a not-yet-created directory has no collisions.
        """
        nonexistent_root = tmp_path / "does-not-exist"
        plan = [_planned("file.txt")]
        findings = detect_collisions(plan, nonexistent_root, overwrite=False)
        assert findings == []


# =============================================================================
# Group 2: exact disk collisions (``collision.exists``)
# =============================================================================
class TestExactDiskCollision:
    """A planned destination that matches an on-disk entry is a collision.

    This is the *recoverable* collision: the caller can pass
    ``overwrite=True`` (with backups) to proceed. The tests pin both
    sides — the collision fires when ``overwrite=False`` and is
    suppressed when ``overwrite=True``.
    """

    @pytest.mark.unit()
    def test_existing_file_triggers_collision(self, tmp_path: Path) -> None:
        """A plan targeting an existing file triggers ``collision.exists``."""
        (tmp_path / "existing.txt").write_text("x")
        plan = [_planned("existing.txt")]

        findings = detect_collisions(plan, tmp_path, overwrite=False)
        assert any(f.code == "collision.exists" for f in findings)

    @pytest.mark.unit()
    def test_existing_directory_triggers_collision(self, tmp_path: Path) -> None:
        """A plan targeting an existing directory is a collision.

        The detector uses ``lstat`` (entry-based, not type-based), so a
        directory occupies the destination just as a file does. The
        remediation is the same in either case (``--overwrite``), so
        the finding is identical.
        """
        (tmp_path / "existing_dir").mkdir()
        plan = [_planned("existing_dir")]

        findings = detect_collisions(plan, tmp_path, overwrite=False)
        assert any(f.code == "collision.exists" for f in findings)

    @pytest.mark.unit()
    def test_broken_symlink_triggers_collision(self, tmp_path: Path) -> None:
        """A plan targeting a broken symlink is a collision.

        ``Path.exists()`` returns False for a broken symlink (target
        does not exist), but ``lstat`` reports the entry itself. The
        name is in use, and overwriting it would replace the symlink
        — which is a data-preservation problem regardless of whether
        the target exists. The detector uses ``lstat`` for this
        reason (same discipline as ``validate_destination``'s
        ``_lstat_exists``).
        """

        (tmp_path / "broken_link").symlink_to(tmp_path / "does-not-exist")
        plan = [_planned("broken_link")]

        findings = detect_collisions(plan, tmp_path, overwrite=False)
        assert any(f.code == "collision.exists" for f in findings)

    @pytest.mark.unit()
    def test_overwrite_true_suppresses_disk_collision(self, tmp_path: Path) -> None:
        """``overwrite=True`` suppresses ``collision.exists``.

        The caller (typically ``create --overwrite``) has consented to
        replacing existing files. The detector does *not* perform the
        backups — that is ``backup_existing``'s job, called by the
        caller before staging. The detector only reports, and the
        caller decides what to do with the report.
        """
        (tmp_path / "existing.txt").write_text("x")
        plan = [_planned("existing.txt")]

        findings = detect_collisions(plan, tmp_path, overwrite=True)
        assert not any(f.code == "collision.exists" for f in findings)

    @pytest.mark.unit()
    def test_multiple_collisions_all_reported(self, tmp_path: Path) -> None:
        """Every collision is reported, not just the first.

        The batch-reporting promise applies here as it does
        throughout the safety layer: a plan with five collisions
        should produce five findings, so the user fixes all of them
        in one pass.
        """
        (tmp_path / "a.txt").write_text("x")
        (tmp_path / "b.txt").write_text("y")
        (tmp_path / "c.txt").write_text("z")
        plan = [_planned("a.txt"), _planned("b.txt"), _planned("c.txt")]

        findings = detect_collisions(plan, tmp_path, overwrite=False)
        collision_paths = {f.path for f in findings if f.code == "collision.exists"}
        assert collision_paths == {"a.txt", "b.txt", "c.txt"}


# =============================================================================
# Group 3: case-insensitive plan duplicates
# =============================================================================
class TestCaseInsensitiveDuplicate:
    """A plan with ``README.md`` *and* ``readme.md`` is a defect.

    This is *not* a disk collision — it is a plan-internal duplicate
    that will collapse into a single file on case-insensitive
    filesystems. The detector reports it because:

    * the plan will behave differently on macOS (one file) vs. Linux
      (two files),
    * the difference is invisible to a user who only tested on one OS,
    * overwrite consent does *not* apply, because there is nothing on
      disk to consent to replacing.

    The tests pin all three properties.
    """

    @pytest.mark.unit()
    def test_case_duplicate_is_reported(self, tmp_path: Path) -> None:
        """Two destinations differing only in case trigger the finding."""
        plan = [_planned("README.md"), _planned("readme.md")]

        findings = detect_collisions(plan, tmp_path, overwrite=False)
        assert any(f.code == "plan.case_insensitive_duplicate" for f in findings)

    @pytest.mark.unit()
    def test_case_duplicate_not_suppressed_by_overwrite(self, tmp_path: Path) -> None:
        """``overwrite=True`` does *not* suppress the case-duplicate finding.

        The flag consents to replacing *disk* files. It does not
        consent to a plan that will behave differently on different
        filesystems. The two are orthogonal concerns, and the tests
        pin the distinction.
        """
        plan = [_planned("README.md"), _planned("readme.md")]

        findings = detect_collisions(plan, tmp_path, overwrite=True)
        assert any(f.code == "plan.case_insensitive_duplicate" for f in findings)

    @pytest.mark.unit()
    def test_case_duplicate_ignored_when_case_distinct(self, tmp_path: Path) -> None:
        """Two distinct destinations are *not* case duplicates.

        A false positive would be worse than a false negative: it
        would reject every legitimate plan on a case-insensitive
        filesystem. The test pins that the duplicate check is
        case-insensitive-but-strict — it fires only when
        ``casefold()`` collides.
        """
        plan = [_planned("readme.md"), _planned("main.py")]

        findings = detect_collisions(plan, tmp_path, overwrite=False)
        assert not any(f.code == "plan.case_insensitive_duplicate" for f in findings)

    @pytest.mark.unit()
    def test_three_way_case_duplicate_reported(self, tmp_path: Path) -> None:
        """Three spellings of the same name are a duplicate set.

        The detector reports the *first* prior spelling it saw, so a
        plan with ``README.md``, ``Readme.md``, and ``readme.md``
        produces at least one finding. The test asserts one finding
        is present (not necessarily three) — the report needs to name
        the problem, not exhaustively enumerate permutations.
        """
        plan = [_planned("README.md"), _planned("Readme.md"), _planned("readme.md")]

        findings = detect_collisions(plan, tmp_path, overwrite=False)
        assert any(f.code == "plan.case_insensitive_duplicate" for f in findings)


# =============================================================================
# Group 4: inspection failures (``collision.inspect_failed``)
# =============================================================================
class TestInspectionFailure:
    """An unexpected ``OSError`` while checking a destination fails closed.

    The detector uses ``lstat`` to check each destination. If
    ``lstat`` raises ``OSError`` (permission denied on the parent,
    I/O error on the filesystem, etc.), we *cannot know* whether the
    destination exists. The correct answer is a ``collision.inspect_failed``
    finding, not "assume it does not exist" — the latter would silently
    allow an overwrite.

    Testing this requires simulating an ``OSError`` that is not
    ``FileNotFoundError``. The test monkeypatches ``os.lstat`` to
    raise for the specific destination, which pins the branch in
    isolation.
    """

    @pytest.mark.unit()
    def test_oserror_during_lstat_reports_finding(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An ``OSError`` that is not ``FileNotFoundError`` fails closed."""
        target = tmp_path / "unknown.txt"
        original_lstat = os.lstat

        def _fake_lstat(path):
            # Only fail for our specific target. The detector also
            # lstat-s the destination root and other components; we
            # do not want to fake those or the test becomes a
            # monkeypatch test rather than a collision test.
            if Path(path) == target:
                raise PermissionError(13, "Permission denied")
            return original_lstat(path)

        monkeypatch.setattr(os, "lstat", _fake_lstat)
        plan = [_planned("unknown.txt")]

        findings = detect_collisions(plan, tmp_path, overwrite=False)
        assert any(f.code == "collision.inspect_failed" for f in findings)


# =============================================================================
# Group 5: ``raise_if_collisions`` — the aggregate-failure path
# =============================================================================
class TestRaiseIfCollisions:
    """``raise_if_collisions`` translates findings into a ``CollisionError``.

    The caller aggregates findings from all three safety gates, then
    calls this function to decide whether to fail. The function raises
    ``forge.safety.errors.CollisionError`` — *not* the CLI-bridge
    class of the same name in ``forge.core.exceptions`` — so the
    translation from safety-layer exception to CLI-boundary exception
    is the caller's responsibility (see ``create._generate``'s
    ``_raise_safety_error``).

    Why the tests import the *safety-layer* ``CollisionError``:
        ``raise_if_collisions`` is a *primitive* in the safety layer.
        The safety layer must not depend on ``forge.core``
        (documented in ``safety/__init__.py``'s dependency-direction
        section), so it cannot raise the CLI-bridge type. The bridge
        lives in ``create._generate``, which catches the safety-layer
        exception and re-raises the CLI type with the same findings.
        The tests for that translation live in
        ``tests/unit/cli/test_create_dual_mode.py``, not here.

    Why this is a separate class from ``TestExactDiskCollision``: the
    *detection* tests prove the findings are produced. The *raising*
    tests prove the findings are correctly translated into the
    exception the boundary code needs. Two different contracts, two
    different classes.
    """

    @pytest.mark.unit()
    def test_raises_when_collisions_present(self, tmp_path: Path) -> None:
        """An aggregated findings list with a collision raises."""
        (tmp_path / "existing.txt").write_text("x")
        plan = [_planned("existing.txt")]
        findings = detect_collisions(plan, tmp_path, overwrite=False)

        with pytest.raises(CollisionError) as info:
            raise_if_collisions(findings, source="create")

        assert info.value.operation == "create"

    @pytest.mark.unit()
    def test_raises_for_case_duplicate(self, tmp_path: Path) -> None:
        """A case-duplicate finding raises ``CollisionError``."""
        plan = [_planned("README.md"), _planned("readme.md")]
        findings = detect_collisions(plan, tmp_path, overwrite=False)

        with pytest.raises(CollisionError):
            raise_if_collisions(findings, source="create")

    @pytest.mark.unit()
    def test_does_not_raise_when_no_collisions(self, tmp_path: Path) -> None:
        """An empty findings list does not raise."""
        raise_if_collisions([], source="create")  # no exception

    @pytest.mark.unit()
    def test_does_not_raise_for_non_collision_findings(self, tmp_path: Path) -> None:
        """Non-collision safety findings do not raise ``CollisionError``.

        The caller aggregates findings from *all* safety gates — some
        are collision findings, some are structural safety findings
        (``path.escapes_root``, ``destination.symlink``, etc.). Only
        the collision-class findings should trigger a
        ``CollisionError``; the structural findings must be routed to
        ``SafetyError`` by the caller. The test pins that
        ``raise_if_collisions`` ignores non-collision findings, so a
        caller who passes a mixed list gets the correct exception
        type for the collision part.
        """
        from forge.safety.errors import Finding

        non_collision = Finding(
            code="path.escapes_root",
            path="bad",
            message="x",
            hint="y",
        )
        # No raise: the finding is not a collision.
        raise_if_collisions([non_collision], source="create")

    @pytest.mark.unit()
    def test_collision_error_carries_findings(self, tmp_path: Path) -> None:
        """The raised ``CollisionError`` carries every collision finding.

        The safety-layer exception carries the original ``Finding``
        objects — not a lossy tuple projection. The CLI bridge in
        ``create._generate`` does the projection; the safety layer
        preserves the full finding shape so that a future consumer
        (a JSON renderer, a logging integration) can see everything
        the detector saw.
        """
        (tmp_path / "a.txt").write_text("x")
        (tmp_path / "b.txt").write_text("y")
        plan = [_planned("a.txt"), _planned("b.txt")]
        findings = detect_collisions(plan, tmp_path, overwrite=False)

        with pytest.raises(CollisionError) as info:
            raise_if_collisions(findings, source="create")

        raised_findings = info.value.findings
        assert len(raised_findings) == 2
        # Each is a `Finding` object with the four fields the CLI
        # renderer needs, plus the optional `root` field.
        for finding in raised_findings:
            assert finding.code == "collision.exists"
            assert finding.path in {"a.txt", "b.txt"}
            assert finding.message
            assert finding.hint


# =============================================================================
# Group 6: finding shape (the caller's contract)
# =============================================================================
class TestFindingShape:
    """Every finding has the shape the CLI renderer relies on.

    Same discipline as ``test_paths.py`` and ``test_destinations.py``:
    a finding without ``code``, ``path``, ``message``, or ``hint`` is
    a dead end. The tests here pin the shape for the two finding codes
    ``detect_collisions`` can emit on its own (``collision.exists``
    and ``plan.case_insensitive_duplicate``).
    """

    @pytest.mark.unit()
    def test_disk_collision_finding_shape(self, tmp_path: Path) -> None:
        """A ``collision.exists`` finding carries all four fields."""
        (tmp_path / "existing.txt").write_text("x")
        plan = [_planned("existing.txt")]

        findings = detect_collisions(plan, tmp_path, overwrite=False)
        collision = next(f for f in findings if f.code == "collision.exists")
        assert collision.path
        assert collision.message
        assert collision.hint

    @pytest.mark.unit()
    def test_case_duplicate_finding_shape(self, tmp_path: Path) -> None:
        """A ``plan.case_insensitive_duplicate`` finding carries all four fields."""
        plan = [_planned("README.md"), _planned("readme.md")]

        findings = detect_collisions(plan, tmp_path, overwrite=False)
        dup = next(f for f in findings if f.code == "plan.case_insensitive_duplicate")
        assert dup.path
        assert dup.message
        assert dup.hint

    @pytest.mark.unit()
    def test_ordering_is_stable(self, tmp_path: Path) -> None:
        """The findings list is sorted by ``path``.

        Determinism matters for golden-file tests and for log
        comparison. The detector sorts its findings by ``path``
        before returning, so the same inputs produce the same order
        on every run — the tests in ``test_create_dual_mode.py``
        rely on this for their stdout assertions.
        """
        (tmp_path / "b.txt").write_text("x")
        (tmp_path / "a.txt").write_text("y")
        plan = [_planned("b.txt"), _planned("a.txt")]

        findings = detect_collisions(plan, tmp_path, overwrite=False)
        paths = [f.path for f in findings]
        assert paths == sorted(paths)
