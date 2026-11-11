"""Regenerate the golden plan snapshots.

Run as a module so the paths work without cwd surprises:

    python -m tests.fixtures.dryrun._regenerate

Writes:
    - golden-plan.json          (from blueprint.yaml)
    - expected-empty-plan.json  (from empty-blueprint.yaml)

Both use a *fixed* destination string (not a tmp_path) so the output
is byte-identical across machines and checkouts. The destination is
never actually written to — it exists purely as a value in the DTO.

Why the plan builders are separate functions
--------------------------------------------
The test that asserts the golden file matches the rendered plan
(``tests/unit/dryrun/test_json_contract.py::test_golden_snapshot_matches``)
must build its plan the *same* way this module builds it. If the test
constructed its own plan by hand, the two would drift — the test
would assert "my hand-built plan renders to the same shape as the
file" rather than "the file matches what the tool produces."

Extracting ``_build_golden_plan()`` and ``_build_empty_plan()`` from
``main()`` makes that shared construction explicit: both the writer
and the test call the same function.

Why ``warnings`` are cleared on the golden plans
------------------------------------------------
``warnings`` is the one field on ``GenerationPlan`` that captures
*runtime* filesystem observations about the destination — e.g. "the
parent directory is not writable", "the destination path is not
resolvable". Those observations depend on the machine the plan is
built on:

    * ``/fixed/path/used/in/golden/tests`` does not exist on any
      machine, so ``validate_destination`` walks up to ``/`` and
      checks its writability. On a normal user's machine, ``/`` is
      not writable — a warning fires. In a root-run CI container,
      ``/`` *is* writable — no warning fires. Same plan builder,
      different output, depending on the environment.

    * A test that asserts on the plan's ``warnings`` field is
      therefore **not asserting on the plan's contract** — it is
      asserting on the environment's contract. That is a category
      error: the golden file's job is to freeze the *structural*
      shape (ids, versions, file lists, collisions), not the
      transient filesystem report.

The plan builder explicitly clears ``warnings`` after building the
plan, on the grounds that the golden fixture is a *structural*
contract and warnings are an *environmental* one. The test that
exercises warnings does so through ``test_service_*.py`` against a
``tmp_path`` — where the environment is controlled and the warnings
are deterministic.

If we ever want to include warnings in the golden fixture, the
builder needs a deterministic destination (a real, controlled
directory) — not a fixed string. That is a bigger change (the
fixture would need to normalise paths and seed files), and it is
not what this fixture's job currently is.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from forge.dryrun import DryRunService
from forge.dryrun.renderers import render_json
from forge.verification import validate_blueprint_document

_FIX = Path(__file__).parent
_FIXED_DESTINATION = "/fixed/path/used/in/golden/tests"


def _build_golden_plan():
    """Build the exact plan ``golden-plan.json`` represents.

    This is the *canonical* plan — the one the test asserts against
    and the one the fixture file contains. Any change to it should
    be paired with a regeneration and a review of the resulting
    diff, so the golden file remains the contract.

    The returned plan's ``warnings`` field is cleared (see the
    module docstring's "Why warnings are cleared" section): warnings
    are a runtime filesystem observation, not a structural property
    of the plan. Freezing them in a golden file would make the
    fixture environment-dependent.
    """
    document = yaml.safe_load((_FIX / "blueprint.yaml").read_text(encoding="utf-8"))
    blueprint = validate_blueprint_document(document, source="blueprint.yaml")
    plan = DryRunService().plan(
        blueprint=blueprint,
        variables={"include_docker": False},
        destination=Path(_FIXED_DESTINATION),
    )
    # Cleared for the same reason the empty plan below clears it —
    # see the module docstring. Uses ``model_copy`` (Pydantic v2) so
    # the frozen model is not mutated in place: the builder is a
    # pure function of its inputs *plus* the deliberate
    # environment-independence of the warnings field.
    return plan.model_copy(update={"warnings": []})


def _build_empty_plan():
    """Build the exact plan ``expected-empty-plan.json`` represents.

    A plan with no files at all — the empty-envelope case. Its
    fixture exists so automation can rely on the envelope's shape
    being constant regardless of plan content.

    Warnings are cleared for the same reason as ``_build_golden_plan``
    (see the module docstring).
    """
    document = yaml.safe_load((_FIX / "empty-blueprint.yaml").read_text(encoding="utf-8"))
    blueprint = validate_blueprint_document(document, source="empty-blueprint.yaml")
    plan = DryRunService().plan(
        blueprint=blueprint,
        variables={},
        destination=Path(_FIXED_DESTINATION),
    )
    return plan.model_copy(update={"warnings": []})


def main() -> None:
    _write(_build_golden_plan(), "golden-plan.json")
    _write(_build_empty_plan(), "expected-empty-plan.json")


def _write(plan, out_file: str) -> None:
    """Write ``plan`` as pretty JSON to ``out_file``.

    Checks the service returned a ``GenerationPlan`` (not a
    ``DryRunError``) before writing — a regeneration script that
    silently wrote an error's ``str()`` into the golden file would
    produce a fixture the tests *think* is a plan.
    """
    if not hasattr(plan, "model_dump"):
        raise SystemExit(f"regeneration failed for {out_file}: {plan}")

    text = render_json(plan)
    # Pretty-print for human review; the test compares parsed JSON,
    # so cosmetic formatting does not affect the assertion — but the
    # diff a reviewer sees is line-by-line, not one giant line.
    pretty = json.dumps(json.loads(text), indent=2, sort_keys=True) + "\n"

    (_FIX / out_file).write_text(pretty, encoding="utf-8")
    print(f"wrote {out_file}")


if __name__ == "__main__":
    main()
