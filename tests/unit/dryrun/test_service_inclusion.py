"""Tests for ``DryRunService.plan().files_to_create``.

The service's promise is: **every file the operation would create is
listed, in sorted order, with the correct kind**. This file proves that
promise across the file sources a blueprint can express:

    * unconditional files (``blueprint.files``),
    * conditional files whose group's condition evaluates true,
    * literal copies (binaries and non-``.j2`` sources),
    * template renders (``.j2`` sources with ``kind="render"``),
    * the empty case (no files at all).

Each test drives the *real* service through a *real* blueprint document
parsed by the *real* verifier. That is deliberate: a test that stubs
out any of the three would only prove the stub works. The whole point
of the dry-run service is that it composes those three correctly, and
that property can only be tested by letting them run.

What this file does **not** test
--------------------------------
    * Ordering rules across both lists — that is the service's sorting
      contract, tested separately in ``test_service_skip.py`` (for the
      skip list) and implicitly here (the assertions compare the full
      sorted list, not just its membership).
    * Collisions — that is ``test_service_collisions.py``.
    * The no-writes invariant — that is ``test_no_writes.py``.
    * JSON rendering — that is ``test_json_contract.py``.

Keeping this file to one concern means a failure points at exactly one
property of the service.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from forge.dryrun import DryRunService
from forge.dryrun.plan import GenerationPlan, PlannedFileKind
from forge.verification import validate_blueprint_document

_FIX = Path(__file__).parents[2] / "fixtures" / "dryrun"


# -----------------------------------------------------------------------------
# Shared helpers
# -----------------------------------------------------------------------------
def _load_blueprint(filename: str = "blueprint.yaml"):
    """Load and validate a fixture blueprint through the real pipeline.

    Uses ``validate_blueprint_document`` rather than constructing the
    model by hand, so the blueprint a test exercises is exactly the
    blueprint the CLI would load at runtime. A model constructed by
    hand in a test can accept shapes the schema would reject — the
    hand-built blueprint would then pass tests that would fail in
    production. This helper eliminates that class of divergence.
    """
    document = yaml.safe_load((_FIX / filename).read_text(encoding="utf-8"))
    return validate_blueprint_document(document, source=filename)


def _run_plan(tmp_path: Path, *, variables: dict, filename: str = "blueprint.yaml"):
    """Run the service against a fixture blueprint and empty destination."""
    blueprint = _load_blueprint(filename)
    return DryRunService().plan(
        blueprint=blueprint,
        variables=variables,
        destination=tmp_path,
    )


# -----------------------------------------------------------------------------
# Unconditional files
# -----------------------------------------------------------------------------
def test_unconditional_files_are_all_listed(tmp_path: Path):
    """Every static file in the blueprint appears in ``files_to_create``.

    The baseline case: a blueprint's ``files:`` list maps one-to-one
    into the plan's ``files_to_create``. If this test fails, either
    ``FileMapper`` is not being called or the service is silently
    dropping the mapper's output — the two most likely regressions.
    """
    plan = _run_plan(tmp_path, variables={"include_docker": False})

    # The fixture declares two static files; both must be present.
    destinations = {f.destination for f in plan.files_to_create}
    assert destinations == {"src/main.py", "pyproject.toml"}


def test_unconditional_files_preserve_their_sources(tmp_path: Path):
    """Each plan entry carries the source it was mapped from.

    The ``source`` field is what the writer uses to read the file for
    rendering or copying. Losing it would make the plan renderable but
    not usable — a subtle failure mode a destination-only test would
    miss entirely.
    """
    plan = _run_plan(tmp_path, variables={"include_docker": False})
    by_destination = {f.destination: f.source for f in plan.files_to_create}
    assert by_destination == {
        "src/main.py": "templates/main.py.j2",
        "pyproject.toml": "templates/pyproject.toml.j2",
    }


# -----------------------------------------------------------------------------
# Conditional files (both branches)
# -----------------------------------------------------------------------------
def test_conditional_files_appear_when_condition_is_true(tmp_path: Path):
    """A conditional group whose condition holds contributes its files.

    This is the positive branch of the conditional contract: the file
    must appear in ``files_to_create``, not just be absent from
    ``files_to_skip``. Testing only the skip list would let a bug ship
    where a condition evaluates true, the group's files are removed
    from skips, but they also fail to appear in creates — a plan that
    claims to include Docker and then doesn't. Asserting on the
    *creates* list catches that.
    """
    plan = _run_plan(tmp_path, variables={"include_docker": True})

    destinations = {f.destination for f in plan.files_to_create}
    assert "Dockerfile" in destinations
    # The unconditional files must still be there — this is what proves
    # the conditional branch *adds* rather than *replaces*.
    assert "src/main.py" in destinations
    assert "pyproject.toml" in destinations


def test_conditional_files_are_absent_when_condition_is_false(tmp_path: Path):
    """A conditional group whose condition is false contributes nothing.

    Complementary to the test above. Combined, the two tests pin down
    the *complete* semantics: conditional inclusion is a binary that
    depends only on the resolved variables. A test that only checked
    the true branch (or only the false branch) would let an
    implementation that always includes or always excludes pass.
    """
    plan = _run_plan(tmp_path, variables={"include_docker": False})

    destinations = {f.destination for f in plan.files_to_create}
    assert "Dockerfile" not in destinations
    # The unconditional files must remain present in this branch too.
    assert destinations == {"src/main.py", "pyproject.toml"}


# -----------------------------------------------------------------------------
# Kind classification — render vs. copy
# -----------------------------------------------------------------------------
def test_render_kind_is_assigned_to_template_sources(tmp_path: Path):
    """``.j2`` sources are planned as ``render``.

    The renderer consumes this field to decide whether to interpolate
    variables. Getting it wrong the wrong way (marking a template
    ``copy``) would ship raw ``{{ ... }}`` into the output; getting it
    wrong the other way (marking a binary ``render``) would crash the
    renderer on invalid UTF-8. Both are correctness bugs a
    destination-only test would miss.
    """
    plan = _run_plan(tmp_path, variables={"include_docker": True})
    by_destination = {f.destination: f.kind for f in plan.files_to_create}

    assert by_destination["src/main.py"] is PlannedFileKind.RENDER
    assert by_destination["pyproject.toml"] is PlannedFileKind.RENDER
    assert by_destination["Dockerfile"] is PlannedFileKind.RENDER


def test_copy_kind_is_assigned_to_non_template_sources(tmp_path: Path):
    """Non-``.j2`` sources are planned as ``copy``.

    Uses a purpose-built blueprint fixture (``copy-blueprint.yaml``
    below) because the shared ``blueprint.yaml`` only contains
    templates. The classification rule (``FileMapper._kind_for``) is
    "``.j2``/``.jinja``/``.jinja2`` sources or known binary extensions
    → copy; otherwise render" — this test pins the non-template half
    of that rule.
    """
    plan = _run_plan(
        tmp_path,
        variables={},
        filename="copy-blueprint.yaml",
    )
    by_destination = {f.destination: f.kind for f in plan.files_to_create}

    # A ``.png`` is a binary: it must be copied, not rendered.
    assert by_destination["assets/logo.png"] is PlannedFileKind.COPY
    # A plain ``.py`` source with no template extension is treated as a
    # literal file by the same rule. This is the "don't be clever" case
    # — if the blueprint author wanted template expansion, they would
    # have used ``.j2``.
    assert by_destination["scripts/setup.py"] is PlannedFileKind.COPY


# -----------------------------------------------------------------------------
# Empty blueprint
# -----------------------------------------------------------------------------
def test_empty_blueprint_produces_empty_create_list(tmp_path: Path):
    """A blueprint with no files yields an empty (not missing) list.

    Automation that iterates ``plan.files_to_create`` should never have
    to guard against ``None``. The empty list is the same *shape* as a
    populated one, only shorter — the "empty plan envelope" contract
    documented in ``DRY_RUN_PLAN.md``.
    """
    plan = _run_plan(tmp_path, variables={}, filename="empty-blueprint.yaml")

    assert isinstance(plan, GenerationPlan)
    assert plan.files_to_create == []


# -----------------------------------------------------------------------------
# Determinism
# -----------------------------------------------------------------------------
def test_files_to_create_is_sorted_by_destination(tmp_path: Path):
    """The create list is sorted, not merely complete.

    The service sorts before constructing the DTO (see
    ``DryRunService.plan``), and every consumer — renderers, golden
    snapshots, JSON automation — relies on that ordering. A test that
    only compared sets would miss a regression where the sort was
    dropped and files happened to arrive in an unstable order on some
    runs.
    """
    plan = _run_plan(tmp_path, variables={"include_docker": True})
    destinations = [f.destination for f in plan.files_to_create]
    assert destinations == sorted(destinations)


def test_repeated_calls_produce_identical_create_lists(tmp_path: Path):
    """Same inputs, same output — the plan is a pure function.

    Determinism is a property the service must preserve even as it
    composes more components over time. Any future change that
    introduces non-determinism (a timestamp in a warning, a set-backed
    list, an iterating-over-a-dict that happens to vary) would trip
    this test before it can confuse a snapshot or a downstream diff.
    """
    plan_a = _run_plan(tmp_path, variables={"include_docker": True})
    plan_b = _run_plan(tmp_path, variables={"include_docker": True})

    assert plan_a.files_to_create == plan_b.files_to_create


# -----------------------------------------------------------------------------
# Error paths that preclude a create list
# -----------------------------------------------------------------------------
def test_condition_error_precludes_plan_construction(tmp_path: Path):
    """A broken condition yields a ``DryRunError``, not a partial plan.

    The service returns early on a condition error because the plan is
    meaningless without knowing which conditional groups survive. A
    regression that swallowed the condition error and returned a plan
    with only unconditional files would be strictly worse than a
    failure — the plan would *look* complete.
    """
    # ``missing-var-blueprint.yaml`` has a condition referencing a
    # variable the blueprint does not declare. Evaluating that
    # condition against an empty variable set produces a
    # ``condition.undefined_variable`` finding.
    result = _run_plan(
        tmp_path,
        variables={},
        filename="missing-var-blueprint.yaml",
    )

    # The service returns the error value, never a plan.
    from forge.dryrun import DryRunError

    assert isinstance(result, DryRunError)
    codes = {f.code for f in result.findings}
    assert "condition.undefined_variable" in codes


@pytest.mark.parametrize("filename", ["blueprint.yaml", "blueprint-with-conditions.yaml"])
def test_plan_type_is_always_generationplan_on_success(tmp_path: Path, filename: str):
    """On success, the return value is always a ``GenerationPlan``.

    A small but load-bearing property: callers use ``isinstance(result,
    DryRunError)`` as the sole failure check. If the service ever
    returned a duck-typed object (a ``dict`` with the same fields, say)
    that check would pass when it should have failed — the caller would
    treat an error as a plan. Pinning the type here makes that
    impossible.
    """
    result = _run_plan(
        tmp_path,
        variables={"include_docker": True, "include_ci": True},
        filename=filename,
    )
    assert isinstance(result, GenerationPlan)
