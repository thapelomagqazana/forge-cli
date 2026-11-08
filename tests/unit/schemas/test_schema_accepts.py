"""Acceptance tests for the bundled Blueprint JSON Schema.

Purpose
-------
These tests are the *positive half* of the schema contract. They
assert that documents which **conform** to the schema are accepted
silently — no exception, no warning, no partial validation.

Why a positive half is necessary
--------------------------------
A schema that rejects *everything* passes every rejection test in
``test_schema_rejects.py``. A schema that rejects *nothing* passes
every acceptance test here. Only the pair together proves the schema
is discriminating: it accepts what it should accept, rejects what it
should reject, and does not blur the line.

The three fixtures in this file are the *target blueprints* named in
the project scope:

    * ``python-api.yaml``         → the FastAPI service blueprint
    * ``python-automation.yaml``  → the scripting / automation blueprint
    * ``typescript-frontend.yaml`` → the frontend blueprint

These are not arbitrary examples. They are the blueprints the CLI
ships and the ones every scaffold in the MVP must succeed at. If the
schema rejects any of them, the shipped product is broken regardless
of how many synthetic documents it accepts.

Design notes
------------
* **Fixtures are the contract.** The three YAML files under
  ``tests/fixtures/blueprints/valid/`` are treated as the
  authoritative *reference* blueprints — they are what ``docs``
  references when explaining "what a blueprint looks like."
  Adding a new target blueprint means adding a fixture here and a
  row to the parametrisation, not editing an inline dict.

* **Assertions target silence, not shape.** ``validate_blueprint_document``
  returns ``None`` on success (by design — a return value would invite
  callers to branch on "was it valid?" and construct their own
  error, which decays the single-funnel discipline). The only
  observable success signal is "no exception was raised." Tests
  therefore *call and continue*, rather than assert a return value.

* **A round-trip invariant is asserted for each fixture.** Beyond
  "the schema accepts this document," we assert that the parsed
  document is a mapping with the fields the CLI reads. This catches
  the failure mode where a fixture is accidentally emptied (a bad
  merge) and the test still passes because the schema accepts an
  empty ``variables: []`` array. An empty fixture is silent
  corruption; a test that checks the parsed document is not silent.

Non-goals
---------
* This file does **not** test the *semantic* layer (variable
  defaults matching their type, ``min_forge_version`` satisfying
  SemVer ordering against the installed version, etc.). Those rules
  are enforced by the verification layer's *input* validator, not by
  the schema. Mixing them here would blur the "shape vs. meaning"
  boundary the schema was designed to keep clean.
* This file does **not** test the schema's own shape — that is
  ``test_schema_shape.py``'s job. Schema bugs and document bugs
  fail in different files so a red CI run names the culprit.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from forge.verification import validate_blueprint_document

# =============================================================================
# Fixtures
# =============================================================================
_VALID_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "blueprints" / "valid"

# The set of target blueprints named in the project scope. The
# parametrisation uses this list rather than a literal so that adding
# a fourth target blueprint is a one-line change to the constant, and
# every test that iterates over target blueprints picks it up
# automatically. This is the "single source of truth" principle
# applied to test data.
_TARGET_BLUEPRINTS: tuple[str, ...] = (
    "python-api.yaml",
    "python-automation.yaml",
    "typescript-frontend.yaml",
)


def _load(fixture: str) -> dict:
    """Read and parse a valid-blueprint fixture.

    ``yaml.safe_load`` (not ``unsafe_load``) is used even for fixtures
    we own. The fixtures are test data, but test data is still data —
    and a future contributor who fetches a fixture from a bug report
    should not accidentally be running code.
    """
    return yaml.safe_load((_VALID_DIR / fixture).read_text(encoding="utf-8"))


# =============================================================================
# Acceptance
# =============================================================================
@pytest.mark.parametrize("fixture", _TARGET_BLUEPRINTS, ids=_TARGET_BLUEPRINTS)
def test_target_blueprint_is_accepted(fixture: str) -> None:
    """Each target blueprint validates cleanly against the bundled schema.

    ``validate_blueprint_document`` returns ``None`` on success (it
    only ever raises on failure). The test therefore asserts absence
    of exception implicitly: if the call returns, the document was
    accepted.
    """
    validate_blueprint_document(_load(fixture), source=fixture)


@pytest.mark.parametrize("fixture", _TARGET_BLUEPRINTS, ids=_TARGET_BLUEPRINTS)
def test_target_blueprint_has_the_fields_the_cli_reads(fixture: str) -> None:
    """The fixture, once parsed, must expose the fields the CLI actually reads.

    The schema's ``required`` array already enforces that these
    fields are present in any valid document. This test re-asserts
    the same invariant on the *fixtures*, for two reasons:

    * **Fixture corruption.** A bad merge that truncates a fixture
      to ``{}`` would still fail the schema — but the failure
      message would say "missing /id", which reads like a schema bug
      to someone unfamiliar with the test layout. Asserting the
      fields directly makes the message say "the fixture is
      incomplete," which is the actual problem.
    * **Reader coupling.** The schema's ``required`` list may
      legitimately change in a future version (e.g. adding a new
      field). A test that asserts the *current* set of fields the
      CLI reads will fail the moment a field is renamed, before the
      loader crashes in production. It is the earliest possible
      signal that a schema change broke a caller.
    """
    document = _load(fixture)
    for field in (
        "id",
        "name",
        "version",
        "description",
        "category",
        "variables",
        "files",
        "verification",
        "compatibility",
    ):
        assert field in document, (
            f"fixture {fixture!r} is missing required field {field!r}; "
            f"the fixture is likely corrupted or truncated"
        )


# =============================================================================
# Round-trip stability
# =============================================================================
@pytest.mark.parametrize("fixture", _TARGET_BLUEPRINTS, ids=_TARGET_BLUEPRINTS)
def test_target_blueprint_round_trips_through_yaml(fixture: str) -> None:
    """Parsing, dumping, and re-parsing a fixture yields an equivalent document.

    This is not a test of *our* code — ``yaml.safe_load`` and
    ``yaml.safe_dump`` are well-tested. It is a test of the *fixtures*:
    if a fixture contains a value that does not survive YAML
    serialisation (an unquoted ``version: 1.0`` becomes a float, a
    YAML timestamp becomes a ``datetime``), then:

    * ``validate_blueprint_document`` on the *original* may pass
      because the loader happens to produce the right Python type.
    * A user who edits the blueprint in a YAML editor may produce a
      *different* Python object from the same bytes — and now the
      schema accepts one representation and rejects the other.

    A round-trip test catches this by making the two representations
    explicit. The current fixtures pass because every ambiguous value
    is quoted (``version: "1.0.0"``, not ``version: 1.0.0``). The
    test is there to ensure that stays true.
    """
    original = _load(fixture)
    dumped = yaml.safe_dump(original, sort_keys=True)
    reparsed = yaml.safe_load(dumped)
    assert reparsed == original, (
        f"fixture {fixture!r} does not round-trip through YAML; "
        f"some value is ambiguous and may parse differently for a "
        f"user editing the file by hand"
    )


# =============================================================================
# Local blueprint consistency
# =============================================================================
@pytest.mark.parametrize("fixture", _TARGET_BLUEPRINTS, ids=_TARGET_BLUEPRINTS)
def test_target_blueprint_has_at_least_one_verification_check(fixture: str) -> None:
    """Every target blueprint must declare at least one verification check.

    The schema enforces ``minItems: 1`` on ``verification``. This
    test asserts the *fixtures* honour that constraint for a
    different reason: a blueprint with zero verification checks
    cannot fail ``forge verify`` after scaffolding, which means the
    "100% of generated projects are verifiable" guarantee would be
    silently false for that blueprint. The schema is right to
    require it; the fixtures must be the *proof* that they do.

    Asserting the property directly (rather than "the schema rejected
    it") keeps the failure message specific: "this blueprint has no
    verification checks" is actionable; "the fixture was rejected by
    the schema at /verification" is not.
    """
    document = _load(fixture)
    assert isinstance(
        document.get("verification"), list
    ), f"fixture {fixture!r} has no 'verification' list"
    assert len(document["verification"]) >= 1, (
        f"fixture {fixture!r} declares no verification checks; "
        f"a blueprint with nothing to verify is not a blueprint"
    )


@pytest.mark.parametrize("fixture", _TARGET_BLUEPRINTS, ids=_TARGET_BLUEPRINTS)
def test_target_blueprint_identifier_follows_dotted_convention(fixture: str) -> None:
    """Blueprint IDs must follow the dotted lowercase convention.

    The schema enforces this with a ``pattern``. The test re-asserts
    it on the fixtures because the convention is load-bearing: the
    interactive wizard's blueprint selector groups by prefix
    (``python.*``), and the loader's future version-conflict
    resolver will key on the ID. A fixture with an ID like
    ``PythonFastApi`` would technically pass a looser pattern but
    would break the selector's grouping — a user-visible regression
    that no schema check alone would catch.

    The two assertions per segment are deliberately separate rather
    than joined with ``and``: a compound ``assert a and b`` produces
    a single failure message that does not name which of ``a`` or
    ``b`` was false, so a red CI run would not say whether the
    segment is empty or mis-cased. Splitting keeps each failure
    self-explanatory. This is what pytest's ``PT018`` rule enforces.
    """
    document = _load(fixture)
    blueprint_id = document["id"]

    assert blueprint_id.count(".") >= 1, f"fixture {fixture!r} id {blueprint_id!r} is not dotted"
    assert (
        blueprint_id == blueprint_id.lower()
    ), f"fixture {fixture!r} id {blueprint_id!r} is not lowercase"

    for index, segment in enumerate(blueprint_id.split(".")):
        assert segment, (
            f"fixture {fixture!r} id {blueprint_id!r} has an empty " f"segment at position {index}"
        )
        assert segment[0].isalpha(), (
            f"fixture {fixture!r} id {blueprint_id!r} segment "
            f"{segment!r} (position {index}) must start with a letter"
        )
