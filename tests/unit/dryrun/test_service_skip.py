"""Tests for ``DryRunService.plan().files_to_skip``.

The skip list's contract is threefold:

1. **Complete.** Every conditional file whose group's condition
   evaluated false appears in the skip list. Not "every conditional
   file whose group *appears* false" — the source of truth is the
   surviving-groups set returned by ``ConditionalEvaluator``.

2. **Attributed.** Each entry carries the *condition text*, verbatim
   as written in the blueprint, and a human-readable *reason*. An
   entry that says only ``- Dockerfile`` (with no cause) is
   indistinguishable from a bug from the user's point of view.

3. **Ordered.** Sorted by destination, like every other list in the
   plan DTO. This file asserts the sort property, not just membership.

The tests deliberately do not test the *evaluation* of conditions —
that is ``tests/unit/conditions/``. They test that the *skip list
reflects* the evaluator's verdict, which is the service's
responsibility, not the evaluator's.

Why three named tests per condition, not one parametrised
--------------------------------------------------------
Each conditional file has three observable properties that can
independently regress:

    * presence in the skip list when its condition is false,
    * absence when its condition is true,
    * the text of its ``condition`` and ``reason`` fields.

A parametrised test that asserts all three at once would fail with a
single opaque message, forcing the reviewer to open the file to learn
which property broke. Three tests means the failure name *is* the
diagnosis.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from forge.dryrun import DryRunService, SkippedFileDTO
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


def _plan(tmp_path: Path, *, variables: dict, filename: str = "blueprint.yaml"):
    """Run the service against a fixture blueprint and empty destination."""
    return DryRunService().plan(
        blueprint=_load_blueprint(filename),
        variables=variables,
        destination=tmp_path,
    )


# -----------------------------------------------------------------------------
# Presence — a false condition puts its files in the skip list
# -----------------------------------------------------------------------------
def test_conditional_file_is_skipped_when_condition_is_false(tmp_path: Path):
    """The headline property: a conditional file appears in ``files_to_skip``.

    The fixture blueprint has one conditional group
    (``include_docker == true``) with one file (``Dockerfile``). With
    ``include_docker=False``, the file must appear in the skip list.
    """
    plan = _plan(tmp_path, variables={"include_docker": False})

    skipped_sources = {s.source for s in plan.files_to_skip}
    assert "templates/Dockerfile.j2" in skipped_sources


# -----------------------------------------------------------------------------
# Absence — a true condition removes its files from the skip list
# -----------------------------------------------------------------------------
def test_conditional_file_is_not_skipped_when_condition_is_true(tmp_path: Path):
    """Complementary: a condition that holds removes its files from skips.

    This is not redundant with the presence test. An implementation
    that populated ``files_to_skip`` from *every* conditional group
    (without filtering by condition) would pass the presence test and
    fail this one. The two tests, together, prove that the skip list is
    a *filtered* view of conditional files, not just a copy of them.
    """
    plan = _plan(tmp_path, variables={"include_docker": True})

    skipped_sources = {s.source for s in plan.files_to_skip}
    assert "templates/Dockerfile.j2" not in skipped_sources


# -----------------------------------------------------------------------------
# Attribution — condition and reason travel with the file
# -----------------------------------------------------------------------------
def test_skip_entry_carries_the_condition_text(tmp_path: Path):
    """The skip entry includes the condition, verbatim.

    Users need to know *why* a file was skipped so they can edit their
    blueprint to include it. The condition text is the shortest path
    to that understanding: they read
    ``condition: include_docker == true`` and immediately know which
    variable to flip.
    """
    plan = _plan(tmp_path, variables={"include_docker": False})

    skip = next(s for s in plan.files_to_skip if s.source == "templates/Dockerfile.j2")
    # The condition is the string the blueprint author wrote,
    # space-for-space, so a user searching their YAML finds it.
    assert skip.condition == "include_docker == true"


def test_skip_entry_carries_a_reason(tmp_path: Path):
    """The skip entry includes a human-readable reason.

    A condition without a reason leaves the user to infer *what*
    "false" means for their value. The reason is what the plan DTO
    promises — see ``SkippedFileDTO.reason`` — and is what a future
    locale-aware renderer would translate.
    """
    plan = _plan(tmp_path, variables={"include_docker": False})

    skip = next(s for s in plan.files_to_skip if s.source == "templates/Dockerfile.j2")
    # The reason must be non-empty and human-readable. The service
    # currently emits "condition evaluated false" — asserting the exact
    # text here would couple the test to a wording that may evolve.
    # Asserting *the property* (non-empty, contains the cause) keeps
    # the test meaningful without freezing the prose.
    assert skip.reason  # non-empty
    assert "false" in skip.reason.lower() or "condition" in skip.reason.lower()


def test_skip_entry_carries_the_destination(tmp_path: Path):
    """The skip entry names the destination it would have been written to.

    The ``destination`` field is not strictly needed for inclusion
    decisions (the ``source`` decides that) — but it *is* needed for
    the human renderer, which shows the file the user *would* have
    gotten. A user reading "Dockerfile was skipped" needs to know the
    destination was ``Dockerfile`` (not ``Dockerfile.prod``), because
    a targeted edit is what they will make next.
    """
    plan = _plan(tmp_path, variables={"include_docker": False})

    skip = next(s for s in plan.files_to_skip if s.source == "templates/Dockerfile.j2")
    assert skip.destination == "Dockerfile"


# -----------------------------------------------------------------------------
# Multiple groups — every false group is present, every true group absent
# -----------------------------------------------------------------------------
def test_each_false_group_contributes_its_files(tmp_path: Path):
    """With two conditions both false, both groups' files appear in skips.

    Uses ``blueprint-with-conditions.yaml``, which has two conditional
    groups (``include_docker``, ``include_ci``), so that the
    per-condition independence of the skip list is actually exercised.
    A test with a single group cannot distinguish "the service filters
    per group" from "the service includes everything whenever any
    condition is false."

    The Docker group has *two* files (Dockerfile, docker-compose.yml);
    both must appear — this is what proves the skip logic handles
    multi-file groups, not just single-file ones.
    """
    plan = _plan(
        tmp_path,
        variables={"include_docker": False, "include_ci": False},
        filename="blueprint-with-conditions.yaml",
    )

    skipped = {s.source for s in plan.files_to_skip}
    # Every file from the Docker group.
    assert "templates/Dockerfile.j2" in skipped
    assert "templates/docker-compose.yml.j2" in skipped
    # Every file from the CI group.
    assert "templates/.github/workflows/ci.yml.j2" in skipped


def test_one_true_group_does_not_affect_another_false_group(tmp_path: Path):
    """Independence: a true condition does not leak into a false one.

    This is the specific bug class that emerges when someone
    "optimises" the skip logic by iterating conditional groups with an
    ``if condition_failed()`` early-exit — a subtle loop-restructure
    that would make the *remaining* groups behave as if they were
    evaluated too. With ``include_docker=True`` and ``include_ci=False``,
    the Docker files must be absent from skips and the CI file present.
    """
    plan = _plan(
        tmp_path,
        variables={"include_docker": True, "include_ci": False},
        filename="blueprint-with-conditions.yaml",
    )

    skipped = {s.source for s in plan.files_to_skip}
    # Docker files: not skipped (their group held).
    assert "templates/Dockerfile.j2" not in skipped
    assert "templates/docker-compose.yml.j2" not in skipped
    # CI file: still skipped (its group did not hold).
    assert "templates/.github/workflows/ci.yml.j2" in skipped


# -----------------------------------------------------------------------------
# Empty cases — the skip list is a list even when there is nothing to skip
# -----------------------------------------------------------------------------
def test_no_conditional_groups_produces_empty_skip_list(tmp_path: Path):
    """A blueprint with no conditional groups yields an empty (not missing) list.

    Same reasoning as ``files_to_create``'s empty test: the field is
    present and is a list, always. Automation that iterates
    ``plan.files_to_skip`` should never need a ``None`` guard.
    """
    plan = _plan(tmp_path, variables={}, filename="empty-blueprint.yaml")
    assert plan.files_to_skip == []


def test_all_conditions_true_produces_empty_skip_list(tmp_path: Path):
    """When every condition holds, nothing is skipped.

    The complete-filter contract: an empty skip list is a *positive*
    statement ("nothing was excluded"), not an absence of information.
    A regression that only populated the list on error would pass the
    presence test and fail this one.
    """
    plan = _plan(
        tmp_path,
        variables={"include_docker": True, "include_ci": True},
        filename="blueprint-with-conditions.yaml",
    )
    assert plan.files_to_skip == []


# -----------------------------------------------------------------------------
# Ordering and determinism
# -----------------------------------------------------------------------------
def test_files_to_skip_is_sorted_by_destination(tmp_path: Path):
    """The skip list is sorted by destination.

    The service sorts before constructing the DTO (see
    ``DryRunService.plan``). Renderers and JSON consumers rely on this
    ordering; a test that only checked *membership* would miss a
    regression where the sort was dropped and files arrived in
    blueprint-declaration order on some runs and reverse order on
    others.
    """
    plan = _plan(
        tmp_path,
        variables={"include_docker": False, "include_ci": False},
        filename="blueprint-with-conditions.yaml",
    )

    destinations = [s.destination for s in plan.files_to_skip]
    assert destinations == sorted(destinations)


def test_repeated_calls_produce_identical_skip_lists(tmp_path: Path):
    """Same inputs, same output — the skip list is a pure function of the plan.

    Determinism is a cross-module property (the evaluator, the service,
    the DTO all have to preserve it), and this test is where that
    property is checked for the skip list specifically. A future
    change that introduced a ``set`` in the skip-collection path (a
    common optimization) would fail here before it could confuse a
    snapshot test.
    """
    plan_a = _plan(
        tmp_path,
        variables={"include_docker": False, "include_ci": False},
        filename="blueprint-with-conditions.yaml",
    )
    plan_b = _plan(
        tmp_path,
        variables={"include_docker": False, "include_ci": False},
        filename="blueprint-with-conditions.yaml",
    )

    assert plan_a.files_to_skip == plan_b.files_to_skip


# -----------------------------------------------------------------------------
# Type stability
# -----------------------------------------------------------------------------
def test_skip_entries_are_skipped_file_dtos(tmp_path: Path):
    """Every entry is a ``SkippedFileDTO``, not a bare dict or tuple.

    Renderers and future HTTP consumers annotate their parameters as
    ``list[SkippedFileDTO]``. A test that only checked field *values*
    would pass if the service returned ``[{"source": ..., ...}]`` —
    which would break type-checking and any consumer that relied on
    attribute access. The type check is a small but real contract.
    """
    plan = _plan(tmp_path, variables={"include_docker": False})

    assert plan.files_to_skip  # the fixture guarantees at least one
    assert all(isinstance(s, SkippedFileDTO) for s in plan.files_to_skip)
