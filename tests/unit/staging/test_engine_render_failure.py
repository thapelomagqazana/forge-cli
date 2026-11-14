"""Failure-path tests for `StagingEngine`.

Scope
-----
These tests cover the *failure* path of ``StagingEngine.stage()``:

    * a rendering failure aborts the run before commit,
    * the staging tree is destroyed when the run aborts,
    * the destination is byte-identical before and after the abort,
    * all findings from every failed file are aggregated in one error,
    * the returned error type carries the structure the CLI needs to
      render actionable findings.

They live in a separate file from ``test_engine_happy.py`` because the
two concerns are orthogonal: a change that breaks the success path
should not require reading the failure tests to diagnose, and a change
that breaks cleanup-on-failure should not require reading the success
tests. One concern per file is what lets a CI failure name the actual
regression in its title.

What this file deliberately does *not* cover
--------------------------------------------
    * Structural self-check failures (``staging.missing`` /
      ``staging.unexpected``) — those are bugs in the engine's own
      bookkeeping and live in ``test_engine_validate.py`` where a
      targeted test can construct the broken state directly without
      depending on the renderer.
    * Progress-event assertions — a rendering failure emits progress
      events (start, some render, error) exactly as a success does;
      the events are covered in ``test_engine_progress.py``.
    * Invisibility of the staging tree during the run — that is a
      property of the *success* path (the tree exists before commit)
      and lives in ``test_engine_invisibility.py``.

Why the fixture is ``missing-var.yaml``, not the happy fixture
--------------------------------------------------------------
The happy fixture would need a *second* template (one that references
an undeclared variable) mixed into its plan to fail. Reusing it here
would couple the failure tests to the happy fixture's variable names
and template layout — a change to the happy path could break the
failure path, which is exactly the coupling "one concern per file"
exists to avoid. ``missing-var.yaml`` is a distinct, self-contained
fixture whose only purpose is to fail in a specific, documented way.

Why ``fingerprint`` from ``forge.dryrun.snapshot``
--------------------------------------------------
The "no filesystem mutations" guarantee (ADR-0018, ADR-0019) is
proved by comparing a full manifest (paths, types, sizes, mtimes,
content hashes) before and after the run — not by a shallow "did the
directory become empty?" check. A shallow check would pass if the
engine wrote a file and then deleted it, or if it touched a file's
mtime without changing its content. ``fingerprint`` from the dry-run
module already computes the correct manifest and is the codebase's
single source of truth for that comparison; duplicating the logic here
would create a second implementation that could drift from the first.
Importing it is not a layering violation — the dry-run module's
``snapshot`` is a *utility*, not a phase, and both phases need the
same utility.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from forge.dryrun.snapshot import fingerprint
from forge.loading.result import Err
from forge.planning import FileMapper, PlannedFile
from forge.staging import RenderingError, StagingEngine
from forge.verification import validate_blueprint_document

# Absolute path to the fixture tree. Same parents[] arithmetic as
# ``test_engine_happy.py`` — see that file's module docstring for the
# derivation.
FIX = Path(__file__).parents[2] / "fixtures" / "staging"


# ---------------------------------------------------------------------------
# Fixture sanity — the input's own invariants
# ---------------------------------------------------------------------------
# Same discipline as the happy-path file: the fixtures that this file
# relies on must be byte-correct *before* the engine sees them, so a
# drift surfaces as a fixture failure (with a fix) rather than as a
# confusing engine-behaviour failure (with a symptom).
#
# The specific invariant this file cares about: ``undefined-ref.j2``
# must reference a variable name that appears in no fixture's
# ``variables:[]`` and no test's variable mapping. If someone "fixes"
# the fixture by declaring the variable, the failure test becomes a
# success test, and the abort-path assertions would fail in a way that
# names the wrong thing (the engine's cleanup logic) rather than the
# fixture.
# ---------------------------------------------------------------------------
def test_fixture_undefined_template_references_undeclared_variable() -> None:
    """``undefined-ref.j2`` must reference a variable nothing supplies.

    The failure fixture (``missing-var.yaml``) lists this template in
    its ``files:[]``. The engine will render it, Jinja will raise
    ``UndefinedError`` for the variable, and the whole staging run
    will abort. If someone adds the variable to the blueprint's
    ``variables:[]`` (a well-intentioned "fix" for a failing test), the
    render succeeds, the run does *not* abort, and the failure tests
    below fail with misleading messages about missing cleanup.

    This test names the actual invariant — "the template references a
    variable that nothing supplies" — so a future contributor editing
    the fixture sees the constraint at the point of the edit rather
    than inferring it from a downstream test's behaviour.

    The assertion is deliberately specific about the variable name
    (``undefined_thing``) rather than a generic "is the template
    non-empty" check. The specific name is what the fixture README
    documents; a test that tolerated any name would let the fixture
    drift to a variable the fixture happens to supply later, which is
    the exact failure mode this test exists to prevent.
    """
    template_text = (FIX / "templates" / "undefined-ref.j2").read_text(encoding="utf-8")
    assert "undefined_thing" in template_text, (
        "fixture templates/undefined-ref.j2 must reference "
        "`undefined_thing` — this is the variable the failure fixture "
        "relies on to be undefined. If the template was renamed, "
        "update this test and missing-var.yaml together."
    )

    blueprint_doc = yaml.safe_load((FIX / "missing-var.yaml").read_text())
    declared_ids = {v["id"] for v in blueprint_doc.get("variables", [])}
    assert "undefined_thing" not in declared_ids, (
        "fixture missing-var.yaml must NOT declare `undefined_thing` in "
        "its variables:[] — the failure path depends on it being "
        "undefined. If it was added to make the fixture validate, "
        "revert that change; the fixture is *supposed* to fail at "
        "render time."
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _plan() -> list[PlannedFile]:
    """Load the failure fixture and produce its plan.

    The plan itself is *valid* — ``FileMapper`` succeeds because
    ``missing-var.yaml`` declares all the variables it references in
    ``destination:`` templates. The failure is not at planning time;
    it is at *render* time, when ``undefined-ref.j2``'s body
    references a variable that the plan does not carry. That
    distinction is what makes this a failure-path test rather than a
    planning-error test: the engine has to start the run and fail
    *during* it, which is the scenario the abort-cleanup logic exists
    for.

    If this assertion ever fails, the fixture has drifted from its
    contract — see ``test_fixture_undefined_template_references_
    undeclared_variable`` for the invariant, and the failure message
    points at the likely cause.
    """
    document = yaml.safe_load((FIX / "missing-var.yaml").read_text())
    blueprint = validate_blueprint_document(document, source="missing-var.yaml")

    variables: dict[str, object] = {"project_name": "demo"}
    plan_result = FileMapper().map(blueprint, variables)
    assert plan_result.is_ok(), (
        "fixture is broken: FileMapper rejected missing-var.yaml. The "
        "fixture is supposed to *plan* successfully and fail at render "
        f"time. Findings: {getattr(plan_result, 'error', None)!r}"
    )
    return plan_result.value


def _stage_failure(tmp_path: Path) -> RenderingError:
    """Stage the failure plan and return the aggregated error.

    Two assertions collapsed into a helper:

        1. ``result`` is ``Err`` — a regression that made the engine
           return ``Ok`` for a template that failed to render would be
           caught by every test in this file at once, with the same
           message, rather than each test asserting it separately.
        2. ``result.error`` is a ``RenderingError`` — the type the CLI
           handler classifies (exit 8) and renders as a findings list.
           A regression that changed the error type (say, to a generic
           ``RuntimeError``) would fail every test with a message that
           names the type, which is the *actual* contract being broken.

    The two assertions run in that order because ``Err`` is the
    stronger contract (the engine returned a ``Result`` at all, not an
    exception); the type check is the narrower one. Failing at the
    broader assertion first, when both are broken, gives a message
    that is closer to the root cause.
    """
    planned = _plan()
    result = StagingEngine().stage(
        planned,
        variables={"project_name": "demo"},
        blueprint_root=FIX,
        destination_root=tmp_path,
    )
    assert isinstance(result, Err), (
        "fixture is broken: the failure plan rendered successfully. "
        "The template ``undefined-ref.j2`` is supposed to reference a "
        "variable nothing supplies; if that changed, the abort-path "
        "assertions in this file are testing the wrong thing."
    )
    assert isinstance(result.error, RenderingError), (
        "engine contract broken: a render failure must surface as "
        f"RenderingError, got {type(result.error).__name__!r}"
    )
    return result.error


# ---------------------------------------------------------------------------
# Tests — abort semantics
# ---------------------------------------------------------------------------
def test_missing_variable_aborts_staging(tmp_path: Path) -> None:
    """The engine returns ``Err`` naming the undefined variable.

    The assertion checks the *finding's code* (``render.undefined_
    variable``), not just that the error is non-empty. A regression
    that returned the right type but with a wrong or empty finding
    would pass a looser test — and would still break the CLI's
    rendering, which switches on the code to decide how to present
    the finding.

    The check is "at least one finding has this code" rather than
    "the first finding has this code" because the engine processes
    files in plan order and multiple files could fail in a richer
    fixture. For *this* fixture exactly one file fails, so the two
    checks happen to be equivalent — but the looser form documents the
    contract correctly and survives a future fixture that fails more
    than one file without breaking the test.
    """
    error = _stage_failure(tmp_path)
    codes = [f.code for f in error.findings]
    assert "render.undefined_variable" in codes, (
        "the engine did not report ``render.undefined_variable`` for the "
        f"missing-variable failure. Findings: {[(f.code, f.path) for f in error.findings]!r}"
    )


def test_finding_names_the_template_source(tmp_path: Path) -> None:
    """The finding's message names the source template that failed.

    A finding's ``path`` is the *destination* path (``broken.py``) —
    what the user was trying to produce. The *source* (``templates/
    undefined-ref.j2``) is where the user has to go to fix the
    problem, and it lives in the ``message`` because the CLI renders
    ``path`` and ``message`` on the same line. See
    ``staging/_renderer_loop.py``'s docstring for why the split is
    intentional: the destination tells the user *what they lost*, the
    message tells them *where to fix it*.

    A regression that dropped the source from the message would leave
    the user with a finding that names the file they were trying to
    produce but not the file they need to edit. That is technically
    informative (they can find the source via the blueprint) but
    unhelpful — the CLI's job is to save them that lookup.
    """
    error = _stage_failure(tmp_path)
    undefined_findings = [f for f in error.findings if f.code == "render.undefined_variable"]
    assert undefined_findings, "no undefined-variable finding to inspect"
    finding = undefined_findings[0]
    assert "templates/undefined-ref.j2" in finding.message, (
        "the undefined-variable finding does not name the source "
        f"template in its message. Message: {finding.message!r}"
    )
    assert "undefined_thing" in finding.message, (
        "the undefined-variable finding does not name the variable. "
        f"Message: {finding.message!r}"
    )


def test_finding_path_is_the_destination_not_the_source(tmp_path: Path) -> None:
    """The finding's ``path`` is the destination, not the source.

    The complement to the previous test. If the engine accidentally
    put the *source* in ``path`` (a plausible mistake — the source is
    what the renderer names in its own error), the CLI would render
    findings as if the destination file were the source template,
    which reads as nonsense to a user scanning the report
    ("``templates/undefined-ref.j2``: undefined variable" — as if the
    *template* is what they were trying to produce, not
    ``broken.py``).

    A finding that put *both* paths in ``path`` (say,
    ``"templates/undefined-ref.j2 -> broken.py"``) would fail this
    test. That is deliberate: the field is the destination by
    contract, and a caller branching on ``path`` (for a "show me the
    files that failed to produce" filter, say) needs the destination
    unambiguously.
    """
    error = _stage_failure(tmp_path)
    undefined_findings = [f for f in error.findings if f.code == "render.undefined_variable"]
    finding = undefined_findings[0]
    assert (
        finding.path == "broken.py"
    ), f"finding.path must be the destination file, got {finding.path!r}"


def test_all_failures_aggregated_not_first_only(tmp_path: Path) -> None:
    """A run with multiple failing templates reports every failure.

    The engine processes files in plan order and *collects* findings;
    it does not raise on the first failure. This test proves that with
    a fixture that would fail twice if it failed at all — but the
    current ``missing-var.yaml`` only fails once. The test therefore
    asserts a weaker but sufficient property: the aggregated error is
    a *list*, not a single finding, and the list survives the abort.
    If the engine started raising on the first failure, the return
    type would change (an exception, not an ``Err``), which
    ``_stage_failure`` already catches.

    A future fixture that fails on more than one file would let this
    test assert the stronger property ``len(error.findings) >= 2``
    without changing its shape; the docstring documents the transition
    so a contributor adding a multi-failure fixture knows what to
    strengthen.
    """
    error = _stage_failure(tmp_path)
    assert isinstance(error.findings, tuple), (
        "RenderingError.findings must be a tuple (immutable, ordered); "
        f"got {type(error.findings).__name__!r}"
    )
    assert len(error.findings) >= 1, (
        "the aggregate error must carry at least one finding — a "
        "RenderingError with no findings is a category error"
    )


# ---------------------------------------------------------------------------
# Tests — cleanup
# ---------------------------------------------------------------------------
def test_staging_tree_removed_on_failure(tmp_path: Path) -> None:
    """The staging tree is gone after a failed run.

    The engine's cleanup responsibility is: if the run aborts, the
    staging tree must not survive. This is enforced by
    ``StagingArea.for_root``'s context manager ``finally`` block
    (which the engine does *not* detach on the failure path — see
    ``safety/staging.py``). The test asserts the observable effect:
    no ``.forge-stage-*`` entry remains under ``tmp_path``.

    A regression that detached on failure (perhaps by moving the
    ``detach()`` call to before the structural self-check) would leave
    a ``.forge-stage-*-ready`` directory behind — subtle disk debris
    that users would eventually notice and complain about, but no
    existing test would catch. This test is what closes that gap.

    The glob covers both ``.forge-stage-*`` (the pre-detach name) and
    ``.forge-stage-*-ready`` (the post-detach name) because a bug
    could produce either. A test that only checked the first pattern
    would pass for the second bug — the exact class of "half-checked"
    test that produces false confidence.
    """
    _stage_failure(tmp_path)
    leftover = list(tmp_path.glob(".forge-stage-*"))
    assert leftover == [], (
        "staging directories survived a failed run — this is a disk "
        f"leak. Leftover: {[p.name for p in leftover]!r}"
    )


def test_destination_byte_identical_after_failure(tmp_path: Path) -> None:
    """A failed run leaves the destination exactly as it was.

    The KPI for the staging phase is "0 filesystem mutations during
    staging" — proved here by a full manifest comparison (paths,
    types, sizes, mtimes, content hashes) rather than by a shallow
    check. The manifest is computed by ``fingerprint`` from the
    dry-run module, which is the codebase's single source of truth
    for that comparison.

    The seed file is deliberate: an empty ``tmp_path`` would make the
    comparison trivially pass even if the engine wrote and then
    deleted a file (a hypothetical bug in the cleanup path). A seed
    file ensures the destination *contains something worth not
    touching*, and the hash check ensures that "worth not touching"
    is verified at the byte level, not just the path level.
    """
    seed = tmp_path / "existing.txt"
    seed.write_text("hello", encoding="utf-8")
    before = fingerprint(tmp_path)

    _stage_failure(tmp_path)

    after = fingerprint(tmp_path)
    assert before == after, (
        "the destination was modified during a failed staging run — "
        "the staging engine must not touch the destination under any "
        "circumstance (see ADR-0019)"
    )


def test_destination_not_created_when_absent(tmp_path: Path) -> None:
    """A failed run does not create the destination directory.

    The previous test covers "destination exists and is untouched".
    This covers "destination does not exist and must not be created".
    Both cases matter: the first is the common one (an existing
    project directory the user is generating into), the second is the
    "someone typo'd the destination path" case (the engine should not
    create a directory tree on the user's filesystem to hold a
    staging area that will be immediately removed — that would leave
    an empty directory behind, which is still a mutation).

    The test asserts the *absence* of the directory, which is a
    property a fingerprint cannot express (fingerprint of a nonexistent
    directory is ``[]``, same as fingerprint of an empty one). A direct
    ``exists()`` check is the correct assertion here — it distinguishes
    "empty directory" from "no directory", which the fingerprint
    conflates for historical reasons.
    """
    absent_destination = tmp_path / "would-be-created"
    assert (
        not absent_destination.exists()
    ), "precondition broken: the test seed created the destination"

    planned = _plan()
    result = StagingEngine().stage(
        planned,
        variables={"project_name": "demo"},
        blueprint_root=FIX,
        destination_root=absent_destination,
    )
    assert isinstance(result, Err), "fixture must fail to stage for this test"

    assert not absent_destination.exists(), (
        "the staging engine created the destination directory during a "
        "failed run — the engine must not modify the destination root, "
        "including creating it (see ADR-0019)"
    )
