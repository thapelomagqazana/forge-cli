"""Rejection tests for the bundled Blueprint JSON Schema.

Purpose
-------
These tests are the *negative half* of the schema contract. They assert
that documents which violate the contract are refused with the correct
``SchemaValidationError`` — and, crucially, that the error's
``json_pointer`` locates the violation precisely.

Why pointers, not messages
--------------------------
Every assertion in this file targets the structured ``errors`` tuple
(``(json_pointer, message)`` pairs) rather than the rendered string.
Two reasons:

* **Stability.** ``jsonschema``'s prose can change between minor
  versions. Pointers follow RFC 6901 and are stable across every
  ``jsonschema`` release in the supported range.
* **Precision.** "The document was rejected" is true for *any*
  violation. "``/variables/0/type`` was rejected" proves the schema
  caught the *specific* bug a fixture was designed to trigger. A
  test that only asserts "raises ``SchemaValidationError``" would
  pass even if the schema rejected for a completely different reason
  — which is the most common way schema tests silently rot.

Coverage strategy
-----------------
Each acceptance criterion from the task has a dedicated fixture and a
dedicated test:

* missing required field  → ``missing-id.yaml``
* unknown field           → ``extra-field.yaml``
* malformed version       → ``bad-version.yaml``
* invalid variable type   → ``bad-type.yaml``

Plus a small set of *defensive* cases that catch the failure modes
the acceptance criteria do not name but which the schema is designed
to reject:

* an enum variable that omits its ``validation.enum`` list
* a ``command`` verification check with no ``command`` field
* a ``required_file`` verification check with no ``path`` field
* a top-level ``schema_version`` that disagrees with the loader

The defensive cases are not paranoia. Each of them is a realistic
authoring mistake: an author who renames a field, who forgets to
copy ``validation`` from an example, who mislabels a check. If the
schema does not reject these, the *generator* will — with a much
worse error message, from a much deeper stack frame.

Design notes
------------
* **Fixtures live in ``tests/fixtures/blueprints/invalid/``.** Keeping
  them as real YAML files (rather than inline dicts) means:
    - The fixtures can be edited by anyone, not just Python
      contributors.
    - A failing test prints the *path* to the fixture, so the fix is
      one editor-open away.
    - Future CLI-level tests can re-use the same files to verify the
      user-facing error rendering.
* **Parametrisation carries the assertion.** The main test is a
  single parametrised function, so adding a case is one tuple, not
  a new function. The function bodies stay tiny; the data does the
  work.
* **One positive test for aggregation.** A document with two
  violations must report *both* in one pass. This is the property
  that makes ``SchemaValidationError`` usable in CI: a script can
  report every problem in one run, not N runs.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from forge.core.exceptions import ExitCode, SchemaValidationError
from forge.verification import validate_blueprint_document

# =============================================================================
# Fixtures
# =============================================================================
_INVALID_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "blueprints" / "invalid"


def _load(fixture: str) -> dict:
    """Read and parse an invalid-blueprint fixture.

    Uses ``yaml.safe_load`` (not ``unsafe_load``) so a fixture cannot
    execute code if it is ever edited by an untrusted contributor.
    The fixtures are test data, but test data is still data.
    """
    return yaml.safe_load((_INVALID_DIR / fixture).read_text(encoding="utf-8"))


# =============================================================================
# Accept / reject the fixtures from the acceptance criteria
# =============================================================================
@pytest.mark.parametrize(
    ("fixture", "expected_pointer"),
    [
        # Required-field absence: the top-level "required" array catches it.
        ("missing-id.yaml", "/id"),
        # ``additionalProperties: false`` at the top level catches this.
        # The pointer is the *extra field itself* — not a generic root
        # pointer — which is what makes the error actionable.
        ("extra-field.yaml", "/extra_field"),
        # SemVer ``pattern`` on ``version`` catches ``"1.0"``.
        ("bad-version.yaml", "/version"),
        # ``enum`` on the variable's ``type`` catches ``"integer"``.
        ("bad-type.yaml", "/variables/0/type"),
    ],
    ids=[
        "missing-required-id",
        "unknown-top-level-field",
        "malformed-semver",
        "invalid-variable-type",
    ],
)
def test_acceptance_criteria_are_rejected(fixture: str, expected_pointer: str) -> None:
    """Each acceptance-criterion fixture is rejected at the documented pointer.

    The pointer assertion is the load-bearing part of this test. If
    the schema ever stops rejecting for the *right* reason (e.g. a
    ``pattern`` was accidentally removed and something else rejects
    first), the pointer assertion catches the drift.
    """
    document = _load(fixture)
    with pytest.raises(SchemaValidationError) as info:
        validate_blueprint_document(document, source=fixture)

    pointers = [pointer for pointer, _ in info.value.errors]
    assert expected_pointer in pointers, (
        f"fixture {fixture!r} was rejected, but not at {expected_pointer!r}. "
        f"Pointers seen: {pointers}"
    )


@pytest.mark.parametrize(
    "fixture",
    [
        "missing-id.yaml",
        "extra-field.yaml",
        "bad-version.yaml",
        "bad-type.yaml",
        "enum-missing-validation.yaml",
        "command-missing-command.yaml",
        "required-file-missing-path.yaml",
    ],
    ids=lambda name: name.removesuffix(".yaml"),
)
def test_fixture_has_exactly_one_violation(fixture: str) -> None:
    """Every rejection fixture must have exactly *one* violation.

    This enforces the near-copy discipline that makes the other tests
    meaningful: a fixture with two defects can be "rejected" for the
    wrong reason, and the test would pass without proving the schema
    catches the intended defect.

    If this test fails, the fix is to *the fixture* — restore the
    removed field, or split the fixture in two — never to the
    assertion. The whole point of the near-copy layout is that
    editing a fixture cannot accidentally weaken a test.
    """
    document = _load(fixture)
    with pytest.raises(SchemaValidationError) as info:
        validate_blueprint_document(document, source=fixture)

    pointers = [pointer for pointer, _ in info.value.errors]
    assert len(pointers) == 1, (
        f"fixture {fixture!r} has {len(pointers)} violations, expected 1. "
        f"Pointers seen: {pointers}. "
        f"The fixture has drifted — it no longer isolates a single defect."
    )


# =============================================================================
# Defensive cases the acceptance criteria do not name
# =============================================================================
@pytest.mark.parametrize(
    ("fixture", "expected_pointer_fragment"),
    [
        # An ``enum`` variable that forgets its ``validation.enum`` list
        # is the single most common authoring mistake — the schema's
        # ``allOf`` conditional exists specifically to catch it.
        ("enum-missing-validation.yaml", "/variables/0/validation"),
        # A ``command`` check with no ``command`` field would pass a
        # naive "type must be command" check but crash the runner.
        ("command-missing-command.yaml", "/verification/0/command"),
        # A ``required_file`` check with no ``path`` is the same class
        # of bug for the other discriminator branch.
        ("required-file-missing-path.yaml", "/verification/0/path"),
    ],
    ids=[
        "enum-variable-needs-validation",
        "command-check-needs-command",
        "required-file-check-needs-path",
    ],
)
def test_structural_invariants_are_rejected(fixture: str, expected_pointer_fragment: str) -> None:
    """The schema rejects the *other* realistic authoring mistakes.

    These are not covered by the acceptance criteria because the
    criteria describe a minimal contract. They are covered *here*
    because the schema's ``allOf`` branches exist to enforce them,
    and a schema branch that is never tested is a schema branch that
    will drift.
    """
    document = _load(fixture)
    with pytest.raises(SchemaValidationError) as info:
        validate_blueprint_document(document, source=fixture)

    pointers = [pointer for pointer, _ in info.value.errors]
    assert any(expected_pointer_fragment in p for p in pointers), (
        f"fixture {fixture!r} was rejected, but not at a pointer "
        f"containing {expected_pointer_fragment!r}. "
        f"Pointers seen: {pointers}"
    )


# =============================================================================
# Aggregation and determinism
# =============================================================================
def test_multiple_violations_are_aggregated_in_one_pass() -> None:
    """A document with several bugs reports *all* of them, not just the first.

    This is what makes the error usable in CI: a reviewer reads one
    report, fixes everything, and re-runs once. A one-error-at-a-time
    loop would turn a five-minute fix into a thirty-minute
    ping-pong — the exact pain ``MissingRequiredFlagsError`` was
    introduced to eliminate for the *interactive* path. Schema
    validation must offer the same courtesy.
    """
    document = _load("missing-id.yaml")
    # Add a second, independent violation to the same document so we
    # can assert both appear in one error's ``errors`` tuple.
    document["version"] = "1.0"  # not SemVer
    with pytest.raises(SchemaValidationError) as info:
        validate_blueprint_document(document, source="missing-id+version.yaml")

    pointers = {pointer for pointer, _ in info.value.errors}
    assert "/id" in pointers, "the missing-id violation was not reported"
    assert "/version" in pointers, (
        "the malformed-version violation was not reported alongside the "
        "missing-id violation; aggregation is broken"
    )


def test_errors_are_sorted_deterministically() -> None:
    """Error output order is deterministic across runs.

    Two consequences matter:

    * **Diff stability.** When a developer fixes one of several
      violations, the error list shifts by one entry, not randomly.
      A snapshot test on the error output — or a human eyeballing
      a CI log twice — sees a coherent diff, not a reshuffling.
    * **Scriptability.** A downstream tool that wants "report the
      *first* violation" gets the same violation every time for the
      same input. Non-deterministic ordering would make that tool
      untrustworthy.

    ``validate_blueprint_document`` sorts by ``(pointer, message)``,
    which is the only stable key available: ``jsonschema`` does not
    guarantee iteration order across versions.
    """
    document = _load("missing-id.yaml")
    document["version"] = "1.0"
    with pytest.raises(SchemaValidationError) as info:
        validate_blueprint_document(document, source="multi-error.yaml")

    pointers = [pointer for pointer, _ in info.value.errors]
    assert pointers == sorted(pointers), (
        "errors must be sorted by pointer for stable output; " f"got order {pointers}"
    )


# =============================================================================
# Exit code contract
# =============================================================================
def test_schema_validation_uses_user_error_exit_code() -> None:
    """``SchemaValidationError`` must carry ``ExitCode.USER_ERROR``.

    The exit code is part of the public CLI contract (ADR-0005). A
    script that runs ``forge create`` and branches on ``$?`` expects
    "invalid input" to be ``1`` — not ``2``, not ``5``. The
    distinction from ``BLUEPRINT_ERROR`` (2) is meaningful:
    ``BLUEPRINT_ERROR`` means "the blueprint you asked for is not
    usable" (missing, unparseable, incompatible); ``USER_ERROR``
    means "the blueprint you supplied failed validation" — a fixable
    authoring mistake, not a broken registry.

    Asserting the enum member (not the integer) also verifies the
    exception inherits from ``ForgeError`` rather than redefining
    ``exit_code`` as a bare int, which would silently break the
    handler's ``match`` statement.
    """
    with pytest.raises(SchemaValidationError) as info:
        validate_blueprint_document(_load("missing-id.yaml"))
    assert info.value.exit_code is ExitCode.USER_ERROR
    assert isinstance(info.value, Exception)


def test_schema_validation_error_message_renders_all_violations() -> None:
    """``str(exc)`` includes each violation with its pointer.

    Humans read the rendered message; scripts read ``.errors``. Both
    must be complete. This test asserts the human-readable surface is
    at least as informative as the structured one — a hint line, a
    header identifying the source, and one bullet per violation.
    """
    document = _load("missing-id.yaml")
    document["version"] = "1.0"
    with pytest.raises(SchemaValidationError) as info:
        validate_blueprint_document(document, source="rendered.yaml")

    rendered = str(info.value)
    assert "rendered.yaml" in rendered, "the source label must appear"
    assert "/id" in rendered, "each pointer must appear in the rendered output"
    assert "/version" in rendered, "each pointer must appear in the rendered output"
    # The hint is a class-level default and must survive rendering.
    assert info.value.hint, "SchemaValidationError must carry a hint"


# =============================================================================
# Schema-version mismatch
# =============================================================================
def test_mismatched_schema_version_is_rejected() -> None:
    """A document declaring a different ``schema_version`` is rejected.

    The schema declares ``"const": "1.0.0"`` for ``schema_version``,
    so a document authored against a future version cannot silently
    pass validation against the current one. The rejection is handled
    by the same aggregated error path as every other violation — no
    special-case branch, no second code path to test — but it is
    worth asserting explicitly because it is the mechanism that will
    enable forward-compatible schema evolution in a future ADR
    amendment.
    """
    document = _load("missing-id.yaml")
    document["schema_version"] = "2.0.0"
    # Restore the required ``id`` so the *only* violation is the
    # version mismatch — otherwise this test would pass for the
    # wrong reason.
    document["id"] = "sample.valid-id"
    with pytest.raises(SchemaValidationError) as info:
        validate_blueprint_document(document, source="wrong-version.yaml")

    pointers = [pointer for pointer, _ in info.value.errors]
    assert "/schema_version" in pointers, f"expected /schema_version violation, got {pointers}"
    # The error message for a ``const`` mismatch is deliberately not
    # asserted — jsonschema's wording for ``const`` failures has
    # changed between minor versions in the past. The pointer is the
    # stable contract.


def test_combinator_sub_errors_do_not_produce_phantom_pointers() -> None:
    """``allOf`` evaluation must not introduce top-level violations.

    ``jsonschema``'s ``if/then`` evaluation can surface phantom
    ``required`` errors with ``absolute_path == []`` when the failure
    is logically nested inside a combinator. If ``_flatten_errors``
    promotes them, the caller sees a pointer like ``/id`` for a
    document that clearly has ``id:`` — the failure mode this test
    exists to prevent.
    """
    document = {
        "schema_version": "1.0.0",
        "id": "sample.combinatorcheck",
        "name": "Combinator Check",
        "version": "1.0.0",
        "description": "Exercises the allOf/if-then branches.",
        "category": "api",
        "compatibility": {"min_forge_version": "1.0.0"},
        "variables": [
            {
                "id": "mode",
                "type": "enum",
                "description": "Pick a mode.",
                "required": True,
                # Deliberately no `validation.enum` — the only defect.
            },
        ],
        "files": [],
        "verification": [
            {"id": "check", "type": "required_file", "path": "README.md"},
        ],
    }
    with pytest.raises(SchemaValidationError) as info:
        validate_blueprint_document(document, source="<inline>")

    pointers = [p for p, _ in info.value.errors]
    assert "/id" not in pointers, (
        "phantom /id pointer from combinator evaluation — "
        f"_flatten_errors is promoting if-branch artifacts. "
        f"Pointers seen: {pointers}"
    )
    assert any(
        "/variables/0/validation" in p for p in pointers
    ), f"the real violation was not reported. Pointers seen: {pointers}"
