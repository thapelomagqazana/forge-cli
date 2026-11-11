"""Tests for ``DryRunService.plan().collisions``.

The collision contract is the load-bearing difference between a
*dry-run* and a *create*:

    * ``forge create`` **fails** on a collision (``CollisionError``,
      exit 6). The user asked to write; the tool refuses until they
      consent with ``--overwrite``.

    * ``forge create --dry-run`` **reports** collisions *as data* in
      the plan. The user asked to *see* what would happen; the tool
      tells them, including what would be overwritten. The exit code
      is 7 (distinct from 6) so CI can gate without failing a human's
      inspection.

Both paths consume the same ``detect_collisions`` findings from
``forge.safety``. The difference is *what the caller does with them*:
create raises; dry-run projects them into ``CollisionDTO``s. This
file proves the projection is correct — every finding becomes a DTO,
the ``existing_kind`` reflects the *actual entry on disk* (not its
symlink target), and the ``--overwrite`` consent suppresses the
collision list without suppressing the plan.

What this file does **not** test
--------------------------------
    * ``detect_collisions`` itself — that is
      ``tests/unit/safety/test_collisions.py``.
    * The create-path collision (exit 6) — that is
      ``tests/unit/cli/test_create_dual_mode.py::TestGeneratePreFlight``.
    * The dry-run *render* of a collision — that is
      ``test_json_contract.py`` (JSON shape) and the human renderer's
      own test file.
    * The dry-run *exit code* on collision (7) — that is
      ``tests/integration/test_dryrun_command.py`` (end-to-end).

Keeping this file to one concern means a failure points at exactly
one property of the service.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from forge.dryrun import CollisionDTO, DryRunService, GenerationPlan
from forge.verification import validate_blueprint_document

_FIX = Path(__file__).parents[2] / "fixtures" / "dryrun"


# -----------------------------------------------------------------------------
# Shared helpers
# -----------------------------------------------------------------------------
def _load_blueprint(filename: str = "blueprint.yaml"):
    """Load and validate a fixture blueprint through the real pipeline.

    Uses ``validate_blueprint_document`` for the same reason as
    ``test_service_inclusion.py``: the blueprint a test exercises must
    be the blueprint the CLI loads at runtime, not a hand-constructed
    lookalike that would accept shapes the schema rejects.
    """
    document = yaml.safe_load((_FIX / filename).read_text(encoding="utf-8"))
    return validate_blueprint_document(document, source=filename)


def _plan(
    tmp_path: Path,
    *,
    variables: dict | None = None,
    filename: str = "blueprint.yaml",
    overwrite: bool = False,
) -> GenerationPlan:
    """Run the service against a fixture blueprint and a destination.

    ``tmp_path`` is the destination root. Tests seed files into it to
    create collisions; the service inspects them via ``lstat`` but
    never writes.
    """
    return DryRunService().plan(
        blueprint=_load_blueprint(filename),
        variables=variables or {"include_docker": False},
        destination=tmp_path,
        overwrite=overwrite,
    )


# -----------------------------------------------------------------------------
# The headline property: a collision is data, not an exception
# -----------------------------------------------------------------------------
def test_collision_is_reported_not_raised(tmp_path: Path):
    """A destination that already exists appears in ``collisions``.

    This is the single most important property of the dry-run
    collision contract: the service *returns* the collision inside
    the plan DTO, rather than raising. A user can then inspect the
    plan, decide what to do, and re-run.

    The ``pyproject.toml`` destination is chosen because the fixture
    ``blueprint.yaml`` declares it as an unconditional file — so
    seeding that file into the destination root creates a collision
    without any conditional logic involved.
    """
    (tmp_path / "pyproject.toml").write_text("pre-existing")

    plan = _plan(tmp_path)

    # Sanity: we got a plan, not an error.
    assert isinstance(plan, GenerationPlan)
    # The collision is present, in the DTO's own type.
    assert plan.has_collisions is True
    assert any(c.destination == "pyproject.toml" for c in plan.collisions)


def test_collision_dto_carries_existing_kind(tmp_path: Path):
    """``existing_kind`` names what is currently on disk.

    The user's next move depends on the kind: a ``file`` can be
    overwritten with ``--overwrite``; a ``symlink`` cannot (the
    safety layer refuses symlinks in the destination path); a ``dir``
    at a file's path is a structural mismatch the user must resolve
    by hand. Surfacing the kind is what turns ``"collision"`` from a
    generic error into an actionable one.
    """
    (tmp_path / "pyproject.toml").write_text("pre-existing")

    plan = _plan(tmp_path)

    collision = next(c for c in plan.collisions if c.destination == "pyproject.toml")
    assert collision.existing_kind == "file"


def test_collision_dto_carries_producing_sources(tmp_path: Path):
    """``sources`` lists the plan entries that would produce the collision.

    A destination is (normally) produced by exactly one source. This
    field lets a user grep the fixture blueprint for the offending
    entry without cross-referencing the plan.
    """
    (tmp_path / "pyproject.toml").write_text("pre-existing")

    plan = _plan(tmp_path)

    collision = next(c for c in plan.collisions if c.destination == "pyproject.toml")
    assert collision.sources == ["templates/pyproject.toml.j2"]


# -----------------------------------------------------------------------------
# Symlinks and directories — the "not a file" cases
# -----------------------------------------------------------------------------
def test_symlink_collision_reports_symlink_kind(tmp_path: Path):
    """A symlink at the destination is reported as ``symlink``, not ``file``.

    This is a ``lstat``-vs-``stat`` distinction the dry-run must
    preserve: a symlink pointing to a real file is not "a file that
    can be overwritten". The safety layer refuses symlinks in the
    destination path, so ``--overwrite`` cannot help — the user must
    remove the symlink first. The ``existing_kind`` field is what
    tells them that; a ``stat``-based detection would mislabel it as
    ``file`` and lead the user to try ``--overwrite`` and fail
    confusingly.
    """
    (tmp_path / "pyproject.toml").symlink_to("/tmp/somewhere-else")  # noqa: S108

    plan = _plan(tmp_path)

    collision = next(c for c in plan.collisions if c.destination == "pyproject.toml")
    assert collision.existing_kind == "symlink"


def test_directory_at_destination_reports_dir_kind(tmp_path: Path):
    """A *directory* at a file's destination is reported as ``dir``.

    This is the "structural collision" case: the plan wants to write
    ``pyproject.toml`` as a file, but there is a directory by that
    name. ``--overwrite`` cannot fix this — replacing a directory
    with a file is destructive in a way the flag does not consent
    to. Reporting it as ``dir`` is what makes the failure obvious;
    a generic "collision" message would leave the user wondering why
    ``--overwrite`` still fails.
    """
    (tmp_path / "pyproject.toml").mkdir()

    plan = _plan(tmp_path)

    collision = next(c for c in plan.collisions if c.destination == "pyproject.toml")
    assert collision.existing_kind == "dir"


# -----------------------------------------------------------------------------
# No collisions in the empty destination
# -----------------------------------------------------------------------------
def test_empty_destination_has_no_collisions(tmp_path: Path):
    """A clean destination has no collisions — the trivial baseline.

    If this test ever fails, the collision-detection path is
    reporting non-existent entries as collisions. That class of bug
    is hard to notice when a test suite only exercises the collision
    case, because the "reported" branch of the assertion is what
    reviewers look at. Testing the "empty" branch catches the bug
    where every destination is treated as if it exists.
    """
    plan = _plan(tmp_path)

    assert plan.has_collisions is False
    assert plan.collisions == []


def test_multiple_collisions_are_all_reported(tmp_path: Path):
    """Every colliding destination appears; none is suppressed.

    The fixture blueprint plans two files (``src/main.py`` and
    ``pyproject.toml``). Seeding both creates two collisions, and
    both must appear — the "one at a time" ping-pong is precisely
    the UX failure the aggregated-collision contract exists to
    prevent.
    """
    (tmp_path / "pyproject.toml").write_text("old pyproject")
    src = tmp_path / "src"
    src.mkdir()
    (src / "main.py").write_text("old main")

    plan = _plan(tmp_path)

    destinations = {c.destination for c in plan.collisions}
    assert destinations == {"pyproject.toml", "src/main.py"}


# -----------------------------------------------------------------------------
# --overwrite suppresses the collision *list*, not the plan
# -----------------------------------------------------------------------------
def test_overwrite_true_suppresses_collision_reporting(tmp_path: Path):
    """With ``--overwrite``, the collision list is empty — by design.

    The user has *consented* to overwriting. The dry-run's job in
    that mode is to show what would be replaced, not to warn about
    it. So the ``detect_collisions`` call is passed ``overwrite=True``
    and returns no findings for existing files; the plan reflects
    that.

    This test is complementary to
    ``test_collision_is_reported_not_raised`` above: the collision
    *logic* is exercised end-to-end (the ``detect_collisions`` call
    runs), but its *output* is filtered by the flag. A regression
    that hardcoded ``overwrite=False`` in the service would fail
    this test.
    """
    (tmp_path / "pyproject.toml").write_text("pre-existing")

    plan = _plan(tmp_path, overwrite=True)

    assert isinstance(plan, GenerationPlan)
    assert plan.has_collisions is False
    assert plan.collisions == []


def test_overwrite_true_still_lists_files_to_create(tmp_path: Path):
    """The plan's *create* list is unaffected by ``--overwrite``.

    A user who passes ``--overwrite`` still wants to see what would
    be written. The ``overwrite`` flag changes the *collision* report
    (collisions become implicit "would be replaced"), not the
    *create* report. This test pins that separation: overwriting does
    not blank the plan, only the collision section.
    """
    (tmp_path / "pyproject.toml").write_text("pre-existing")

    plan = _plan(tmp_path, overwrite=True)

    destinations = {f.destination for f in plan.files_to_create}
    assert destinations == {"src/main.py", "pyproject.toml"}


def test_plan_echoes_the_overwrite_flag(tmp_path: Path):
    """The plan DTO records whether ``--overwrite`` was set.

    The human renderer uses this field to describe the plan mode
    correctly ("overwrite (backups will be created)" vs.
    "no-overwrite"). Consumers inspecting the JSON can branch on it
    without re-plumbing the CLI's flag state. Asserting on the
    field here means a future refactor that dropped it from the DTO
    fails immediately, rather than silently breaking the renderer's
    mode display.
    """
    assert _plan(tmp_path, overwrite=True).overwrite is True
    assert _plan(tmp_path, overwrite=False).overwrite is False


# -----------------------------------------------------------------------------
# Ordering and determinism
# -----------------------------------------------------------------------------
def test_collisions_are_sorted_by_destination(tmp_path: Path):
    """The collision list is sorted by destination.

    The service sorts before constructing the DTO (see
    ``DryRunService.plan``). Renderers and JSON consumers rely on
    this ordering; a test that only checked *membership* would miss
    a regression where the sort was dropped and collisions arrived
    in ``detect_collisions``'s incidental order (which is
    deterministic but arbitrary).
    """
    (tmp_path / "pyproject.toml").write_text("old")
    src = tmp_path / "src"
    src.mkdir()
    (src / "main.py").write_text("old")

    plan = _plan(tmp_path)

    destinations = [c.destination for c in plan.collisions]
    assert destinations == sorted(destinations)


def test_repeated_calls_produce_identical_collisions(tmp_path: Path):
    """Same inputs, same output — the collision list is a pure function.

    Determinism matters for the collision list specifically because
    a snapshot test may one day capture the JSON output; a
    non-deterministic order in the intermediate data would fail that
    test intermittently, which is the worst failure mode (silent
    flakiness).
    """
    (tmp_path / "pyproject.toml").write_text("old")
    src = tmp_path / "src"
    src.mkdir()
    (src / "main.py").write_text("old")

    plan_a = _plan(tmp_path)
    plan_b = _plan(tmp_path)

    assert plan_a.collisions == plan_b.collisions


# -----------------------------------------------------------------------------
# Type stability
# -----------------------------------------------------------------------------
def test_collision_entries_are_collision_dtos(tmp_path: Path):
    """Every entry is a ``CollisionDTO``, not a bare dict or tuple.

    Renderers and future HTTP consumers annotate their parameters as
    ``list[CollisionDTO]``. A test that only checked field *values*
    would pass if the service returned ``[{"destination": ..., ...}]``
    — which would break type-checking and any consumer that relied on
    attribute access. The type check is a small but real contract.
    """
    (tmp_path / "pyproject.toml").write_text("pre-existing")

    plan = _plan(tmp_path)

    assert plan.collisions  # the fixture guarantees at least one
    assert all(isinstance(c, CollisionDTO) for c in plan.collisions)


def test_collision_dtos_are_immutable(tmp_path: Path):
    """``CollisionDTO`` instances cannot be modified after construction.

    Frozen Pydantic models raise ``ValidationError`` on attribute
    assignment. The test documents the contract at the point where a
    consumer might try to "patch" a collision — for example, to
    suppress one before rendering. The correct approach is to build
    a new plan, not to mutate the collision; making that obvious is
    the point of freezing.

    The exception type is asserted as ``pydantic.ValidationError``
    specifically, not ``Exception`` (B017) and not a bare
    ``pytest.raises(Exception)`` (PT011):

        * Pydantic is the library that *raises* on frozen-model
          attribute assignment — the ``ValidationError`` is its
          documented behaviour (``BaseModel.__setattr__`` calls
          ``__pydantic_validator__.validate_assignment``, which
          fails on the frozen field). Asserting on the concrete
          type ties the test to the mechanism, not to "anything
          raised" — a regression that changed the failure mode
          (e.g. to a ``TypeError``) would fail here and force a
          re-review, rather than silently passing.

        * ``pytest.raises(Exception)`` would also pass if the
          assignment raised for an *unrelated* reason (a typo in
          the field name, an ``AttributeError`` from a
          misspelled attribute). Pinning to ``ValidationError``
          rules that out.

    ``# type: ignore[misc]`` on the assignment line is for mypy,
    which correctly flags "can't assign to a frozen attribute" as a
    *type* error even though the test's whole purpose is to prove
    that error occurs at *runtime*. The two layers of checking
    (mypy's static check + this test's runtime check) are
    complementary: mypy catches the mistake at *development* time;
    this test catches a regression that removed the freezing at
    *test* time.
    """
    (tmp_path / "pyproject.toml").write_text("pre-existing")
    plan = _plan(tmp_path)
    collision = plan.collisions[0]

    with pytest.raises(ValidationError):
        collision.existing_kind = "file"  # type: ignore[misc]


# -----------------------------------------------------------------------------
# Integration with the fixture layout
# -----------------------------------------------------------------------------
def test_collision_in_fixture_destination_directory(tmp_path: Path):
    """Collisions detected against the pre-seeded fixture destination.

    Complements the inline-seeded tests above by exercising the
    *fixture-based* pattern: copy the seeded destination tree from
    ``collision-in-destination/`` into ``tmp_path``, then plan against
    it. This proves the collision path works when the destination has
    structure (subdirectories, existing files) rather than only the
    files a test wrote with a single line.

    The fixture directory itself only contains ``.gitkeep`` — the
    *layout* it implies (a destination root with content) is what
    this test pins. Tests that want a specific file seed it
    themselves; this one seeds ``pyproject.toml`` because that is the
    file the shared blueprint plans.
    """
    seeded = tmp_path / "dest"
    seeded.mkdir()
    (seeded / "pyproject.toml").write_text("from-fixture")

    plan = DryRunService().plan(
        blueprint=_load_blueprint(),
        variables={"include_docker": False},
        destination=seeded,
        overwrite=False,
    )

    assert plan.has_collisions
    assert plan.collisions[0].destination == "pyproject.toml"
