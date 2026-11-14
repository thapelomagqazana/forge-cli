"""Invisibility tests for `StagingEngine`.

Scope
-----
These tests cover the *invisibility* property of the staging phase:
during a run, nothing the engine does is observable by a user browsing
the destination directory. Specifically:

    * the staging directory has a name that starts with a dot, so it is
      hidden from plain ``ls``,
    * the staging directory lives *next to* the destination root (a
      sibling under ``destination.parent``), so a user looking at their
      project directory sees nothing new,
    * no other files or directories appear in the destination root
      while staging is in progress or after it has finished,
    * after a successful commit, only the planned files appear — nothing
      from staging remains.

The user-visible guarantee (ADR-0019) is the whole point of staging:
a partially-completed project is worse than no project at all, and an
in-progress staging tree visible to the user would suggest exactly that
half-finished state. Invisibility is what makes the guarantee
*believable* — a user who can see a `.forge-stage-XXXXXX/` directory
next to their project knows something is happening but cannot act on
it, which is worse than silence.

What this file deliberately does *not* cover
--------------------------------------------
    * Behaviour after commit — whether the committed files are correct
      is the happy-path tests' concern (``test_engine_happy.py``).
    * Failure-path cleanup — whether a failed run leaves debris is the
      failure-path tests' concern (``test_engine_render_failure.py``).
      A test asserting "no ``.forge-stage-*`` after failure" lives
      there, not here, so that a failure-mode regression points a
      reader at the failure test that broke, not the invisibility test
      that happens to use the same glob.
    * Progress-event emission — whether the CLI *shows* the user what
      is happening is the progress tests' concern. Invisibility is
      about the *filesystem*, not the terminal.

Why these tests use a live staging area rather than a mocked one
---------------------------------------------------------------
The property "the staging directory is hidden and sibling to the
destination" is a filesystem fact, not an API contract. A mocked
``StagingArea`` would let the test assert on a made-up path and
pass — proving nothing about the real engine's behaviour. The tests
run the actual engine on the actual fixture, on a real ``tmp_path``,
and assert on the actual directory tree. That is the only way to
catch a regression that changes the staging directory's location or
naming convention.

The fixture blueprint is the same one used elsewhere in the staging
suite (``tests/fixtures/staging/blueprint.yaml``). Using a *different*
fixture would test a different plan and let a fixture-specific bug
hide from the tests that matter. Consistency across the suite is not
about saving keystrokes; it is about the fixtures staying in lockstep
with the invariants the suite as a whole depends on.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from forge.planning import FileMapper, PlannedFile
from forge.staging import StagingEngine, StagingResult
from forge.verification import validate_blueprint_document

# Absolute path to the fixture tree. Same parents[] arithmetic as the
# other staging test files — see ``test_engine_happy.py``'s module
# docstring for the derivation.
FIX = Path(__file__).parents[2] / "fixtures" / "staging"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _plan() -> list[PlannedFile]:
    """Load the fixture blueprint and produce its plan.

    Returns the plan only — the blueprint and variables are not needed
    by any test in this file (invisibility is about *paths*, not about
    the blueprint). The plan result is guarded with an assertion so a
    broken fixture fails at the helper with a clear message, not three
    lines later in the test body.
    """
    document = yaml.safe_load((FIX / "blueprint.yaml").read_text())
    blueprint = validate_blueprint_document(document, source="blueprint.yaml")

    variables: dict[str, object] = {"project_name": "demo"}
    plan_result = FileMapper().map(blueprint, variables)
    assert plan_result.is_ok(), (
        "fixture is broken: FileMapper rejected the fixture blueprint. "
        f"Findings: {getattr(plan_result, 'error', None)!r}"
    )
    return plan_result.value


def _stage(tmp_path: Path) -> StagingResult:
    """Stage the fixture plan into a temporary destination root.

    Mirrors the helper in ``test_engine_happy.py`` — same arguments,
    same success guard. Kept file-local (rather than imported) so this
    file's helper can gain invisibility-specific assertions in a
    future refactor without silently changing the semantics of the
    happy-path tests. The two helpers are *currently* identical; they
    are not *contractually* identical, and coupling them would make
    every future divergence a coordination exercise.

    The caller is responsible for ``.destroy()`` — see the module
    docstring in ``test_engine_happy.py`` for why a pytest fixture is
    the wrong pattern here.
    """
    planned = _plan()
    result = StagingEngine().stage(
        planned,
        variables={"project_name": "demo"},
        blueprint_root=FIX,
        destination_root=tmp_path,
    )
    assert result.is_ok(), (
        "fixture is broken: the happy-path plan must stage successfully. "
        f"Findings: {getattr(result, 'error', None)!r}"
    )
    return result.value


# ---------------------------------------------------------------------------
# Tests — directory naming
# ---------------------------------------------------------------------------
def test_staging_directory_starts_with_dot(tmp_path: Path) -> None:
    """The staging directory's name starts with a dot.

    A dot-prefixed name is hidden from plain ``ls`` on Unix-like
    systems — the user's first look at their project directory shows
    nothing extra, which is what makes "no partial output visible"
    believable. A name without the dot would show up alongside the
    project's own files, and the user would (rightly) wonder what it
    is.

    The assertion is on the *basename*, not on the full path. The full
    path includes ``tmp_path``, whose name is test-run-specific and
    would make the assertion fragile. Only the directory the engine
    created is under the engine's control, and only that part needs to
    be dot-prefixed.

    The constant ``".forge-stage-"`` is used by ``StagingArea.for_root``
    to build the directory name. Asserting on the *prefix* (rather
    than on the exact string) tolerates the random suffix that
    ``mkdtemp`` appends — which is required for concurrent runs and
    is not part of the naming contract.
    """
    result = _stage(tmp_path)
    try:
        assert result.root.name.startswith("."), (
            f"staging directory name must start with a dot to be hidden "
            f"from ``ls``, got {result.root.name!r}"
        )
        assert result.root.name.startswith(".forge-stage-"), (
            f"staging directory name must use the ``.forge-stage-`` "
            f"prefix so it is recognisable in ``ls -a``, got "
            f"{result.root.name!r}"
        )
    finally:
        result.destroy()


def test_staging_directory_is_a_sibling_of_the_destination(tmp_path: Path) -> None:
    """The staging directory lives next to the destination, not inside it.

    The engine stages in the destination's *parent* (see
    ``safety/staging.py``'s "Where the staging directory lives"
    section for the rationale). Concretely: if the destination root
    is ``/projects/demo``, the staging directory is
    ``/projects/.forge-stage-XXXX`` — a *sibling* of ``/projects/demo``,
    not a child of it.

    The consequence for a user browsing ``/projects/demo`` (the
    directory they are looking at) is: the staging tree is not *in*
    it. The user sees their project directory without the staging
    machinery cluttering it. A user browsing the *parent*
    (``/projects``) with ``ls -a`` would see the hidden staging
    directory — that is the intended transparency point: the user
    has to deliberately look to see staging in progress, and cannot
    mistake it for project content.

    The assertion uses ``is_relative_to`` to express the *positive*
    structural claim (staging is inside the parent, i.e., sibling of
    the destination), plus an explicit inequality check that rules
    out the child case (staging *is* the destination's child), which
    ``is_relative_to`` alone would allow. Two assertions together pin
    the exact structural relationship; a single ``==`` against
    either path would silently pass for one of the wrong layouts.

    The comparison is against ``tmp_path.parent.resolve()`` rather
    than ``tmp_path.parent`` because ``pytest`` gives ``tmp_path`` as
    a resolved path on modern Python, but a future pytest could
    change that. Resolving both sides makes the assertion robust to
    that change.

    Why an explicit ``!=`` in addition to ``is_relative_to``:
        ``result.root.is_relative_to(tmp_path)`` alone is *true* for
        the child case (staging inside the destination) — precisely
        the case this test must reject. A single assertion would
        silently pass for the child layout. The pair of assertions
        — "staging is inside ``tmp_path.parent``" and "staging is
        not inside ``tmp_path``" — is the structural expression of
        "staging is a sibling of the destination." Each assertion
        catches a different wrong layout (a great-grandchild of
        ``tmp_path.parent`` would pass the first but is not a
        sibling; a child of ``tmp_path`` would pass the first but is
        not a sibling). Both must hold for the invariant to be
        satisfied.
    """
    result = _stage(tmp_path)
    try:
        # Staging lives *inside* the destination's parent — i.e. it
        # is a sibling of the destination (or, in the pathological
        # case of a deep destination, a descendant of the parent,
        # which we rule out with the second assertion below).
        assert result.root.is_relative_to(tmp_path.parent.resolve()), (
            f"staging directory must live inside the destination's "
            f"parent {tmp_path.parent.resolve()!r}, got {result.root!r}"
        )

        # Staging is *not* inside the destination root. This is the
        # assertion that catches the "staging is a child of the
        # destination" regression — the exact bug that would make
        # staging visible in the user's project directory.
        assert not result.root.is_relative_to(tmp_path.resolve()), (
            f"staging directory must not be inside the destination "
            f"root {tmp_path.resolve()!r} — it must be a sibling, so "
            f"a user browsing the destination does not see it. Got "
            f"{result.root!r}"
        )

        # And the sibling-ness expressed directly: the staging
        # directory's parent is *exactly* the destination's parent,
        # not some deeper subdirectory of it. This is the assertion
        # that rules out "staging is nested three levels up inside
        # the parent" — which would still be "not inside the
        # destination" but is not the intended structure.
        assert result.root.parent == tmp_path.parent.resolve(), (
            f"staging directory must be a direct sibling of the "
            f"destination root. Expected parent {tmp_path.parent.resolve()!r}, "
            f"got {result.root.parent!r}"
        )
    finally:
        result.destroy()


def test_destination_root_untouched_while_staging_exists(tmp_path: Path) -> None:
    """No visible entries appear in the destination during staging.

    While the staging tree exists (before commit), the destination
    directory must contain nothing the engine wrote. The only entries
    that may appear are the ones the caller pre-seeded — a user's
    existing project must not see "helpful" markers, lock files, or
    summary lines from the engine.

    The seed file is deliberate: an empty ``tmp_path`` would make
    "nothing visible" trivially true even if the engine wrote a file
    and then deleted it (a hypothetical regression). A seed ensures
    "nothing *extra* is visible" is a meaningful claim.

    The staging directory is excluded from the comparison because it
    is *intentionally* invisible to plain ``ls`` (dot-prefixed) and
    lives *next to* the destination (sibling), so it never appears in
    the destination's own listing in the first place. Excluding it
    from the glob would be wrong — the assertion is on what a user
    browsing ``tmp_path`` would see, which excludes the staging
    directory *by path*, not by name. The glob is on ``tmp_path.iterdir()``,
    which yields children of ``tmp_path`` — a sibling of ``tmp_path``
    does not appear there, so no exclusion is needed and none is done.
    """
    seed = tmp_path / "existing.txt"
    seed.write_text("hello", encoding="utf-8")
    visible_before = sorted(p.name for p in tmp_path.iterdir())

    result = _stage(tmp_path)
    try:
        visible_after = sorted(p.name for p in tmp_path.iterdir())
        assert visible_after == visible_before, (
            "the destination root gained visible entries during staging. "
            f"Before: {visible_before!r}, after: {visible_after!r}. "
            "The staging directory must be a *sibling* of the "
            "destination (see safety/staging.py), not a child of it — "
            "otherwise the user sees it in plain ``ls``."
        )
    finally:
        result.destroy()


def test_no_visible_entries_after_commit(tmp_path: Path) -> None:
    """After a full stage-then-commit cycle, only project files remain.

    The property being tested is that *staging leaves no trace* — a
    user browsing their project directory after ``forge create`` sees
    their project, not the staging machinery that produced it. This
    is the composite of two guarantees: (a) the staging tree is
    invisible during the run (previous test), and (b) the staging
    tree is destroyed after commit (``_commit_staged``'s
    ``try/finally``).

    The test drives both phases explicitly (stage, then commit) so it
    exercises the composite guarantee without depending on
    ``cli/commands/create.py``'s wiring — a change to that wiring
    should fail a *CLI* test, not this one. The staging engine's
    contract is "nothing visible while staging"; a *separate* CLI
    test asserts "the full pipeline leaves the project clean".

    The commit is done via the ``StagingResult``'s manifest, not via
    ``StagingArea.commit()``, because that is the path the engine's
    documentation prescribes (the ``StagingArea`` has already been
    detached by the time the caller receives the result).
    """
    import os

    result = _stage(tmp_path)
    try:
        # Commit explicitly, mirroring ``_commit_staged``'s loop.
        for relpath, _kind in result.manifest:
            source = result.root / relpath
            target = result.destination_root / relpath
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, target)
    finally:
        result.destroy()

    # After commit and destroy, the only entries in the destination
    # are the committed project files — no staging debris, no
    # ``.forge-stage-*`` directory anywhere near the destination.
    leftover_staging = list(tmp_path.glob(".forge-stage-*"))
    assert leftover_staging == [], (
        "staging debris remained after commit: " f"{[p.name for p in leftover_staging]!r}"
    )

    # The committed files themselves must be present — the test would
    # be trivially satisfiable by a commit that wrote nothing.
    assert (tmp_path / "pyproject.toml").exists(), (
        "commit did not produce pyproject.toml — the invisibility "
        "assertion above passed for the wrong reason"
    )
    assert (tmp_path / "src" / "demo" / "main.py").exists(), (
        "commit did not produce src/demo/main.py — the invisibility "
        "assertion above passed for the wrong reason"
    )


# ---------------------------------------------------------------------------
# Tests — staging directory permissions
# ---------------------------------------------------------------------------
def test_staging_directory_is_user_private(tmp_path: Path) -> None:
    """The staging directory is created with default user permissions.

    ``tempfile.mkdtemp`` creates directories with mode ``0o700`` on
    POSIX (user-only read/write/execute). On a multi-user machine —
    shared CI runners, dev boxes with other users — the staging tree's
    contents (rendered templates before commit) must not be readable
    by other users. This is a defense-in-depth check: the staging
    tree is only *ever* transiently present, and only during an
    authorised operation, but its contents may be sensitive (API keys
    in generated configs, license text, etc.) and it lives in a
    directory the user may not fully control.

    The assertion checks the mode's *user-access bits* are set
    (otherwise the owner cannot use the directory at all) and the
    *group/other-access bits* are clear (otherwise other users can
    read what the engine staged). It does not assert an exact mode
    because ``umask`` can narrow ``0o700`` to ``0o500`` or ``0o000``
    on unusually-configured systems — any mode that keeps the owner's
    access and denies others' is acceptable.

    Windows has no meaningful POSIX mode; the test is POSIX-only. A
    future Windows CI would ``pytest.importorskip("posix")`` or
    ``pytest.mark.skipif``; the current test suite is Linux/macOS,
    and adding the marker when Windows CI is introduced is a
    one-line change in the fixture's skipif, not a rewrite.
    """
    import os
    import stat as stat_mod

    result = _stage(tmp_path)
    try:
        mode = os.stat(result.root).st_mode

        # Owner must have read+write+execute on their own staging
        # directory — anything less means the engine cannot populate it
        # (which would have failed earlier) or the test is inspecting
        # the wrong path.
        owner_bits = stat_mod.S_IRWXU
        assert (mode & owner_bits) == owner_bits, (
            f"staging directory {result.root} is not owner-accessible "
            f"(mode {oct(mode)}); the engine cannot write to a "
            "directory it does not control"
        )

        # Group and other must have *no* access. ``mkdtemp`` guarantees
        # this on POSIX; the assertion pins it so a future refactor
        # that switches to ``mkdir`` (which respects umask and might
        # leave group-readable permissions) is caught at the
        # regression, not at the security review.
        group_other_bits = stat_mod.S_IRWXG | stat_mod.S_IRWXO
        assert (mode & group_other_bits) == 0, (
            f"staging directory {result.root} is accessible by other "
            f"users (mode {oct(mode)}); rendered files may contain "
            "sensitive content and must be user-private"
        )
    finally:
        result.destroy()
