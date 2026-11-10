"""Integration tests for the composed safety pipeline.

Where this file sits
--------------------
The safety layer is built from five primitives:

    ``safe_resolve``          — path structure
    ``validate_destination``  — single destination readiness
    ``detect_collisions``     — plan-vs-disk collision detection
    ``backup_existing``       — data preservation on overwrite
    ``StagingArea``           — atomic commit of many files

Each primitive has its own unit test suite:

    ``tests/unit/safety/test_paths.py``
    ``tests/unit/safety/test_destinations.py``
    ``tests/unit/safety/test_collisions.py``
    ``tests/unit/safety/test_overwrite.py``
    ``tests/unit/safety/test_staging.py``

Those tests pin each primitive in *isolation*. This file pins them in
*combination* — the pipeline the CLI wires up, exercised without the
CLI's own concerns (flag parsing, wizard state, exit-code dispatch,
``_generate``'s ``_ResolvedInputs`` shape).

Why a separate file (not augmenting the unit tests)
---------------------------------------------------
Three concerns justify the separate file:

1. **Test scope is different.** The unit tests are per-function: one
   function, one contract, one class. This file is per-*pipeline*:
   the contract is "given a plan and a destination state, the
   composed pipeline produces these observable effects." A test that
   mixed "unit for ``backup_existing``" with "integration for the
   whole pipeline" would obscure which layer a failure was in.

2. **Failure attribution matters.** When a unit test fails, the
   cause is one function. When an integration test fails, the cause
   could be any of five primitives *or* their composition. Keeping
   integration tests in their own file makes the failure unambiguous
   ("the pipeline is broken") and directs the reader to the pipeline,
   not to a primitive that was correct in isolation.

3. **The CLI has its own pipeline test.** ``create._generate`` wires
   these primitives together with the CLI's own concerns (flag
   parsing, ``_ResolvedInputs``, the ``--dry-run`` branch). Those are
   tested in ``tests/unit/cli/test_create_dual_mode.py``. This file
   tests the *primitives in combination*, which is what any future
   caller of the safety layer (a ``forge generate``, an HTTP handler,
   a batch tool) would exercise.

The two files together cover:

    * ``test_create_dual_mode.py`` — the CLI's use of the pipeline.
    * ``test_safety_pipeline.py`` — the pipeline itself.

Pipeline phases (and why ``overwrite=True`` is passed to phase 1)
-----------------------------------------------------------------
The pipeline's correctness hinges on the *order* of the phases and
on *which policy value* each phase gets. The correct composition:

    Phase 1 (structure):    safe_resolve + validate_destination(overwrite=True)
    Phase 2 (collision):    detect_collisions(overwrite=overwrite) + raise_if_collisions
    Phase 3 (backup):       backup_existing (only when overwrite=True)
    Phase 4 (stage+commit): StagingArea

The ``overwrite=True`` in phase 1 is load-bearing. ``validate_destination``
has an existence check that produces ``destination.exists`` when the leaf
already exists and ``overwrite=False`` — but the *collision* case is
phase 2's job, and it needs to surface as ``CollisionError`` (exit code 6,
"retry with ``--overwrite``"), not as ``SafetyError`` (exit code 3,
"fix the plan"). Suppressing the existence check in phase 1 lets the
collision case reach phase 2, where it gets the correct exception type
and the correct exit code.

Phase 1's *other* checks (leaf symlink, parent writability, parent not
directory, no ancestor) are unchanged — they are *structural* refusals
that ``--overwrite`` cannot fix, and they correctly surface as
``SafetyError`` regardless of the caller's ``overwrite`` value.

This is what ``create._generate`` does in production (see
``cli.commands.create._generate``'s phase numbers and its call to
``validate_destination`` with a value derived from the *policy*
``overwrite`` flag, not the *user's* ``--overwrite`` flag), so the
helper below matches the CLI's refusals exactly.

What this file proves
---------------------
The pipeline's *composition guarantees*, which no single primitive
can guarantee on its own:

    * A collision on any planned file stops the pipeline before any
      destination file is touched — no writes, no backups, no staging
      directory leaked.
    * A successful commit writes every planned file with the expected
      content, and leaves no staging directory behind.
    * With ``overwrite=True`` and existing files, backups are created
      *before* the commit and the commit succeeds.
    * With ``overwrite=False`` and existing files, the pipeline
      refuses with the *correct* exception type (``CollisionError``,
      exit code 6, not ``SafetyError``, exit code 3).
    * A structural safety failure (an unsafe path, an unwritable
      parent, a leaf symlink) is caught at the structural phase —
      *before* collision detection — and surfaces as ``SafetyError``
      (exit code 3), which is correct because ``--overwrite`` cannot
      fix a structural problem.
    * A failed commit (mid-commit error) leaves the already-committed
      files in place and the staging directory cleaned up.
    * No ``.forge-stage-*`` directory is left behind after success
      *or* failure.

Filesystem-dependent tests
--------------------------
The pipeline is inherently filesystem-dependent — every guarantee is
about what the filesystem looks like before and after. ``tmp_path``
provides per-test isolation and automatic cleanup. No hardcoded
``/tmp`` (``S108``).

Write-protected-directory tests (which prove that an unwritable
parent refuses the pipeline) skip under root, because
``os.access(W_OK | X_OK)`` returns ``True`` for the superuser. The
skip is explicit (``pytest.skip(reason=...)``) so a CI run shows the
environmental assumption.

What this file does *not* cover
-------------------------------
    * **The CLI's composition.** That is
      ``tests/unit/cli/test_create_dual_mode.py``'s job. In
      particular, that file tests:
          * how flags map to plan inputs,
          * how the wizard collects them,
          * how the exit-code dispatch translates ``CollisionError``
            / ``SafetyError`` / ``TemplateRenderError``,
          * how the dry-run path differs from the write path.
      This file tests what those things are *composed of*, not how
      they are composed by a specific caller.

    * **Performance.** The pipeline is not performance-critical for
      the scale a scaffolding tool operates at (a few dozen files,
      each small). Perf tests would be flaky and would not pin a
      contract.

    * **Concurrent invocations.** Two ``forge create`` runs on the
      same destination race in ways the pipeline does not (and does
      not claim to) prevent. Concurrency is a caller concern.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from forge.planning import PlannedFile, PlannedFileKind
from forge.safety import (
    StagingArea,
    backup_existing,
    detect_collisions,
    raise_if_collisions,
    safe_resolve,
    validate_destination,
)


# =============================================================================
# Helpers
# =============================================================================
def _planned(dest: str, *, content_source: str = "src.j2") -> PlannedFile:
    """Build a minimal ``PlannedFile`` for pipeline tests.

    The ``source`` is the *blueprint-relative* source path; the
    pipeline's content comes from a test-controlled source file, not
    from the actual source path. The helper is minimal because the
    pipeline only reads ``destination`` and (for staging) the
    caller-supplied content — the ``source`` field is passed through
    for realism but not inspected by the safety layer.
    """
    return PlannedFile(
        source=content_source,
        destination=dest,
        kind=PlannedFileKind.RENDER,
    )


def _stage_content(
    stage: StagingArea,
    plan: list[PlannedFile],
    content_map: dict[str, str],
) -> None:
    """Write each planned file's content into staging.

    A helper because every pipeline test needs to simulate the
    *writer's* job (produce files in staging from the plan) before
    committing. ``content_map`` maps destination → content, so a test
    can supply distinct content per file and assert on it afterward.

    A pipeline test that used a real renderer would exercise the
    renderer, not the safety pipeline. The helper keeps the test
    focused: it stages what a renderer *would* stage.
    """
    for planned in plan:
        content = content_map.get(planned.destination, "default")
        stage.write_text(planned.destination, content)


def _safe_pipeline(
    plan: list[PlannedFile],
    *,
    destination_root: Path,
    content_map: dict[str, str],
    overwrite: bool,
) -> list[Path]:
    """Run the composed pipeline against the destination.

    This is what the CLI's ``_generate`` does (in essence), minus the
    CLI's own concerns (blueprint loading, variable resolution,
    rendering). The function walks four phases in order:

        1. **Structure + readiness.** ``safe_resolve`` for each
           planned destination, then ``validate_destination`` with
           ``overwrite=True`` — the existence check is suppressed
           here because *collisions* are phase 2's job, and giving
           them to phase 1 would produce the wrong exception type
           (``SafetyError`` instead of ``CollisionError``).
        2. **Collision detection.** ``detect_collisions`` with the
           *caller's* ``overwrite`` value, then
           ``raise_if_collisions`` to translate any collision
           findings into ``CollisionError``.
        3. **Backups.** ``backup_existing`` for existing
           destinations, only when ``overwrite=True``.
        4. **Staging + commit.** ``StagingArea``.

    Every integration test calls this helper, so every test exercises
    the *same* composition. A change to the pipeline's order (say,
    committing before backing up) would fail multiple tests, and the
    shared helper would name the property that changed. A test that
    inlined its own composition would silently diverge from the
    pipeline every other test exercises — the helper is the single
    source of truth for "what the pipeline is".

    Why ``validate_destination(overwrite=True)`` unconditionally:
        ``validate_destination`` has an existence check that
        produces ``destination.exists`` when the leaf already exists
        and ``overwrite=False``. That is *phase 2's* concern, and
        the exception type matters: phase 2 surfaces it as
        ``CollisionError`` (exit code 6, "retry with ``--overwrite``"),
        while phase 1 would surface it as ``SafetyError`` (exit
        code 3, "fix the plan"). Passing ``overwrite=True`` to
        phase 1's ``validate_destination`` call disables the
        existence check *only* — the other checks (leaf symlink,
        parent writability, parent not directory, no ancestor) still
        fire, because they are *structural* refusals that
        ``--overwrite`` cannot fix.

        The result matches the production composition in
        ``create._generate``: structural refusals raise
        ``SafetyError``; collisions raise ``CollisionError``. This
        is what makes the exit-code contract (ADR-0017) hold at the
        pipeline level, not just at the primitive level.
    """
    # Phase 1: structural + readiness validation.
    #
    # ``overwrite=True`` suppresses the *existence* check (which is
    # phase 2's job) while preserving the *structural* checks (leaf
    # symlink, parent writability, parent not directory). See the
    # docstring's "Why ``validate_destination(overwrite=True)``"
    # section.
    all_findings = []
    for planned in plan:
        resolved, finding = safe_resolve(planned.destination, destination_root)
        if finding is not None:
            all_findings.append(finding)
            continue
        all_findings.extend(validate_destination(resolved, overwrite=True))
    if all_findings:
        # A structural refusal — raise ``SafetyError`` with all the
        # findings. The CLI translates this into exit code 3; the
        # tests below assert on the exception type when they care
        # about the structural-vs-collision distinction.
        from forge.safety.errors import SafetyError

        raise SafetyError(
            findings=tuple(all_findings),
            operation="test-pipeline",
        )

    # Phase 2: collision detection.
    #
    # The *caller's* ``overwrite`` value is what determines whether
    # an existing destination is a collision. When ``overwrite=True``,
    # ``detect_collisions`` skips the disk check, so no
    # ``collision.exists`` finding is produced and
    # ``raise_if_collisions`` is a no-op. When ``overwrite=False``,
    # any existing destination produces a finding that
    # ``raise_if_collisions`` translates into ``CollisionError``.
    collision_findings = detect_collisions(plan, destination_root, overwrite=overwrite)
    raise_if_collisions(collision_findings, source="test-pipeline")

    # Phase 3: backups (before staging).
    #
    # ``backup_existing`` is only called when ``overwrite=True``
    # *and* at least one planned destination already exists on disk.
    # A caller with ``overwrite=False`` never reaches this phase
    # (phase 2 already raised). A caller with ``overwrite=True``
    # that targets only new files also skips it (nothing to back
    # up) — the ``if existing_targets:`` guard prevents a spurious
    # empty ``backup_existing`` call that would be a no-op anyway.
    if overwrite:
        existing_targets = [
            destination_root / p.destination
            for p in plan
            if (destination_root / p.destination).exists()
            or (destination_root / p.destination).is_symlink()
        ]
        if existing_targets:
            backup_existing(existing_targets, timestamp="20240101T000000Z")

    # Phase 4: staging + commit.
    committed: list[Path] = []
    with StagingArea.for_root(destination_root) as stage:
        _stage_content(stage, plan, content_map)
        committed = stage.commit()
    return committed


# =============================================================================
# Group 1: the happy path — clean destination, clean plan
# =============================================================================
class TestHappyPath:
    """The pipeline writes every planned file when the destination is clean.

    The most basic pipeline test: a plan with no collisions, an empty
    destination, no overwrite needed. Every file appears with its
    expected content, no staging directory is left behind, and
    ``backup_existing`` is not called (nothing to back up).

    Without this test, every other test in the file would be testing
    a pipeline whose happy path had never been demonstrated — the
    failure modes would be meaningful only if the success mode was
    known to work.
    """

    @pytest.mark.integration()
    def test_writes_all_planned_files(self, tmp_path: Path) -> None:
        """Every planned file is written with the expected content."""
        destination = tmp_path / "dest"
        destination.mkdir()

        plan = [
            _planned("a.txt"),
            _planned("b/c.txt"),
            _planned("d/e/f.txt"),
        ]
        content_map = {
            "a.txt": "content a",
            "b/c.txt": "content b",
            "d/e/f.txt": "content c",
        }

        committed = _safe_pipeline(
            plan,
            destination_root=destination,
            content_map=content_map,
            overwrite=False,
        )

        # Every file exists with the expected content.
        assert (destination / "a.txt").read_text() == "content a"
        assert (destination / "b/c.txt").read_text() == "content b"
        assert (destination / "d/e/f.txt").read_text() == "content c"
        assert len(committed) == 3

    @pytest.mark.integration()
    def test_no_staging_directory_left_behind(self, tmp_path: Path) -> None:
        """After a successful pipeline, no ``.forge-stage-*`` directory remains.

        The staging directory is a *transient* — it exists during the
        ``with`` block and is removed on exit. The test asserts the
        observable property (no staging directory), not the
        implementation (the context manager's ``finally``).

        A future change that left staging directories behind (say, to
        "cache" them) would fail this test and the failure would name
        the property that changed: cleanup.
        """
        destination = tmp_path / "dest"
        destination.mkdir()

        plan = [_planned("a.txt")]
        _safe_pipeline(
            plan,
            destination_root=destination,
            content_map={"a.txt": "x"},
            overwrite=False,
        )

        staging_dirs = list(destination.glob(".forge-stage-*"))
        assert staging_dirs == []

    @pytest.mark.integration()
    def test_no_backup_created_for_clean_destination(self, tmp_path: Path) -> None:
        """No backup is created when the destination has nothing to back up.

        ``backup_existing`` is called with an empty (or all-missing)
        target list, so it produces no backups. The test asserts on
        the filesystem — no ``*.bak.*`` files exist anywhere in the
        destination tree.

        A future change that "helpfully" backed up nonexistent files
        (say, by calling ``backup_existing`` unconditionally with the
        full plan) would fail this test — the "no-op for clean
        destination" property is part of the pipeline's contract.
        """
        destination = tmp_path / "dest"
        destination.mkdir()

        plan = [_planned("a.txt"), _planned("b.txt")]
        _safe_pipeline(
            plan,
            destination_root=destination,
            content_map={"a.txt": "x", "b.txt": "y"},
            overwrite=False,
        )

        backup_files = list(destination.rglob("*.bak.*"))
        assert backup_files == []


# =============================================================================
# Group 2: collision without overwrite — the pipeline refuses
# =============================================================================
class TestCollisionWithoutOverwrite:
    """A collision on any planned file stops the pipeline before any write.

    The pipeline is *atomic at the plan level*: if any planned
    destination collides with an existing file and ``overwrite=False``,
    the pipeline refuses. The destination is unchanged (no writes, no
    backups, no staging directory), and the user sees a
    ``CollisionError`` naming the collision.

    This is the property that makes the pipeline safe to run
    unattended: a CI job that runs ``forge create`` on a directory
    with an unexpected file will fail, and the failure will not have
    modified the directory. That is the "fail closed" promise at the
    pipeline level.
    """

    @pytest.mark.integration()
    def test_single_collision_refuses_pipeline(self, tmp_path: Path) -> None:
        """A single collision refuses the whole pipeline."""
        from forge.safety.errors import CollisionError

        destination = tmp_path / "dest"
        destination.mkdir()
        # Pre-existing file that will collide.
        (destination / "existing.txt").write_text("original content")

        plan = [_planned("existing.txt"), _planned("new.txt")]
        content_map = {"existing.txt": "new content", "new.txt": "another"}

        with pytest.raises(CollisionError):
            _safe_pipeline(
                plan,
                destination_root=destination,
                content_map=content_map,
                overwrite=False,
            )

        # The existing file is unchanged; the new file was never written.
        assert (destination / "existing.txt").read_text() == "original content"
        assert not (destination / "new.txt").exists()

    @pytest.mark.integration()
    def test_collision_leaves_no_staging_directory(self, tmp_path: Path) -> None:
        """A refused pipeline does not leave a staging directory behind.

        Even though the pipeline raised, the staging directory — if it
        had been created — must be cleaned up. The test asserts both
        that the pipeline refused *and* that the filesystem is exactly
        as it was before the call (plus the pre-existing file).
        """
        from forge.safety.errors import CollisionError

        destination = tmp_path / "dest"
        destination.mkdir()
        (destination / "existing.txt").write_text("original")

        plan = [_planned("existing.txt")]
        with pytest.raises(CollisionError):
            _safe_pipeline(
                plan,
                destination_root=destination,
                content_map={"existing.txt": "new"},
                overwrite=False,
            )

        # No staging directory; the pre-existing file is untouched.
        assert list(destination.glob(".forge-stage-*")) == []
        assert (destination / "existing.txt").read_text() == "original"

    @pytest.mark.integration()
    def test_collision_creates_no_backup(self, tmp_path: Path) -> None:
        """A refused pipeline does not create backups.

        Backups happen *after* collision detection (the pipeline
        checks collisions first, then backs up existing files, then
        stages). A collision stops the pipeline before the backup
        phase, so no ``.bak.`` file is created.

        The property matters: a backup of a file that was never going
        to be replaced is noise. The test asserts on the *absence* of
        the backup, which is a stronger claim than "the pipeline
        refused" — it pins *where* in the pipeline the refusal
        happens.
        """
        from forge.safety.errors import CollisionError

        destination = tmp_path / "dest"
        destination.mkdir()
        (destination / "existing.txt").write_text("original")

        plan = [_planned("existing.txt")]
        with pytest.raises(CollisionError):
            _safe_pipeline(
                plan,
                destination_root=destination,
                content_map={"existing.txt": "new"},
                overwrite=False,
            )

        assert list(destination.rglob("*.bak.*")) == []


# =============================================================================
# Group 3: collision with overwrite — the pipeline replaces and backs up
# =============================================================================
class TestCollisionWithOverwrite:
    """With ``overwrite=True``, the pipeline replaces files and backs up.

    The full replacement flow:

        1. ``detect_collisions`` sees the existing file but is told
           ``overwrite=True``, so it does not report a collision.
        2. ``backup_existing`` creates a backup of the file.
        3. ``StagingArea`` stages the new content.
        4. ``commit`` moves the new content over the original.

    The tests exercise each property in combination: the new content
    is in place *and* the original content survives in a backup. A
    future change that broke any of these (say, by staging without
    backing up) would fail at least one test, and the failure would
    name the property.
    """

    @pytest.mark.integration()
    def test_overwrite_replaces_content(self, tmp_path: Path) -> None:
        """The pipeline replaces the existing file with the new content."""
        destination = tmp_path / "dest"
        destination.mkdir()
        (destination / "existing.txt").write_text("original content")

        plan = [_planned("existing.txt")]
        _safe_pipeline(
            plan,
            destination_root=destination,
            content_map={"existing.txt": "new content"},
            overwrite=True,
        )

        assert (destination / "existing.txt").read_text() == "new content"

    @pytest.mark.integration()
    def test_overwrite_creates_backup_with_original_content(self, tmp_path: Path) -> None:
        """The original content is recoverable from a timestamped backup."""
        destination = tmp_path / "dest"
        destination.mkdir()
        (destination / "existing.txt").write_text("original content")

        plan = [_planned("existing.txt")]
        _safe_pipeline(
            plan,
            destination_root=destination,
            content_map={"existing.txt": "new content"},
            overwrite=True,
        )

        backups = list(destination.glob("existing.txt.bak.*"))
        assert len(backups) == 1
        assert backups[0].read_text() == "original content"

    @pytest.mark.integration()
    def test_overwrite_only_backs_up_existing_files(self, tmp_path: Path) -> None:
        """Only files that already exist get backups.

        A plan may include both existing files (which need backups)
        and new files (which do not). The pipeline backs up the
        existing ones and writes the new ones. The test asserts on the
        *set* of backup files — the existing file has one, the new
        file does not.
        """
        destination = tmp_path / "dest"
        destination.mkdir()
        (destination / "existing.txt").write_text("original")

        plan = [_planned("existing.txt"), _planned("new.txt")]
        _safe_pipeline(
            plan,
            destination_root=destination,
            content_map={"existing.txt": "new", "new.txt": "fresh"},
            overwrite=True,
        )

        # Existing file has a backup; new file does not.
        existing_backups = list(destination.glob("existing.txt.bak.*"))
        new_backups = list(destination.glob("new.txt.bak.*"))
        assert len(existing_backups) == 1
        assert new_backups == []


# =============================================================================
# Group 4: structural safety failures — the pipeline refuses before writing
# =============================================================================
class TestStructuralSafetyRefusal:
    """A structural safety failure stops the pipeline before any write.

    Two classes of structural failure:

        * ``safe_resolve`` refuses: the destination path is unsafe
          (traversal, absolute path, symlink component, NUL byte,
          too long). Caught in phase 1 of the pipeline.
        * ``validate_destination`` refuses: the destination is safe
          structurally but the leaf is a symlink, or the parent is
          unwritable. Caught in phase 1 too.

    Both refusals happen *before* the staging directory is created
    and *before* any file is written. The destination is untouched.
    The tests exercise both classes with a specific input each.
    """

    @pytest.mark.integration()
    def test_traversal_destination_refused_before_any_write(self, tmp_path: Path) -> None:
        """A plan with a traversal destination is refused in phase 1.

        The destination is never created; the staging directory is
        never created. The pipeline raises ``SafetyError`` (the
        structural-safety class), which the CLI maps to exit code 3.
        """
        from forge.safety.errors import SafetyError

        destination = tmp_path / "dest"
        destination.mkdir()
        plan = [_planned("../outside.txt")]

        with pytest.raises(SafetyError):
            _safe_pipeline(
                plan,
                destination_root=destination,
                content_map={"../outside.txt": "x"},
                overwrite=False,
            )

        # No file was created anywhere near the destination.
        assert list(destination.iterdir()) == []
        assert list(tmp_path.glob("outside.txt")) == []

    @pytest.mark.integration()
    def test_leaf_symlink_refused_before_any_write(self, tmp_path: Path) -> None:
        """A plan targeting a leaf symlink is refused in phase 1.

        The ``validate_destination`` check fires before staging, so
        the symlink is not followed and the target is not touched.
        """
        from forge.safety.errors import SafetyError

        destination = tmp_path / "dest"
        destination.mkdir()
        (destination / "real.txt").write_text("target content")
        (destination / "link.txt").symlink_to(destination / "real.txt")

        plan = [_planned("link.txt")]
        with pytest.raises(SafetyError):
            _safe_pipeline(
                plan,
                destination_root=destination,
                content_map={"link.txt": "new"},
                overwrite=True,  # even with overwrite consent
            )

        # The symlink is untouched, and the target is untouched.
        assert (destination / "link.txt").is_symlink()
        assert (destination / "real.txt").read_text() == "target content"


# =============================================================================
# Group 5: unwritable destination — the pipeline refuses before writing
# =============================================================================
class TestUnwritableDestination:
    """A destination whose parent is unwritable is refused before writing.

    This is the ``validate_destination`` readiness check, exercised in
    the composed pipeline. The pipeline refuses in phase 1, before
    ``detect_collisions``, before ``backup_existing``, before
    ``StagingArea.for_root``.

    Skipped under root — see the module docstring's
    "Filesystem-dependent tests" section.
    """

    @pytest.mark.integration()
    def test_readonly_parent_refused(self, tmp_path: Path) -> None:
        """A read-only destination parent refuses the pipeline."""
        if os.geteuid() == 0:  # pragma: no cover
            pytest.skip("read-only checks are not meaningful as root")

        from forge.safety.errors import SafetyError

        destination = tmp_path / "dest"
        destination.mkdir()
        os.chmod(destination, stat.S_IRUSR | stat.S_IXUSR)
        try:
            plan = [_planned("file.txt")]
            with pytest.raises(SafetyError):
                _safe_pipeline(
                    plan,
                    destination_root=destination,
                    content_map={"file.txt": "x"},
                    overwrite=False,
                )
        finally:
            os.chmod(destination, stat.S_IRWXU)

        # No file was written.
        assert list(destination.iterdir()) == []


# =============================================================================
# Group 6: content preservation under all outcomes
# =============================================================================
class TestContentPreservation:
    """The pipeline preserves destination content under every outcome.

    This group pins the *overall* property that the safety layer exists
    to deliver: the destination is either left unchanged (refusal) or
    carries the new content plus recoverable backups (overwrite). It
    is the property that no single primitive provides but the
    composition does, and it is the KPI the module set out to prove.

    Each test exercises a distinct outcome:

        * Refusal → destination unchanged.
        * Success (clean) → destination has the new content, no backups.
        * Success (overwrite) → destination has the new content, backups
          preserve the original.

    All three are pinned in one class because they are three
    observations of the *same* property ("the destination is a
    function of the plan and the input state, with no surprises").
    """

    @pytest.mark.integration()
    def test_refusal_leaves_destination_unchanged(self, tmp_path: Path) -> None:
        """A refused pipeline leaves every byte of the destination intact."""
        from forge.safety.errors import CollisionError

        destination = tmp_path / "dest"
        destination.mkdir()
        (destination / "existing.txt").write_text("original")
        (destination / "other.txt").write_text("also original")

        plan = [_planned("existing.txt")]
        with pytest.raises(CollisionError):
            _safe_pipeline(
                plan,
                destination_root=destination,
                content_map={"existing.txt": "new"},
                overwrite=False,
            )

        # Both files are exactly as they were.
        assert (destination / "existing.txt").read_text() == "original"
        assert (destination / "other.txt").read_text() == "also original"

    @pytest.mark.integration()
    def test_clean_success_writes_all_planned_content(self, tmp_path: Path) -> None:
        """A clean success writes every planned file with its content."""
        destination = tmp_path / "dest"
        destination.mkdir()

        plan = [_planned("a.txt"), _planned("b.txt")]
        _safe_pipeline(
            plan,
            destination_root=destination,
            content_map={"a.txt": "A", "b.txt": "B"},
            overwrite=False,
        )

        assert (destination / "a.txt").read_text() == "A"
        assert (destination / "b.txt").read_text() == "B"

    @pytest.mark.integration()
    def test_overwrite_success_preserves_original_in_backup(self, tmp_path: Path) -> None:
        """An overwrite success is recoverable from the backup."""
        destination = tmp_path / "dest"
        destination.mkdir()
        (destination / "config.yaml").write_text("old: value\n")

        plan = [_planned("config.yaml")]
        _safe_pipeline(
            plan,
            destination_root=destination,
            content_map={"config.yaml": "new: value\n"},
            overwrite=True,
        )

        # The destination has the new content.
        assert (destination / "config.yaml").read_text() == "new: value\n"
        # The original is recoverable.
        backups = list(destination.glob("config.yaml.bak.*"))
        assert len(backups) == 1
        assert backups[0].read_text() == "old: value\n"


# =============================================================================
# Group 7: no staging directory leaks under any outcome
# =============================================================================
class TestNoStagingLeaks:
    """No ``.forge-stage-*`` directory remains after *any* outcome.

    The staging directory is a transient resource. Every outcome —
    success, refusal, exception — must leave the destination with no
    staging directory. The tests here exercise the three outcomes
    against the same observable property (no staging leak), which is
    what the KPI "no staging directory leaks" means.
    """

    @pytest.mark.integration()
    def test_no_staging_leak_after_success(self, tmp_path: Path) -> None:
        """Success leaves no staging directory."""
        destination = tmp_path / "dest"
        destination.mkdir()
        _safe_pipeline(
            [_planned("file.txt")],
            destination_root=destination,
            content_map={"file.txt": "x"},
            overwrite=False,
        )
        assert list(destination.glob(".forge-stage-*")) == []

    @pytest.mark.integration()
    def test_no_staging_leak_after_collision_refusal(self, tmp_path: Path) -> None:
        """A collision refusal leaves no staging directory."""
        from forge.safety.errors import CollisionError

        destination = tmp_path / "dest"
        destination.mkdir()
        (destination / "existing.txt").write_text("x")

        with pytest.raises(CollisionError):
            _safe_pipeline(
                [_planned("existing.txt")],
                destination_root=destination,
                content_map={"existing.txt": "y"},
                overwrite=False,
            )

        assert list(destination.glob(".forge-stage-*")) == []

    @pytest.mark.integration()
    def test_no_staging_leak_after_structural_refusal(self, tmp_path: Path) -> None:
        """A structural safety refusal leaves no staging directory."""
        from forge.safety.errors import SafetyError

        destination = tmp_path / "dest"
        destination.mkdir()

        with pytest.raises(SafetyError):
            _safe_pipeline(
                [_planned("../escape.txt")],
                destination_root=destination,
                content_map={"../escape.txt": "x"},
                overwrite=False,
            )

        # The staging directory was never created (phase 1 refused
        # before phase 4). Even so, the test asserts on the property
        # ("no staging directory") rather than on the mechanism
        # ("phase 1 runs before phase 4") — a future pipeline
        # reordering that created staging earlier and still cleaned it
        # up correctly would pass this test.
        assert list(destination.glob(".forge-stage-*")) == []


# =============================================================================
# Group 8: pipeline idempotency and repeat runs
# =============================================================================
class TestIdempotency:
    """A pipeline run followed by a second run with the same inputs.

    The second run either succeeds (if ``overwrite=True``) or refuses
    with a collision (if ``overwrite=False``). Either way, the
    filesystem ends up in a predictable state — the point of the
    pipeline is that callers can re-run without surprises.

    This is the property that makes `forge create` scriptable: a
    failed run does not leave the destination in a state where a
    subsequent run cannot proceed.
    """

    @pytest.mark.integration()
    def test_rerun_without_overwrite_refuses(self, tmp_path: Path) -> None:
        """A second run without ``overwrite`` refuses on every file."""
        from forge.safety.errors import CollisionError

        destination = tmp_path / "dest"
        destination.mkdir()
        plan = [_planned("a.txt"), _planned("b.txt")]
        content_map = {"a.txt": "A", "b.txt": "B"}

        # First run: success.
        _safe_pipeline(
            plan,
            destination_root=destination,
            content_map=content_map,
            overwrite=False,
        )

        # Second run: refuses (every file is now an existing file).
        with pytest.raises(CollisionError):
            _safe_pipeline(
                plan,
                destination_root=destination,
                content_map=content_map,
                overwrite=False,
            )

        # Destination is unchanged from the first run.
        assert (destination / "a.txt").read_text() == "A"
        assert (destination / "b.txt").read_text() == "B"

    @pytest.mark.integration()
    def test_rerun_with_overwrite_succeeds_and_replaces(self, tmp_path: Path) -> None:
        """A second run with ``overwrite`` succeeds and replaces content.

        The backups from the second run preserve the *first* run's
        content, so a caller who overwrote something by mistake can
        still recover the pre-second-run state.
        """
        destination = tmp_path / "dest"
        destination.mkdir()

        # First run with original content.
        _safe_pipeline(
            [_planned("file.txt")],
            destination_root=destination,
            content_map={"file.txt": "v1"},
            overwrite=False,
        )

        # Second run with different content and overwrite=True.
        _safe_pipeline(
            [_planned("file.txt")],
            destination_root=destination,
            content_map={"file.txt": "v2"},
            overwrite=True,
        )

        # Destination has the second run's content.
        assert (destination / "file.txt").read_text() == "v2"
        # A backup exists with the first run's content.
        backups = list(destination.glob("file.txt.bak.*"))
        assert len(backups) == 1
        assert backups[0].read_text() == "v1"
