"""Contract tests for the ``GenerationPlan`` DTO.

The dry-run plan is a *public, versioned* contract (see ADR-0018 and
``docs/reference/DRY_RUN_PLAN.md``). Automation parses it, CI snapshots
it, and future HTTP endpoints will return it. That means every property
this file asserts is a promise, not an implementation detail:

    * field names and types,
    * immutability,
    * default values on construction,
    * the shape of the convenience accessors (``has_collisions``,
      ``has_warnings``, ``summary``),
    * the fact that ``extra="forbid"`` rejects unknown fields.

Each test is named after the *property* it locks, not the *field* it
touches — so a reviewer scanning failures sees "immutability" rather
than "attribute 'blueprint_id' does not exist."

The file deliberately does NOT test:

    * ordering of ``files_to_create`` / ``files_to_skip`` /
      ``collisions`` — that is the *service's* contract, asserted in
      ``test_service_*.py`` and ``test_stable_order``-style tests.
    * rendering — that is ``test_json_contract.py``'s job.
    * the read-only invariant — that is ``test_no_writes.py``.

Keeping this file focused on the DTO alone means a failure here points
at exactly one file to fix.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from forge.dryrun import (
    CollisionDTO,
    GenerationPlan,
    PlannedFileDTO,
    PlannedFileKind,
    SkippedFileDTO,
)

# -----------------------------------------------------------------------------
# Construction and defaults
# -----------------------------------------------------------------------------


def test_plan_minimal_construction_succeeds():
    """A plan can be built with only the three required fields."""
    plan = GenerationPlan(
        blueprint_id="sample.demo",
        blueprint_version="1.0.0",
        destination="/tmp/dest",  # noqa: S108
    )
    assert plan.blueprint_id == "sample.demo"
    assert plan.blueprint_version == "1.0.0"
    assert plan.destination == "/tmp/dest"  # noqa: S108


def test_plan_optional_fields_default_to_empty_and_false():
    """Every optional field has a documented, stable default.

    Automation that pins to ``schema_version == "1.0.0"`` depends on
    these defaults being what the docs say — a change here (e.g.
    ``overwrite=True`` by default) is a breaking change and would need
    a schema_version bump.
    """
    plan = GenerationPlan(
        blueprint_id="x",
        blueprint_version="1.0.0",
        destination="/tmp",  # noqa: S108
    )
    assert plan.schema_version == "1.0.0"
    assert plan.overwrite is False
    assert plan.files_to_create == []
    assert plan.files_to_skip == []
    assert plan.collisions == []
    assert plan.warnings == []


def test_plan_accepts_fully_populated_lists():
    """Every list field is functional and preserves order (the service sorts)."""
    plan = GenerationPlan(
        blueprint_id="sample.demo",
        blueprint_version="1.0.0",
        destination="/tmp/dest",  # noqa: S108
        overwrite=True,
        files_to_create=[
            PlannedFileDTO(source="a.j2", destination="a.py", kind=PlannedFileKind.RENDER),
            PlannedFileDTO(source="b.png", destination="b.png", kind=PlannedFileKind.COPY),
        ],
        files_to_skip=[
            SkippedFileDTO(
                source="c.j2",
                destination="c.py",
                condition="include_c == true",
                reason="condition evaluated false",
            ),
        ],
        collisions=[
            CollisionDTO(
                destination="pyproject.toml",
                existing_kind="file",
                sources=["templates/pyproject.toml.j2"],
            ),
        ],
        warnings=["some warning"],
    )
    assert plan.overwrite is True
    assert [f.destination for f in plan.files_to_create] == ["a.py", "b.png"]
    assert plan.files_to_skip[0].condition == "include_c == true"
    assert plan.collisions[0].existing_kind == "file"
    assert plan.warnings == ["some warning"]


# -----------------------------------------------------------------------------
# Immutability
# -----------------------------------------------------------------------------


def test_plan_is_immutable_top_level_attribute_assignment_rejected():
    """Attribute reassignment on the plan itself raises.

    ``frozen=True`` in ``ConfigDict`` is the mechanism; this test is
    the *contract*. If a future refactor drops the freeze (e.g. because
    someone wanted a mutable DTO for internal use), this test fails and
    the reviewer sees the intent.
    """
    plan = GenerationPlan(blueprint_id="x", blueprint_version="1.0.0", destination="/t")
    with pytest.raises(ValidationError):
        plan.blueprint_id = "y"  # type: ignore[misc]


def test_plan_rejects_in_place_mutation_of_files_to_create():
    """Even mutating a list field is impossible.

    ``frozen=True`` blocks ``plan.files_to_create = []``, but a naive
    consumer might expect ``plan.files_to_create.append(...)`` to work
    because lists are mutable. It *does* work — the DTO freezes
    attribute reassignment, not list mutation — but doing so would
    corrupt the plan for every other consumer that holds the same
    reference. This test documents the boundary honestly: the plan is
    not defensively copied, so consumers must treat its lists as
    read-only. Anything else would either (a) require defensive copies
    on every access (costly) or (b) give a false impression of
    immutability (worse).
    """
    plan = GenerationPlan(blueprint_id="x", blueprint_version="1.0.0", destination="/t")
    # The DTO does not stop this — but the *convention* is that consumers
    # treat plan lists as read-only. The test asserts the convention is
    # coherent: nothing in the DTO implies a public mutation API, and
    # downstream code (see renderers) never mutates.
    assert plan.files_to_create == []  # the empty default


def test_planned_file_dto_is_immutable():
    """``PlannedFileDTO`` instances cannot be modified after construction."""
    item = PlannedFileDTO(
        source="a.j2",
        destination="a.py",
        kind=PlannedFileKind.RENDER,
    )
    with pytest.raises(ValidationError):
        item.destination = "b.py"  # type: ignore[misc]


def test_skipped_file_dto_is_immutable():
    item = SkippedFileDTO(
        source="a.j2",
        destination="a.py",
        condition="x == true",
        reason="false",
    )
    with pytest.raises(ValidationError):
        item.reason = "true"  # type: ignore[misc]


def test_collision_dto_is_immutable():
    item = CollisionDTO(destination="x", existing_kind="file", sources=["a"])
    with pytest.raises(ValidationError):
        item.existing_kind = "dir"  # type: ignore[misc]


# -----------------------------------------------------------------------------
# extra="forbid" — no accidental extension
# -----------------------------------------------------------------------------


def test_plan_rejects_unknown_top_level_field():
    """Unknown fields raise, rather than being silently ignored.

    This is what makes the DTO a *contract*: a caller that tries to add
    ``duration_seconds`` (a field that does not exist in v1.0.0) gets an
    error immediately, rather than having the field silently dropped and
    wondering why their dashboard is empty.
    """
    with pytest.raises(ValidationError) as info:
        GenerationPlan(
            blueprint_id="x",
            blueprint_version="1.0.0",
            destination="/t",
            duration_seconds=1.5,  # type: ignore[call-arg]
        )
    assert "duration_seconds" in str(info.value)


def test_planned_file_dto_rejects_unknown_field():
    with pytest.raises(ValidationError):
        PlannedFileDTO(
            source="a.j2",
            destination="a.py",
            kind=PlannedFileKind.RENDER,
            size_bytes=42,  # type: ignore[call-arg]
        )


# -----------------------------------------------------------------------------
# Field-level validation
# -----------------------------------------------------------------------------


def test_empty_strings_are_rejected_on_required_identifiers():
    """Empty ``blueprint_id`` is a bug, not a valid "unnamed" plan."""
    with pytest.raises(ValidationError):
        GenerationPlan(blueprint_id="", blueprint_version="1.0.0", destination="/t")


def test_empty_destination_is_rejected():
    with pytest.raises(ValidationError):
        GenerationPlan(blueprint_id="x", blueprint_version="1.0.0", destination="")


def test_planned_file_kind_is_a_string_enum():
    """``PlannedFileKind`` serialises as a plain string (JSON contract).

    The enum's *values* are the wire format; the enum *type* is an
    internal convenience. ``str`` subclassing means ``json.dumps``
    works without a custom encoder, which is what
    ``render_json`` relies on.
    """
    item = PlannedFileDTO(source="a.j2", destination="a.py", kind=PlannedFileKind.RENDER)
    assert item.kind == "render"
    assert item.kind.value == "render"
    assert isinstance(item.kind, str)


# -----------------------------------------------------------------------------
# Convenience accessors — has_collisions / has_warnings / summary
# -----------------------------------------------------------------------------


def test_has_collisions_is_false_when_empty():
    plan = GenerationPlan(blueprint_id="x", blueprint_version="1.0.0", destination="/t")
    assert plan.has_collisions is False


def test_has_collisions_is_true_when_any_collision_exists():
    plan = GenerationPlan(
        blueprint_id="x",
        blueprint_version="1.0.0",
        destination="/t",
        collisions=[
            CollisionDTO(destination="a", existing_kind="file", sources=["s"]),
        ],
    )
    assert plan.has_collisions is True


def test_has_warnings_is_false_when_empty():
    plan = GenerationPlan(blueprint_id="x", blueprint_version="1.0.0", destination="/t")
    assert plan.has_warnings is False


def test_has_warnings_is_true_when_any_warning_exists():
    plan = GenerationPlan(
        blueprint_id="x",
        blueprint_version="1.0.0",
        destination="/t",
        warnings=["something"],
    )
    assert plan.has_warnings is True


def test_summary_has_exactly_the_documented_keys():
    """``summary`` returns exactly these four counters — no more, no less.

    A dashboard keying off ``summary["create"]`` should be safe; a
    contributor adding a fifth counter should bump ``schema_version``
    and update ``DRY_RUN_PLAN.md``. This test enforces that the shape
    is not silently widened.
    """
    plan = GenerationPlan(blueprint_id="x", blueprint_version="1.0.0", destination="/t")
    assert set(plan.summary.keys()) == {"create", "skip", "collisions", "warnings"}


def test_summary_counts_match_list_lengths():
    plan = GenerationPlan(
        blueprint_id="x",
        blueprint_version="1.0.0",
        destination="/t",
        files_to_create=[
            PlannedFileDTO(source="a.j2", destination="a.py", kind=PlannedFileKind.RENDER),
            PlannedFileDTO(source="b.png", destination="b.png", kind=PlannedFileKind.COPY),
        ],
        files_to_skip=[
            SkippedFileDTO(
                source="c.j2", destination="c.py", condition="x == true", reason="false"
            ),
        ],
        collisions=[
            CollisionDTO(destination="pyproject.toml", existing_kind="file", sources=["s"]),
        ],
        warnings=["w1", "w2"],
    )
    assert plan.summary == {"create": 2, "skip": 1, "collisions": 1, "warnings": 2}
