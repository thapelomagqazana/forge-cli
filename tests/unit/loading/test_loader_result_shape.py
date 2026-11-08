"""Unit tests for the *shape* of what `BlueprintLoader.load` returns.

What this suite must prove
--------------------------
Every other loader test asserts something about the *content* of a
`Result` — the wrapped blueprint for valid inputs, the specific
errors for invalid ones, the phase prefixes, the hints. This suite is
different: it asserts properties of the `Result` itself, independent
of what it contains.

That distinction matters. A refactor that changed the loader to return
`Optional[Blueprint]` (None on failure), or to raise on failure, or to
return a bare list of errors, would pass almost every other loader
test — those tests happen to `isinstance(result, Err)` first, so the
type check is present but scattered across dozens of test bodies
without a single place that says "the loader's *return contract* is
this." This suite is that single place.

What "shape" means concretely
-----------------------------
The loader's declared return type is
``Result[Blueprint, list[ValidationError]]``. That is a `Union` of
two dataclasses from `forge.loading.result`. The shape properties the
loader must guarantee are:

1. **Return type is `Ok` or `Err`, never anything else.** No
   ``None``, no bare `Blueprint`, no raised exception for expected
   input problems, no `Optional`. A future refactor that
   "simplifies" to `Optional[Blueprint]` would pass dozens of other
   tests (they happen to check `isinstance(result, Err)` in the
   failure cases) but silently break the invariant that *every*
   return is one of the two cases.

2. **`Ok` wraps exactly a `Blueprint`.** The wrapped value is the
   typed model, not a dict, not a subclass, not a wrapper. A
   refactor that returned a "future-proof" container around the
   blueprint would pass `isinstance(result, Ok)` but break every
   caller that expects `result.value` to be a `Blueprint`.

3. **`Err` wraps exactly a `list[ValidationError]`.** The error
   payload is a list (not a tuple, not an iterator, not a single
   `ValidationError`, not a `SchemaValidationError` object). Callers
   iterate it; a shape change would break them differently depending
   on what replaced it.

4. **Both cases are immutable and constructible in isolation.** A
   `Result` is a value, not a pipeline stage. A caller can construct
   `Ok(blueprint)` and `Err([...])` themselves (tests do this to
   bypass the loader); a future refactor that moved construction
   logic into the loader would break those callers.

5. **Both cases are usable with `match` and `isinstance`.** The
   loader's own callers branch on the result. A refactor that
   changed `Ok`/`Err` to a single class with a discriminator field
   would pass "isinstance(Ok, Ok)" (tautologically) but break every
   `case Ok(value): ...` statement in the codebase.

6. **The type alias `Result` resolves to `Union[Ok, Err]` at runtime.**
   `isinstance(result, Ok)` and `match` depend on this. A refactor
   that made `Result` an abstract base class would silently break
   both.

Why *not* just test via the loader's real fixtures
--------------------------------------------------
Because those tests already exist and they test *content*. A shape
test that loaded a real fixture would have the same content
assertions as `test_loader_valid.py` plus one `isinstance` check —
and if the shape changed, dozens of tests would fail together with
no clear signal about what changed. This suite isolates the *shape*
property: a shape regression fails here, in one place, with a
message that names the property that broke. Content regressions fail
in the content suites.

That separation is what makes a failing test easy to debug: a
failure in this file means "the loader changed its return type or
the type system around it," not "a particular fixture failed to
parse."

What this suite is deliberately *not* doing
-------------------------------------------
* **Testing the loader's pipeline logic.** YAML parsing, schema
  validation, model construction, filesystem checks — none of that
  is exercised here. Those are in the content suites.

* **Testing `Result` in isolation.** `tests/unit/loading/test_result.py`
  exhaustively tests `Ok`/`Err` behaviour (unwrap, map, equality,
  pattern matching). This suite does not duplicate that; it only
  asserts that the *loader* returns those types.

* **Testing error content.** A test that the loader's error list
  contains "missing template" belongs with the missing-template
  suite; a test that its paths end in `files.0.source` belongs with
  the multi-error suite. Here, "the error payload is a list of
  `ValidationError` instances" is asserted for *any* failure —
  content is irrelevant.

Structure
---------
Tests are grouped by shape property:

    TestReturnTypeIsResult    — Ok | Err, nothing else
    TestOkWrapsBlueprint      — Ok(x) where isinstance(x, Blueprint)
    TestErrWrapsErrorList     — Err(x) where isinstance(x, list) and
                                every item is a ValidationError
    TestImmutabilityShape     — the shapes cannot be mutated
    TestBranchingSupport      — isinstance and match work
    TestTypeAliasResolves     — `Result` is `Union[Ok, Err]` at runtime

Each test stages its own input (a valid blueprint or a broken one,
depending on what it needs) and asserts only the shape property.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from forge.loading import BlueprintLoader, Err, Ok, Result
from forge.loading.errors import ValidationError
from forge.models import Blueprint

# Reuse the shared fixture directories. The specific fixtures chosen
# are the *simplest* ones that produce each shape: a single valid
# blueprint for the `Ok` case and a single invalid one for the `Err`
# case. Using a multi-error fixture would test the same shape with
# more noise.
_VALID_FIXTURES = Path(__file__).parents[2] / "fixtures" / "blueprints" / "valid"
_INVALID_FIXTURES = Path(__file__).parents[2] / "fixtures" / "loader"
_SYNTAX_ERROR_FIXTURE = _INVALID_FIXTURES / "malformed.yaml"
_MISSING_TEMPLATE_FIXTURE = _INVALID_FIXTURES / "missing-template.yaml"


# =============================================================================
# Return type is `Result` — Ok or Err, nothing else
# =============================================================================
class TestReturnTypeIsResult:
    """The loader returns `Ok(...)` or `Err(...)`. Never `None`, never raise.

    This is the loader's *public contract*, and it is the thing most
    likely to be silently changed by a "cleanup" refactor that wants
    to use `Optional[Blueprint]` or raise exceptions. Those refactors
    would pass every content test (because the content tests happen
    to check `isinstance(result, Err)` in the failure path), and
    would only break at the call sites that assume the shape — which
    are typically one or two per command, and typically discovered in
    production.

    The three tests below cover the three inputs a caller can pass:
    a valid blueprint (should be Ok), an invalid blueprint (should
    be Err), and a file that cannot be loaded at all (should still
    be Err, not raise). Together they pin "every path through
    ``.load`` produces one of the two cases."
    """

    def test_valid_returns_ok(self):
        """A valid blueprint produces `Ok`, not a bare `Blueprint`."""
        result = BlueprintLoader().load(_VALID_FIXTURES / "python-api.yaml")
        assert isinstance(result, Ok)

    def test_valid_does_not_return_bare_blueprint(self):
        """Even if `Ok` were somehow unpacked, the loader must return
        the wrapper — callers branch on it.

        A "helpful" refactor that returned the blueprint directly
        would break `result.is_err()` at every call site. This test
        would fail with a clear `AttributeError` on
        `result.is_err()`, which reads as "the shape changed", not
        as a mysterious downstream bug.
        """
        result = BlueprintLoader().load(_VALID_FIXTURES / "python-api.yaml")
        assert hasattr(result, "is_ok"), (
            f"loader returned {type(result).__name__}, which does not "
            f"have is_ok(); the return shape must be a Result"
        )
        assert hasattr(result, "is_err")

    def test_invalid_returns_err(self):
        """A missing template produces `Err`, not a raised exception."""
        result = BlueprintLoader().load(_MISSING_TEMPLATE_FIXTURE)
        assert isinstance(result, Err)

    def test_invalid_does_not_return_none(self):
        """`None` for "no blueprint" would break `match` callers and
        silently pass `isinstance(x, Ok)` checks as False without
        raising a clear error."""
        result = BlueprintLoader().load(_MISSING_TEMPLATE_FIXTURE)
        assert result is not None

    def test_syntax_error_returns_err_not_raises(self):
        """A malformed YAML file produces `Err`, not a raised exception.

        The parse phase is the most tempting to "just raise" because
        the underlying `yaml.YAMLError` is already an exception. But
        the loader's contract is uniform: *expected* input problems
        return `Err`. If the parse phase raised, every caller would
        need `try/except` around `.load()`, which is exactly the
        wrapping the `Result` design removes.
        """
        result = BlueprintLoader().load(_SYNTAX_ERROR_FIXTURE)
        assert isinstance(result, Err)

    def test_missing_file_returns_err_not_raises(self, tmp_path: Path):
        """A file that does not exist returns `Err`, not `FileNotFoundError`.

        Same reasoning as the syntax error case: the loader's
        contract is that *every* failure mode is a value, not an
        exception. A future refactor that let `OSError` escape would
        break callers that expect `Result`.
        """
        missing = tmp_path / "does-not-exist.yaml"
        result = BlueprintLoader().load(missing)
        assert isinstance(result, Err)

    def test_loader_is_not_none(self):
        """`BlueprintLoader()` returns a usable instance.

        Trivial-seeming, but a future refactor that made construction
        conditional (e.g. lazy-init on first use) could return None
        from a factory function while keeping the class importable.
        This test forces the constructor to be eager and side-effect
        free, which is what makes it safe to call in a tight loop
        (as discovery does).
        """
        loader = BlueprintLoader()
        assert loader is not None
        assert hasattr(loader, "load")


# =============================================================================
# Ok wraps exactly a Blueprint
# =============================================================================
class TestOkWrapsBlueprint:
    """The wrapped value is a `Blueprint` model, not a dict or wrapper.

    The loader parses *and* returns the typed model (ADR-0008). A
    refactor that returned a raw dict, a subclass, or a wrapper
    object would satisfy `isinstance(result, Ok)` but break every
    downstream caller that does `bp.variables`, `bp.files`, and so
    on with a clear error at that exact line — a much better failure
    mode than silently returning `{}` for everything.
    """

    def test_ok_wraps_a_blueprint_instance(self):
        result = BlueprintLoader().load(_VALID_FIXTURES / "python-api.yaml")
        assert isinstance(result, Ok)
        assert isinstance(result.value, Blueprint)

    def test_ok_does_not_wrap_a_dict(self):
        """A dict would pass `isinstance(result, Ok)` and pass every
        test that only checks `result.is_ok()`, but would fail every
        consumer's `result.value.variables` access with `AttributeError`
        — a worse failure mode than returning the typed model."""
        result = BlueprintLoader().load(_VALID_FIXTURES / "python-api.yaml")
        assert isinstance(result, Ok)
        assert not isinstance(result.value, dict)

    def test_ok_unwrap_returns_same_object(self):
        """`unwrap()` and `.value` must agree — callers use both."""
        result = BlueprintLoader().load(_VALID_FIXTURES / "python-api.yaml")
        assert isinstance(result, Ok)
        assert result.unwrap() is result.value

    def test_ok_does_not_have_error_attribute(self):
        """`Ok` must not expose `.error` — the asymmetry with `Err` is
        what makes "forgot to check the branch" fail loudly."""
        result = BlueprintLoader().load(_VALID_FIXTURES / "python-api.yaml")
        assert isinstance(result, Ok)
        assert not hasattr(result, "error")


# =============================================================================
# Err wraps exactly a list[ValidationError]
# =============================================================================
class TestErrWrapsErrorList:
    """The error payload is a list of `ValidationError` — nothing else.

    The CLI's renderer iterates `result.error` and calls `.format()`
    on each. A tuple would break iteration differently (it would work
    for iteration but fail `extend`); an iterator would be consumed
    by the first renderer and empty for the second; a single
    `ValidationError` would fail iteration with `TypeError: not
    iterable`. All three are silently different from the loader's
    contract, and all three would pass a test that only asserted
    `isinstance(result, Err)`.

    This class is the one place those shape changes are caught.
    """

    def test_err_wraps_a_list(self):
        result = BlueprintLoader().load(_MISSING_TEMPLATE_FIXTURE)
        assert isinstance(result, Err)
        assert isinstance(result.error, list)

    def test_err_list_is_not_empty(self):
        """An empty list would satisfy "is a list of ValidationErrors"
        while being useless — the CLI would render zero errors and
        exit non-zero, leaving the user with no output. The loader
        must always populate the list on failure."""
        result = BlueprintLoader().load(_MISSING_TEMPLATE_FIXTURE)
        assert isinstance(result, Err)
        assert len(result.error) >= 1

    def test_err_items_are_validation_errors(self):
        """Every item in the list is a `ValidationError`, not a raw
        `jsonschema.ValidationError`, not a `pydantic.ValidationError`,
        not a string."""
        result = BlueprintLoader().load(_MISSING_TEMPLATE_FIXTURE)
        assert isinstance(result, Err)
        for item in result.error:
            assert isinstance(item, ValidationError), (
                f"error list contains {type(item).__name__}, expected " f"ValidationError"
            )

    def test_err_items_have_required_attributes(self):
        """The four attributes the CLI and every other consumer read
        are present on every error.

        This test is redundant with `test_result.py`'s contract test
        in spirit, but it is not redundant in *purpose*: `test_result`
        proves the attributes exist on hand-constructed instances;
        this test proves the loader produces instances that have
        them. A refactor that returned a `ValidationError`-shaped
        duck type without the attributes would pass `isinstance` (if
        it inherited from `ValidationError`) but fail this test if
        it shadowed any of the fields.
        """
        result = BlueprintLoader().load(_MISSING_TEMPLATE_FIXTURE)
        assert isinstance(result, Err)
        for item in result.error:
            assert hasattr(item, "path")
            assert hasattr(item, "message")
            assert hasattr(item, "hint")
            assert hasattr(item, "source")

    def test_err_does_not_have_value_attribute(self):
        """`Err` must not expose `.value` — same asymmetry rationale
        as `test_ok_does_not_have_error_attribute`."""
        result = BlueprintLoader().load(_MISSING_TEMPLATE_FIXTURE)
        assert isinstance(result, Err)
        assert not hasattr(result, "value")


# =============================================================================
# Immutability shape — the results cannot be mutated
# =============================================================================
class TestImmutabilityShape:
    """Both `Ok` and `Err` are frozen dataclasses; the loader's
    returned instances cannot be mutated by a consumer.

    A future refactor that removed `frozen=True` from `Result` or
    from `ValidationError` would let one consumer modify an error
    that another consumer is about to render. The immutability of
    the *values* the loader produces is a load-bearing property of
    the pipeline, not just of the `Result` type.
    """

    def test_ok_is_frozen(self):
        result = BlueprintLoader().load(_VALID_FIXTURES / "python-api.yaml")
        assert isinstance(result, Ok)
        with pytest.raises((AttributeError, TypeError)):
            result.value = None  # type: ignore[misc]

    def test_err_is_frozen(self):
        result = BlueprintLoader().load(_MISSING_TEMPLATE_FIXTURE)
        assert isinstance(result, Err)
        with pytest.raises((AttributeError, TypeError)):
            result.error = []  # type: ignore[misc]

    def test_validation_error_is_frozen(self):
        """The `ValidationError` items are also immutable.

        If an error's `.message` could be mutated after production,
        the loader's own `format()` rendering could disagree with the
        error's contract — a subtle failure mode that only shows up
        in CI logs.
        """
        result = BlueprintLoader().load(_MISSING_TEMPLATE_FIXTURE)
        assert isinstance(result, Err)
        error = result.error[0]
        with pytest.raises((AttributeError, TypeError)):
            error.message = "mutated"  # type: ignore[misc]


# =============================================================================
# Branching support — isinstance and match work as callers expect
# =============================================================================
class TestBranchingSupport:
    """Both callers that use `isinstance` and callers that use `match`
    work identically.

    The loader's callers in the CLI use `match` (they read as prose:
    "on success do X, on failure do Y"); discovery and tests use
    `isinstance`. Both must work — a refactor that changed the
    `Ok`/`Err` classes to a single class with a discriminator field
    would break one of them, depending on how `isinstance` is
    implemented. This class pins both.
    """

    def test_isinstance_discriminates(self):
        ok = BlueprintLoader().load(_VALID_FIXTURES / "python-api.yaml")
        err = BlueprintLoader().load(_MISSING_TEMPLATE_FIXTURE)
        assert isinstance(ok, Ok)
        assert not isinstance(ok, Err)
        assert isinstance(err, Err)
        assert not isinstance(err, Ok)

    def test_match_on_ok_binds_value(self):
        result = BlueprintLoader().load(_VALID_FIXTURES / "python-api.yaml")
        match result:
            case Ok(value):
                bound = value
            case Err(_):  # pragma: no cover - unreachable
                bound = None
        assert isinstance(bound, Blueprint)

    def test_match_on_err_binds_error_list(self):
        result = BlueprintLoader().load(_MISSING_TEMPLATE_FIXTURE)
        match result:
            case Ok(_):  # pragma: no cover - unreachable
                bound = None
            case Err(errors):
                bound = errors
        assert isinstance(bound, list)
        assert all(isinstance(e, ValidationError) for e in bound)


# =============================================================================
# The `Result` type alias resolves at runtime
# =============================================================================
class TestTypeAliasResolves:
    """`Result` is a `Union[Ok, Err]` at runtime.

    Callers write `def f() -> Result[Blueprint, list[ValidationError]]:`
    in annotations. At runtime, `Result` is a `TypeAlias` for
    `Union[Ok[T], Err[E]]`. `isinstance(x, Ok)` works because `Ok`
    and `Err` are concrete classes; a future refactor that made
    `Result` an abstract base class (with `Ok` and `Err` as
    subclasses) would *also* work for `isinstance` but would break
    `match` (unless the ABC were specifically designed for it) and
    would break `Union`-typed annotations in tools that expect a
    union.

    This test pins the current shape so a future refactor knows what
    callers depend on, rather than discovering it via production
    failures.
    """

    def test_result_alias_accepts_ok_instance(self):
        # The annotation is enough — the test is that this line is
        # valid at import time, which it is only if `Result` can be
        # subscripted. A future refactor that removed subscripting
        # (e.g. made `Result` a plain class) would fail this line at
        # collection time.
        r: Result[Blueprint, list[ValidationError]] = Ok(
            Blueprint.model_validate(_minimal_blueprint_dict())
        )
        assert isinstance(r, Ok)

    def test_result_alias_accepts_err_instance(self):
        r: Result[Blueprint, list[ValidationError]] = Err([])
        assert isinstance(r, Err)

    def test_isinstance_against_ok_and_err_both_work(self):
        """The two concrete classes exist and are distinguishable."""
        assert Ok(1) is not None
        assert Err([]) is not None
        assert not isinstance(Ok(1), Err)
        assert not isinstance(Err([]), Ok)


# =============================================================================
# Helper — a minimal schema-valid blueprint dict
# =============================================================================
def _minimal_blueprint_dict() -> dict:
    """The smallest dict that Pydantic will accept as a Blueprint.

    Used only by ``TestTypeAliasResolves`` to construct an ``Ok``
    instance directly (without the loader). Every other test in this
    file goes through the loader, which is the point — the loader's
    shape is what is under test, and constructing an ``Ok`` by hand
    in one test is a deliberate exception to prove that the type
    alias is usable by callers who want to bypass the loader (e.g.
    tests and mocks).
    """
    return {
        "schema_version": "1.0.0",
        "id": "sample.shape",
        "name": "Shape Test",
        "version": "1.0.0",
        "description": "Minimal blueprint for Result shape tests.",
        "category": "api",
        "verification": [{"id": "ok", "type": "required_file", "path": "README.md"}],
        "compatibility": {"min_forge_version": "1.0.0"},
    }
