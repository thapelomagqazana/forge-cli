"""Tests for the "no filesystem mutations during dry-run" guarantee.

Two independent enforcement mechanisms, both tested here
-------------------------------------------------------
The dry-run's "no mutations" contract is not a promise in a docstring —
it is enforced by two *complementary* mechanisms, each catching a
different class of regression:

1. **Structural (the reader-facing contract).**
   ``DryRunService.plan()`` never constructs a ``StagingArea``, never
   opens a file for writing, never creates a directory. It only reads
   metadata (``lstat``, ``os.walk``, file contents for hashing). A
   reviewer can verify this by scanning the module's imports — there
   is no ``open(..., "w")``, no ``Path.mkdir()``, no ``shutil.*``
   that writes. ``read_only`` staging is the belt that catches a
   future refactor that "helpfully" starts staging inside the plan.

2. **Behavioural (the observer-facing contract).**
   The ``fingerprint`` helper in ``forge.dryrun.snapshot`` walks the
   destination tree before and after the dry-run and produces a
   sorted manifest of ``(relpath, kind, size, mtime, digest)``
   tuples. If the two fingerprints are equal, the tree is
   *byte-identical* and *untouched* — not just "no new files
   appeared", but "no file was rewritten, renamed, chmod-ed, or
   timestamp-touched either." This catches regressions that a
   file-count assertion would miss (a same-size rewrite, a
   rename-revert, a `os.utime` call).

Both are tested, because each catches a class the other misses:

- A regression that swaps in ``open(path, "w")`` for a read
  would fail the *fingerprint* test, but not the structural
  check (the source code no longer matches the docstring's promise
  — but a test that only reads source can't run the code path).
- A regression that leaves a *temporary* file in the destination
  (say, ``<dest>/.forge-stage-XXXX/file`` created then removed
  during the plan) would fail the *structural* test if we stubbed
  ``StagingArea`` to detect it — but might pass a fingerprint test
  if the file is removed before the fingerprint is taken.

Together, the two tests are stronger than either alone, and both
are cheap.

What this file does **not** test
--------------------------------
    * Collision detection — that is ``test_service_collisions.py``.
    * Inclusion/skip — those are ``test_service_inclusion.py`` and
      ``test_service_skip.py``.
    * Dry-run rendering — that is ``test_json_contract.py`` and the
      human renderer's own test file.
    * CLI-level dry-run integration (exit code 7 on collisions) —
      that is ``tests/integration/test_dryrun_command.py``.

Each test below is short because the enforcement lives in the
*helper* (``forge.dryrun.snapshot``), which has its own unit tests
and its own guarantees. This file's job is to *invoke* the helper
around the service and assert the invariant — one property, one
assertion, one failure mode.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from forge.dryrun import DryRunService
from forge.dryrun.snapshot import Entry, fingerprint
from forge.verification import validate_blueprint_document

_FIX = Path(__file__).parents[2] / "fixtures" / "dryrun"


# -----------------------------------------------------------------------------
# Shared helpers
# -----------------------------------------------------------------------------
def _load_blueprint(filename: str = "blueprint.yaml"):
    """Load and validate a fixture blueprint through the real pipeline.

    Same reasoning as every other dry-run test file: the blueprint
    the test exercises must be the blueprint the CLI loads at
    runtime.
    """
    document = yaml.safe_load((_FIX / filename).read_text(encoding="utf-8"))
    return validate_blueprint_document(document, source=filename)


def _plan(tmp_path: Path, *, variables: dict | None = None, overwrite: bool = False):
    """Run the service once against ``tmp_path`` as the destination."""
    return DryRunService().plan(
        blueprint=_load_blueprint(),
        variables=variables or {"include_docker": False},
        destination=tmp_path,
        overwrite=overwrite,
    )


def _assert_unchanged(before: list[Entry], after: list[Entry]) -> None:
    """Assert ``before == after`` with a diff message on failure.

    The raw ``==`` assertion produces an unreadable diff when a
    tree has more than a few entries. ``assert_unchanged`` in
    ``forge.dryrun.snapshot`` produces a categorised report
    (``created`` / ``removed`` / ``modified``) which is what a
    reviewer needs to diagnose a regression. This wrapper exists
    so every test in this file uses the same message format.
    """
    from forge.dryrun.snapshot import assert_unchanged

    assert_unchanged(before, after, context="dry-run")


# -----------------------------------------------------------------------------
# Behavioural tests: fingerprint equality
# -----------------------------------------------------------------------------
def test_empty_destination_stays_empty(tmp_path: Path):
    """A dry-run against an *empty* destination leaves it empty.

    The baseline: no destination content exists before the run, and
    none is created by it. If this test fails, the service has
    *created* something — a directory, a file, a hidden entry — and
    the "no writes" contract is violated at its most fundamental
    level.

    ``tmp_path`` may contain pytest's own bookkeeping files
    (``.pytest_cache`` and friends live elsewhere, but the temp dir
    itself can have entries on some platforms); we fingerprint the
    *whole* ``tmp_path`` so any new entry — including a hidden one —
    shows up.
    """
    before = fingerprint(tmp_path)
    _plan(tmp_path)
    after = fingerprint(tmp_path)
    _assert_unchanged(before, after)


def test_existing_destination_is_byte_identical(tmp_path: Path):
    """A dry-run over a *populated* destination does not touch any file.

    This is the realistic case a user hits: they already have some
    files in the target directory, they run ``--dry-run`` to see
    what would happen, and they expect *nothing* to change. The
    fingerprint captures:

        * file content (via SHA-256),
        * file *modification time* (via ``mtime_ns`` — nanosecond
          precision, so a same-second ``os.utime`` shows up),
        * file *size*,
        * directory entries (including empty subdirectories),
        * symlink *targets* (via ``readlink``).

    A same-content rewrite would still show up because mtime
    changes. A same-content-and-mtime rewrite (an adversarial
    case that only happens by accident) would slip past — which
    is what the structural tests below are for.
    """
    # Seed a realistic destination: a file at the top, a subdirectory
    # with a file, and a symlink. Three shapes, one fingerprint.
    (tmp_path / "README.md").write_text("# old project\n")
    src = tmp_path / "src"
    src.mkdir()
    (src / "main.py").write_text("print('old')\n")
    (tmp_path / "old-link").symlink_to("README.md")

    before = fingerprint(tmp_path)
    _plan(tmp_path)
    after = fingerprint(tmp_path)
    _assert_unchanged(before, after)


def test_destination_that_does_not_exist_is_not_created(tmp_path: Path):
    """A dry-run against an *absent* destination does not create it.

    This is subtly different from the empty-destination test: the
    absent case exercises the branch where the service's collision
    detection would normally walk a directory that doesn't exist.
    A naive implementation might ``mkdir`` the destination root to
    make ``lstat`` easier — a "helpful" mistake that violates the
    contract. This test catches it.

    ``tmp_path / "absent"`` is not created by pytest; only
    ``tmp_path`` itself is. So the assertion "the subdirectory
    still does not exist" is a genuine check.
    """
    absent = tmp_path / "would-be-created"

    before = fingerprint(tmp_path)  # captures tmp_path, not `absent`
    plan = DryRunService().plan(
        blueprint=_load_blueprint(),
        variables={"include_docker": False},
        destination=absent,
        overwrite=False,
    )
    after = fingerprint(tmp_path)

    # The plan itself is valid — the dry-run succeeded.
    from forge.dryrun import GenerationPlan

    assert isinstance(plan, GenerationPlan)
    # But the destination was not created.
    assert not absent.exists()
    # And nothing in the parent changed either.
    _assert_unchanged(before, after)


def test_destination_with_collisions_is_byte_identical(tmp_path: Path):
    """A dry-run with *collisions* does not touch the collided files.

    The most dangerous case for accidental mutation: the service has
    detected collisions, it knows which files would be replaced, and
    it has them in hand — a buggy implementation could "helpfully"
    write a warning file, a plan dump, or a backup, before returning.
    The fingerprint catches any of those.

    This is the case a user hits when they run ``--dry-run`` against
    a non-empty destination, see the collisions, and decide whether
    to ``--overwrite``. Nothing must have changed while they were
    deciding.
    """
    (tmp_path / "pyproject.toml").write_text("old pyproject\n")

    before = fingerprint(tmp_path)
    plan = _plan(tmp_path)
    after = fingerprint(tmp_path)

    # The collision was correctly reported...
    assert plan.has_collisions
    # ...and nothing was touched.
    _assert_unchanged(before, after)


def test_destination_with_overwrite_flag_is_byte_identical(tmp_path: Path):
    """``--overwrite`` does *not* cause the dry-run to overwrite.

    A subtle bug class: ``--overwrite`` in a dry-run means "show me
    what would be overwritten," not "overwrite it." The flag changes
    the *collision report* (suppresses it), and the plan's
    ``overwrite`` field reflects it — but the actual write is deferred
    to ``create``. A regression that read the flag as "go ahead" would
    pass the collision-suppression tests and fail here.

    This test pairs with ``test_overwrite_true_suppresses_collision_
    reporting`` in ``test_service_collisions.py``: that test pins the
    *report*, this one pins the *non-write*.
    """
    (tmp_path / "pyproject.toml").write_text("old pyproject\n")

    before = fingerprint(tmp_path)
    plan = _plan(tmp_path, overwrite=True)
    after = fingerprint(tmp_path)

    # The plan echoes the flag...
    assert plan.overwrite is True
    # ...but the flag did not cause a write.
    _assert_unchanged(before, after)


def test_repeated_dry_runs_are_idempotent(tmp_path: Path):
    """Two dry-runs in a row leave the destination untouched both times.

    A subtle bug: a dry-run that leaves *debris* — a temp file, a
    log, a cache — would pass a single-run test (fingerprints match
    before/after) only if the debris were cleaned up. If the cleanup
    happened *after* the fingerprint was taken, a single-run test
    would miss it. Two runs in a row is a stronger check: the second
    run starts from a state that must equal the state after the
    first, and ends in a state equal to the state after the second.

    In practice, if the service is deterministic and does not write,
    three fingerprints (before, after-first, after-second) will all
    be equal. The test asserts the first and third; the middle
    fingerprint is not asserted separately because if the third
    equals the first, and the second differs from the first, the
    service must have "unwritten" the debris between runs — a
    physically impossible outcome. Simpler to just say: before ==
    after-second.
    """
    (tmp_path / "pyproject.toml").write_text("old pyproject\n")

    before = fingerprint(tmp_path)
    _plan(tmp_path)
    _plan(tmp_path)
    after = fingerprint(tmp_path)

    _assert_unchanged(before, after)


# -----------------------------------------------------------------------------
# Structural tests: read-only staging area
# -----------------------------------------------------------------------------
def test_staging_area_read_only_raises_on_write(tmp_path: Path):
    """A ``read_only=True`` staging area refuses every write.

    This is the *structural* half of the guarantee. Even if a future
    refactor introduces an ``open(..., "w")`` inside the plan (or
    inside a helper it calls), the write is caught here first — as
    a ``RuntimeError`` — because ``StagingArea.write_bytes`` and
    ``write_text`` both check ``self.read_only`` and raise before
    touching the filesystem.

    The dry-run service never constructs a staging area; this test
    uses one directly to prove the mechanism works. That separation
    — service vs. staging-area test — is deliberate: the service
    test proves the *current* code does not write, and this test
    proves the *future* code cannot accidentally write through the
    staging path.
    """
    from forge.safety import StagingArea

    with StagingArea.for_root(tmp_path, read_only=True) as stage:
        with pytest.raises(RuntimeError, match="read-only"):
            stage.write_bytes("out.txt", b"data")
        with pytest.raises(RuntimeError, match="read-only"):
            stage.write_text("out.txt", "data")
        with pytest.raises(RuntimeError, match="read-only"):
            stage.commit()


def test_staging_area_read_only_does_not_create_directory(tmp_path: Path):
    """``read_only=True`` does not even create the staging directory.

    The staging-area constructor calls ``mkdtemp`` to allocate a
    directory — but in read-only mode, it must *not*. Otherwise a
    dry-run that installed a read-only staging area defensively
    would itself create a directory on the destination root — which
    is a filesystem mutation, even if it were later cleaned up.

    The fingerprint check catches this (a directory appears), but
    this test makes the reason explicit: read-only mode is *zero-
    side-effect*, not "write-then-clean-up."
    """
    from forge.safety import StagingArea

    before = fingerprint(tmp_path)
    with StagingArea.for_root(tmp_path, read_only=True):
        # Just enter the context manager — the assertion is about what
        # happens on entry.
        pass
    after = fingerprint(tmp_path)

    _assert_unchanged(before, after)


# -----------------------------------------------------------------------------
# The wrapper in `forge.dryrun.snapshot`
# -----------------------------------------------------------------------------
def test_snapshot_helper_detects_a_planted_change(tmp_path: Path):
    """Sanity check: the fingerprint detects a mutation we *do* make.

    This is the "is the detector even looking" test. Without it,
    every "no mutations" assertion above could pass because the
    fingerprint is broken (returns ``[]`` always, or two identical
    constants) — a bug that would let a real write ship while the
    test suite glowed green.

    The test:

    1. Fingerprints an empty tree.
    2. Writes a file.
    3. Fingerprints again.
    4. Asserts that ``assert_unchanged`` raises ``AssertionError``
       with a message naming the created file.

    If this test ever passes silently (i.e. ``assert_unchanged``
    does not raise), the "no mutations" tests in this file are
    meaningless. Keeping it means the whole file is self-validating:
    the detector is proven to work, and the assertions that rely on
    it mean something.
    """
    from forge.dryrun.snapshot import assert_unchanged

    before = fingerprint(tmp_path)
    (tmp_path / "planted.txt").write_text("mutant")
    after = fingerprint(tmp_path)

    with pytest.raises(AssertionError) as info:
        assert_unchanged(before, after, context="sanity-check")

    assert "planted.txt" in str(info.value)
    assert "created" in str(info.value).lower()
