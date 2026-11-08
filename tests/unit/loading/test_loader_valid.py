"""Unit tests for `BlueprintLoader.load` — the happy path.

What this suite must prove
--------------------------
The loader's success case is easy to overlook because it has no error
surface: a valid blueprint produces `Ok(blueprint)` and that's the
whole story. But the success case has *four* distinct guarantees that
matter to every downstream consumer, and none of them are exercised by
the error-path tests:

1. **`Ok` is returned, not `Err`.** The loader does not throw, does
   not return `None`, and does not return a partial result. Callers
   branch on `is_ok()` and unwrap; if the loader ever returned
   something else, every call site would break.

2. **The returned value is a `Blueprint` model, not a raw dict.** The
   loader parses *and* returns the typed model — a caller who needs
   the model gets it without re-parsing. If the loader ever returned
   a dict (e.g. after a refactor to "just validate, don't parse"), the
   schema/model parity guarantee (ADR-0008) would silently disappear.

3. **The model is fully populated.** Not just "not None" — its
   scalar fields match the source, its collections are non-empty
   where the fixture had entries, and its nested types (variables,
   files, verification) are the correct Pydantic classes, not raw
   dicts. A future refactor that built the model from a projection
   (e.g. only top-level fields) would pass a `isinstance(Blueprint)`
   check but fail these assertions.

4. **Every valid fixture loads.** The loader's contract is
   mode-agnostic — it does not care which blueprint it is, only that
   it is valid. The test suite iterates over *all* valid fixtures
   rather than testing one, so a regression that affects only
   blueprints with certain features (e.g. blueprints with
   conditional files) is caught on first run.

What is deliberately *not* tested
---------------------------------
* **Error paths.** Those live in `test_loader_malformed_yaml.py`,
  `test_loader_multi_error.py`, and `test_loader_missing_template.py`.
  Mixing success and failure tests in one file makes a failing test's
  module name an unreliable signal about *what* broke.

* **`check_files=False`.** That is a loader-option behaviour, not a
  success-case behaviour — it belongs with the missing-template tests
  (where it is used to prove discovery-style metadata-only loads).
  Testing it here would confuse "the loader succeeded" with "the
  loader was asked to skip a phase."

* **Blueprint content correctness.** The loader's job is "this is a
  valid blueprint, here it is as a model." Whether the blueprint's
  description is 15 words or 50, or whether its variables are named
  `author` or `owner`, is not the loader's concern. The fixtures are
  already validated by the schema/model suites; the loader test
  only asserts the loader *returned them faithfully*.

* **Fixture content drift.** If a fixture is edited to remove a
  variable, the loader still loads it (that is what the loader does);
  only the schema suite catches the semantic change. This test asserts
  *specific field values* to catch the loader silently dropping data,
  but it does not assert that the fixture's shape matches some
  ideal — that would couple the loader test to the fixture's
  content, which is the schema suite's job.

Fixture strategy
----------------
The valid fixtures live under `tests/fixtures/blueprints/valid/` and
are consumed by the schema tests, the model tests, the discovery
tests, and this loader test. That is the point: a valid blueprint is
valid for every layer, and every layer's test proves it accepts what
the others accept. A fixture that the schema accepts but the loader
rejects would be a critical bug that the shared fixture would catch
immediately.

The tests are parameterised over fixtures *by name*, not by directory
listing, so a new fixture is added deliberately (with its expected
field assertions), not implicitly (and possibly without verification
that it loads correctly under the loader).
"""

from __future__ import annotations

from pathlib import Path

import pydantic
import pytest

from forge.loading import BlueprintLoader, Ok
from forge.loading.result import Result
from forge.models import Blueprint

# Shared with the schema, model, and discovery test suites. If a
# fixture moves or a new one is added, every suite sees the change.
_FIXTURES = Path(__file__).parents[2] / "fixtures" / "blueprints" / "valid"

# The three official blueprints that must exist. Naming them here (in
# addition to discovering them from the directory) makes a missing
# fixture a clear failure of this test, not a mysterious "no
# parameterised cases" error. It also documents the loader's contract
# — "these three blueprints, at minimum, must load."
_EXPECTED_FIXTURES = [
    "python-api.yaml",
    "python-automation.yaml",
    "typescript-frontend.yaml",
]


def _load(fixture: str) -> Result:
    """Helper to keep each test's body focused on its assertion.

    The `check_files=True` default is intentional — this is the
    default `forge validate` and `forge create` use, and the whole
    point of the success suite is proving the *default* loader
    path succeeds on valid fixtures.
    """
    return BlueprintLoader().load(_FIXTURES / fixture)


# =============================================================================
# Fixture inventory — guard against silent fixture removal
# =============================================================================
class TestFixtureInventory:
    """Every expected fixture exists, and the directory has no strays.

    This is a lightweight sanity check that runs before the heavier
    parameterised tests. Its failure mode (a missing fixture file) is
    common during refactors — someone renames a fixture, updates the
    schema tests, and forgets to update the loader tests. Catching
    the inventory mismatch at the top of the file makes the fix
    obvious; without it, the failure would appear as three
    cryptic "file not found" errors from downstream tests.
    """

    @pytest.mark.parametrize("fixture", _EXPECTED_FIXTURES)
    def test_expected_fixture_exists(self, fixture: str):
        assert (_FIXTURES / fixture).is_file(), (
            f"expected fixture {fixture!r} is missing from "
            f"{_FIXTURES}. Add it, or update _EXPECTED_FIXTURES if it was "
            f"intentionally removed."
        )

    def test_no_unexpected_fixtures(self):
        """New fixtures must be added to ``_EXPECTED_FIXTURES``, not
        silently picked up by the directory listing.

        An implicit "iterate the directory" test would pass when a
        contributor drops in a fixture that does not actually load —
        the loader would succeed on the *other* fixtures and the new
        one would never be exercised. Requiring the fixture name to
        be listed forces the contributor to add a positive test for
        it, which is exactly the case a new fixture should exercise.
        """
        on_disk = {p.name for p in _FIXTURES.glob("*.yaml") if p.is_file()}
        expected = set(_EXPECTED_FIXTURES)
        extras = on_disk - expected
        assert not extras, (
            f"fixtures present but not in _EXPECTED_FIXTURES: {sorted(extras)}. "
            f"Add them to the list (with field assertions in the "
            f"appropriate test class) or remove them."
        )


# =============================================================================
# The core contract — every valid fixture returns Ok(Blueprint)
# =============================================================================
class TestReturnsOkBlueprint:
    """`Ok(Blueprint)` for every valid fixture, no surprises.

    The parameterisation is what gives this test its power: a
    regression that affects *all* fixtures (e.g. a loader refactor
    that returns a dict) fails here, and a regression that affects
    only *some* fixtures (e.g. a bug in conditional-files handling
    that only affects fixtures with conditional files) also fails
    here, on the specific parameter that exercises the bug.
    """

    @pytest.mark.parametrize("fixture", _EXPECTED_FIXTURES)
    def test_load_returns_ok(self, fixture: str):
        """The `Result` is `Ok`, not `Err` — this is the whole contract."""
        result = _load(fixture)
        assert isinstance(result, Ok), (
            f"expected Ok for {fixture!r}, got Err: " f"{getattr(result, 'error', '<no error>')}"
        )

    @pytest.mark.parametrize("fixture", _EXPECTED_FIXTURES)
    def test_wrapped_value_is_blueprint_model(self, fixture: str):
        """The wrapped value is the *typed* model, not a raw dict.

        Returning a dict would satisfy "validation succeeded" but
        silently break the ADR-0008 guarantee that callers get a
        typed model without re-parsing. The `isinstance` check
        pins the type.
        """
        result = _load(fixture)
        assert isinstance(result, Ok)
        assert isinstance(result.value, Blueprint)

    @pytest.mark.parametrize("fixture", _EXPECTED_FIXTURES)
    def test_unwrap_returns_same_object(self, fixture: str):
        """`unwrap()` and `.value` must agree — callers use both.

        This is a property of `Result` (`test_result.py` covers it
        exhaustively), but it is worth re-asserting in the loader
        context: a future refactor that wraps the model in a copy
        (e.g. to make it "safer") would break identity here before
        it breaks callers that rely on it downstream.
        """
        result = _load(fixture)
        assert isinstance(result, Ok)
        assert result.unwrap() is result.value


# =============================================================================
# Field fidelity — the model matches the source
# =============================================================================
class TestFieldFidelity:
    """Scalars on the model match the fixture's YAML.

    These assertions catch a class of bug the previous class cannot:
    a loader that returns *a* Blueprint, but not *the* Blueprint.
    This could happen if a future refactor built the model from a
    projection (e.g. only top-level fields), or if the loader
    re-validated the model after construction (which is a no-op
    today but could become lossy if the model gained a `from_orm`-
    style constructor).

    Each fixture is tested individually rather than parameterised
    because the field values differ per fixture, and asserting them
    by fixture name makes the test readable as a specification of
    what each fixture contains.
    """

    def test_python_api_scalar_fields_match(self):
        """Spot-check the well-known fields against the fixture."""
        result = _load("python-api.yaml")
        assert isinstance(result, Ok)
        bp = result.value

        assert bp.id == "python.fastapi.api"
        assert bp.name == "Python FastAPI API"
        assert bp.version == "1.0.0"
        assert bp.category.value == "api"

    def test_python_automation_scalar_fields_match(self):
        result = _load("python-automation.yaml")
        assert isinstance(result, Ok)
        bp = result.value

        # The exact strings live in the fixture; only the identity
        # fields are asserted here. A future fixture reword of the
        # description should not fail this test — the loader only
        # promises fidelity, not content.
        assert bp.id == "python.automation.script"
        assert bp.category.value == "automation"

    def test_typescript_frontend_scalar_fields_match(self):
        result = _load("typescript-frontend.yaml")
        assert isinstance(result, Ok)
        bp = result.value

        assert bp.id == "typescript.vite.react"
        assert bp.category.value == "frontend"

    @pytest.mark.parametrize("fixture", _EXPECTED_FIXTURES)
    def test_source_label_is_on_the_model_via_load_path(self, fixture: str):
        """The loader does not invent a `source` field, but it also
        does not require one. The model's `id` reflects the
        fixture's declared id, not the file path.

        This is a subtle but important contract: the loader returns
        the *declared* identity, not the *file's* identity. A future
        refactor that set `bp.id = fixture.stem` (a plausible
        "convenience") would break this.
        """
        result = _load(fixture)
        assert isinstance(result, Ok)
        # The declared id is always dotted-lowercase and matches the
        # fixture name (by convention in the fixtures directory).
        assert "." in result.value.id
        assert result.value.id != fixture


# =============================================================================
# Nested collections are typed and non-empty where the fixture had them
# =============================================================================
class TestNestedCollections:
    """The model's nested collections use Pydantic classes, not dicts.

    A caller iterating `bp.variables` and calling `var.type.value`
    relies on `var` being a `VariableDefinition`, not a raw dict.
    This test proves the loader constructs the nested tree, not just
    the top level — a subtle failure mode of "validate then return
    the input dict" implementations.
    """

    def test_python_api_variables_are_typed(self):
        result = _load("python-api.yaml")
        assert isinstance(result, Ok)
        bp = result.value

        assert bp.variables, "fixture is expected to declare variables"
        for var in bp.variables:
            # Nested class name is checked structurally, not by
            # importing the class — the loader test should not couple
            # to the model module's exact exports.
            assert type(var).__name__ == "VariableDefinition"
            assert hasattr(var, "id")
            assert hasattr(var, "type")

    def test_python_api_files_are_typed(self):
        result = _load("python-api.yaml")
        assert isinstance(result, Ok)
        bp = result.value

        assert bp.files, "fixture is expected to declare files"
        for entry in bp.files:
            assert type(entry).__name__ == "FileEntry"
            assert hasattr(entry, "source")
            assert hasattr(entry, "destination")

    def test_python_api_verification_is_typed(self):
        result = _load("python-api.yaml")
        assert isinstance(result, Ok)
        bp = result.value

        assert bp.verification, "fixture is expected to declare verification"
        for check in bp.verification:
            assert type(check).__name__ == "CheckDefinition"
            assert hasattr(check, "id")
            assert hasattr(check, "type")

    def test_compatibility_is_typed(self):
        result = _load("python-api.yaml")
        assert isinstance(result, Ok)
        bp = result.value

        assert bp.compatibility is not None
        assert hasattr(bp.compatibility, "min_forge_version")
        # SemVer shape — the schema already guarantees this, but the
        # loader must not have dropped the string.
        assert bp.compatibility.min_forge_version.count(".") >= 2


# =============================================================================
# Immutability and reentrance
# =============================================================================
class TestImmutability:
    """Loaded models are frozen — mutation would corrupt callers.

    `Blueprint` inherits `_StrictModel`'s `frozen=True`. A future
    refactor that made it mutable would let one consumer's
    post-processing silently change another consumer's view. The
    loader's job is to return an immutable snapshot; testing it here
    makes the loader-level implication of the model's contract
    explicit.
    """

    def test_returned_blueprint_is_frozen(self):
        result = _load("python-api.yaml")
        assert isinstance(result, Ok)
        # Pydantic v2 raises `pydantic.ValidationError` (specifically
        # `pydantic_core.ValidationError`, which `pydantic` re-exports)
        # when assigning to a field of a `frozen=True` model — it does
        # *not* raise `AttributeError`/`FrozenInstanceError` from this
        # path. Catching the specific class (rather than the wider
        # `Exception`) is what makes this test's failure mode
        # meaningful: an unrelated exception during the assignment
        # would be a real bug, not a "the model isn't frozen" result.
        # The `type: ignore[misc]` is needed because `Blueprint` is
        # statically frozen, so mypy rejects the assignment.
        with pytest.raises(pydantic.ValidationError):
            result.value.name = "mutated"  # type: ignore[misc]

    def test_loading_twice_returns_distinct_objects(self):
        """Two loads parse twice; the returned models are equal but not
        identical.

        This is a correctness property (a cached model would surprise
        tests that mutate a returned object) and a performance
        documentation (there is no cache; callers that need one must
        add it). Both are important to pin, since a future "optimise
        by caching" change would look harmless but could cause subtle
        bugs in consumers that expect fresh snapshots.
        """
        first = _load("python-api.yaml")
        second = _load("python-api.yaml")
        assert isinstance(first, Ok)
        assert isinstance(second, Ok)
        assert first.value == second.value
        assert first.value is not second.value


# =============================================================================
# No errors on the success path
# =============================================================================
class TestNoLeakedErrors:
    """On success, nothing leaks into the error channel."""

    @pytest.mark.parametrize("fixture", _EXPECTED_FIXTURES)
    def test_ok_has_no_error_attribute(self, fixture: str):
        """The `Ok` case must not expose `.error`.

        A tempting "improvement" is to make `Ok` carry a `None`
        error field for uniformity. That would let a caller write
        `result.error` on a success case without an `AttributeError`,
        silently masking "I forgot to check the branch." The
        absence of `.error` is a deliberate asymmetry with `Err`'s
        absence of `.value`.
        """
        result = _load(fixture)
        assert isinstance(result, Ok)
        assert not hasattr(result, "error")

    @pytest.mark.parametrize("fixture", _EXPECTED_FIXTURES)
    def test_ok_is_err_is_false(self, fixture: str):
        """The `is_err()` branch must be explicitly false, not falsy-by-accident.

        Callers write `if result.is_err():` — the return value must
        be `False`, not `None` or `""`.
        """
        result = _load(fixture)
        assert result.is_ok() is True
        assert result.is_err() is False


# =============================================================================
# Additional fixtures — the schema accepts what the loader accepts
# =============================================================================
class TestParityWithSchemaFixtures:
    """The loader accepts every fixture the schema suite accepts.

    This is the loader-level corollary of ADR-0007/0008: if a
    document passes schema and model validation, the loader must
    return it as `Ok`. Testing this *through the loader* proves the
    loader does not add its own hidden validation on top of the
    schema and model — a tempting "safety" addition that would make
    the loader's accept set a strict subset of the schema's, which
    is exactly the drift the loader is supposed to prevent.

    The test is a *contract* test: it does not exercise new
    fixtures; it proves the shared fixtures (used by every suite)
    load cleanly through the loader. This is why the fixture
    directory is shared across layers rather than duplicated per
    layer.
    """

    @pytest.mark.parametrize("fixture", _EXPECTED_FIXTURES)
    def test_valid_blueprints_accepted_by_loader(self, fixture: str):
        """Loader accept set == schema+model accept set, for known fixtures."""
        result = _load(fixture)
        assert isinstance(result, Ok), (
            f"loader rejected {fixture!r} despite it being a valid "
            f"blueprint. This indicates the loader is adding hidden "
            f"validation on top of the schema/model contract."
        )
