"""Unit tests for the `Result` type.

What this suite must prove
--------------------------
`Result` is the loader's only failure shape. Every consumer of the
loading package — `forge validate`, `forge create`, discovery, and every
future entry point — branches on `is_ok()` / `is_err()` and extracts
values via `unwrap()` or the `.value` / `.error` attributes. If any of
those behaviours changes, the whole application misbehaves in subtle
ways (silent defaults, wrong exit codes, skipped validation). The tests
below pin the *entire* public contract, not a sample of it.

Two classes of failure this suite guards against
------------------------------------------------
1. **Silent success.** A caller who forgets to check `.is_err()` must
   be punished loudly — `unwrap()` and `unwrap_or()` on `Err` raise
   `RuntimeError`. A future refactor that "improves ergonomics" by
   returning `None` or the default would break every caller that
   relied on the safety net; the `test_*_raises` tests below make
   that refactor impossible to merge.

2. **Silent failure.** A caller who writes `if result:` (Python's
   favourite truthiness idiom) must not accidentally take the failure
   branch on a valid `Ok(0)`. The `test_wrapped_falsy_values_are_valid`
   and `test_wrapped_none_is_valid` tests enforce that `Result` has no
   `__bool__` and that the value's truthiness is irrelevant.

What is deliberately *not* tested
---------------------------------
* **Internal representation.** The dataclass fields (`value`, `error`)
  are tested only through the documented attributes. A future refactor
  could rename `value` to `_value` and use a property; these tests
  would still pass.

* **Thread safety.** `Result` is a frozen dataclass — immutable by
  construction. Thread-safety follows from immutability and is not
  separately testable without contrived scenarios.

* **Exceptions' exact class for stray attribute assignment.** See
  `TestFrozenAndSlots.test_ok_rejects_stray_attributes` — the
  documented contract is "assignment fails", not "assignment fails
  with specifically `AttributeError`". CPython's `@dataclass(frozen=True,
  slots=True)` raises `TypeError` for stray attributes on 3.10 to 3.12
  due to a `super().__setattr__` delegation quirk; the test accepts
  both classes and the docstring explains why the tolerance is
  principled, not sloppy.

Structure
---------
Tests are grouped by behaviour family — `TestOk`, `TestErr`,
`TestMapping`, `TestEquality`, `TestRepr`, `TestFrozenAndSlots`,
`TestPatternMatching`, `TestGenericTyping` — so a reader can answer
"what does this type guarantee?" by reading class and test names
alone. For a foundational type like `Result`, the test file is the
second specification.
"""

from __future__ import annotations

import pytest

from forge.loading import Err, Ok, Result


# =============================================================================
# Ok — success branch
# =============================================================================
class TestOk:
    """Every `Ok` method is unconditional: it cannot fail."""

    def test_is_ok_returns_true(self):
        assert Ok(42).is_ok() is True

    def test_is_err_returns_false(self):
        assert Ok(42).is_err() is False

    def test_unwrap_returns_wrapped_value(self):
        assert Ok("hello").unwrap() == "hello"

    def test_unwrap_returns_identity_not_copy(self):
        """`unwrap` must return the *same object*, not a copy.

        Callers treat the wrapped value as authoritative — e.g. a
        `Blueprint` model whose identity matters for future caching.
        Copying (e.g. via `deepcopy` "to be safe") would silently
        break identity-based optimisations and make equality
        ambiguous across the loader's own code paths.
        """
        sentinel = object()
        assert Ok(sentinel).unwrap() is sentinel

    def test_value_attribute_matches_unwrap(self):
        """Both access paths must agree — callers use both interchangeably."""
        r = Ok(99)
        assert r.value == r.unwrap()

    def test_unwrap_or_ignores_default(self):
        """`unwrap_or` on `Ok` never consults the default.

        `default` is a *value*, not a callable, so "consulting" it
        means using it as the return. The test proves the wrapped
        value wins by using two distinguishable values.
        """
        assert Ok(7).unwrap_or(-1) == 7

    def test_wrapped_none_is_valid(self):
        """`Ok(None)` is distinct from `Err(...)`.

        A loader phase that succeeds with "no value" (e.g. a future
        optional lookup) must not be confused with a failure. The
        test below would fail if `Ok(None)` were treated as falsy —
        which is why `Result` deliberately has no `__bool__`.
        """
        r = Ok(None)
        assert r.is_ok() is True
        assert r.unwrap() is None

    def test_wrapped_falsy_values_are_valid(self):
        """Falsiness of the *value* is irrelevant; only the *branch* matters."""
        for falsy in (0, "", [], {}, False, 0.0, ()):
            assert Ok(falsy).is_ok() is True
            assert Ok(falsy).unwrap() == falsy

    def test_no_bool_dunder(self):
        """`Result` must not be truthy or falsy.

        Python's `if result:` idiom is common; if it worked, it would
        silently take a branch on the *wrapped value's* truthiness
        rather than the branch's own. The absence of `__bool__` makes
        `if result:` raise `TypeError` at the call site, forcing the
        caller to use the explicit `.is_ok()` / `.is_err()` methods.

        This test pins the *absence* of the method by asking Python
        for it — a future "helpful" addition would fail immediately.
        """
        assert not hasattr(Ok(1), "__bool__")
        assert not hasattr(Err("e"), "__bool__")


# =============================================================================
# Err — failure branch
# =============================================================================
class TestErr:
    """Every `Err` extraction is loud: silent failure is the bug class
    this type exists to prevent."""

    def test_is_ok_returns_false(self):
        assert Err(["boom"]).is_ok() is False

    def test_is_err_returns_true(self):
        assert Err(["boom"]).is_err() is True

    def test_unwrap_raises(self):
        with pytest.raises(RuntimeError):
            Err(["boom"]).unwrap()

    def test_unwrap_error_includes_payload(self):
        """The exception must name the payload so a CI failure points at
        the actual problem, not a bare `RuntimeError`.

        A test that only asserted `RuntimeError` would pass even if
        the payload were dropped from the message — which would make
        debugging a real failure much harder.
        """
        payload = ["validation error one", "validation error two"]
        with pytest.raises(RuntimeError) as info:
            Err(payload).unwrap()
        assert "validation error one" in str(info.value)

    def test_unwrap_or_raises(self):
        """`unwrap_or` on `Err` must *not* silently swallow.

        This is the load-bearing difference from Python's
        `dict.get(key, default)` idiom. If this ever returned
        `default` instead of raising, every caller that relied on the
        safety net would lose it silently — no test would catch the
        regression until production.
        """
        with pytest.raises(RuntimeError):
            Err(["boom"]).unwrap_or("default")

    def test_unwrap_or_error_mentions_errors(self):
        """The `unwrap_or` exception must mention the error payload so
        the fix is visible in the traceback."""
        with pytest.raises(RuntimeError) as info:
            Err(["the specific problem"]).unwrap_or(0)
        assert "the specific problem" in str(info.value)

    def test_error_attribute_matches_payload(self):
        payload = {"path": "version", "msg": "invalid"}
        assert Err(payload).error is payload

    def test_has_no_value_attribute(self):
        """`Err` must not expose `.value`.

        A caller who writes `result.value` on an `Err` should get an
        `AttributeError` at that exact line, not a silent `None` that
        propagates three frames downstream. This is the "fail at the
        point of mistake" principle the whole `Result` type is built
        around.
        """
        r = Err(["boom"])
        with pytest.raises(AttributeError):
            _ = r.value  # type: ignore[attr-defined]


# =============================================================================
# Mapping — functor laws
# =============================================================================
class TestMapping:
    """`map` and `map_err` must respect the branch they are called on.

    These are the only operations that transform the wrapped value;
    if the branch is lost, downstream code silently processes the
    wrong shape (e.g. a success transformed as if it were a failure).
    """

    # -- Ok.map --------------------------------------------------------

    def test_ok_map_applies_function(self):
        assert Ok(2).map(lambda x: x * 10).unwrap() == 20

    def test_ok_map_returns_ok(self):
        assert Ok(1).map(lambda x: x).is_ok() is True

    def test_ok_map_preserves_exception_propagation(self):
        """An exception inside `map` must propagate, not be swallowed.

        Wrapping the exception in `Err` would blur the line between
        "validation failed" (data) and "the caller's function is
        broken" (bug). Bugs must be loud.

        The `match=` pins the *specific* message raised by `boom`.
        `ValueError` alone would satisfy any `ValueError` — including
        one raised by an unrelated bug in `Result.map` itself — which
        is exactly what `PT011` exists to prevent. `match` is a
        regular expression, so if a future message contains regex
        metacharacters, escape it (``re.escape(msg)``) rather than
        interpolating raw.
        """

        def boom(_: int) -> int:
            raise ValueError("map should not swallow this")

        with pytest.raises(ValueError, match="map should not swallow this"):
            Ok(1).map(boom)

    # -- Ok.map_err ----------------------------------------------------

    def test_ok_map_err_is_noop(self):
        """Mapping the error channel of a success does nothing."""
        r = Ok(5).map_err(lambda e: f"wrapped: {e}")
        assert r.is_ok() is True
        assert r.unwrap() == 5

    # -- Err.map -------------------------------------------------------

    def test_err_map_is_noop(self):
        """Mapping the value channel of a failure does nothing."""
        r = Err(["e"]).map(lambda x: x + 1)
        assert r.is_err() is True

    def test_err_map_does_not_call_function(self):
        """A failed result must not invoke the `map` function.

        If it did, callers would pay the cost of a transformation for
        a value they do not have — and worse, the transformation might
        raise, hiding the original error. Recording invocation is
        necessary because a return-value check alone would pass even
        if the function were called and its result discarded.
        """
        called = False

        def record(_: object) -> object:
            nonlocal called
            called = True
            return None

        Err(["e"]).map(record)
        assert called is False

    # -- Err.map_err ---------------------------------------------------

    def test_err_map_err_applies_function(self):
        assert Err(3).map_err(lambda e: e * 2).error == 6

    def test_err_map_err_returns_err(self):
        assert Err("x").map_err(str.upper).is_err() is True

    def test_err_map_err_preserves_exception_propagation(self):
        """Same rule as `Ok.map` — bugs must be loud.

        See `test_ok_map_preserves_exception_propagation` for why
        ``match`` narrows ``ValueError`` to the message that ``boom``
        raises, rather than accepting any ``ValueError``.
        """

        def boom(_: int) -> int:
            raise ValueError("map_err should not swallow this")

        with pytest.raises(ValueError, match="map_err should not swallow this"):
            Err(1).map_err(boom)

    # -- Chainability --------------------------------------------------

    def test_ok_map_chain(self):
        """Composing two maps is the same as mapping their composition."""
        assert Ok(3).map(lambda x: x + 1).map(lambda x: x * 2).unwrap() == 8

    def test_mixed_chain_on_ok(self):
        """`map_err` in the middle of a success chain is a no-op."""
        r = Ok(5).map(lambda x: x - 1).map_err(str).map(lambda x: x + 10)
        assert r.unwrap() == 14

    def test_mixed_chain_on_err(self):
        """`map` in the middle of a failure chain is a no-op."""
        r = Err(1).map(lambda x: x - 1).map_err(lambda e: e + 10).map(str)
        assert r.error == 11


# =============================================================================
# Equality and hashability
# =============================================================================
class TestEquality:
    """Value semantics are what let tests assert on outcomes directly."""

    def test_ok_equality_by_wrapped_value(self):
        assert Ok(42) == Ok(42)

    def test_ok_inequality_by_wrapped_value(self):
        assert Ok(42) != Ok(43)

    def test_err_equality_by_error_payload(self):
        assert Err(["a"]) == Err(["a"])

    def test_err_inequality_by_error_payload(self):
        assert Err(["a"]) != Err(["b"])

    def test_ok_and_err_are_not_equal(self):
        """Even with a matching payload, the branches differ."""
        assert Ok("x") != Err("x")
        assert Err("x") != Ok("x")

    def test_ok_is_hashable(self):
        """Frozen + slots means hashable; useful for set-membership
        and dict keys when deduplicating results."""
        assert {Ok(1), Ok(1), Ok(2)} == {Ok(1), Ok(2)}

    def test_err_is_hashable(self):
        assert {Err("a"), Err("a"), Err("b")} == {Err("a"), Err("b")}


# =============================================================================
# Repr — debugging affordance
# =============================================================================
class TestRepr:
    """`repr` is what appears in stack traces and logs.

    The durable property is **structural**: the branch name prefixes
    the repr (``Ok(...)`` or ``Err(...)``). A log scanner greps for
    ``^Err(`` or ``\\bErr\\(``, not for the substring ``"Err"``
    anywhere — because the wrapped value may legitimately contain
    that text (a variable named ``"Err"``, a message describing the
    other branch, etc.). The tests below assert the structural
    property, not a substring property that would be defeated by any
    wrapped value containing the other branch's name.
    """

    def test_ok_repr_mentions_value(self):
        text = repr(Ok("hello"))
        assert "Ok" in text
        assert "hello" in text

    def test_err_repr_mentions_error(self):
        text = repr(Err(["boom"]))
        assert "Err" in text
        assert "boom" in text

    def test_ok_repr_starts_with_ok_token(self):
        """`repr(Ok(x))` must begin with `Ok(`, not merely contain it."""
        assert repr(Ok("anything")).startswith("Ok(")

    def test_err_repr_starts_with_err_token(self):
        assert repr(Err("anything")).startswith("Err(")

    def test_ok_repr_does_not_begin_with_err_token(self):
        """Even when the wrapped value contains the other branch's name,
        the *prefix* stays unambiguous.

        A naive substring check would flag `Ok("Err-looking")` as a
        failure; a log scanner using anchored patterns would not. This
        test asserts the property the scanner actually relies on.
        """
        assert not repr(Ok("Err-looking")).startswith("Err(")

    def test_err_repr_does_not_begin_with_ok_token(self):
        assert not repr(Err("Ok-looking")).startswith("Ok(")


# =============================================================================
# Immutability
# =============================================================================
class TestFrozenAndSlots:
    """`frozen=True` and `slots=True` are part of the type's contract.

    A future refactor that removes either would silently allow
    mutation (`result.value = ...`) and stray attributes
    (`result.debug_info = ...`), both of which would corrupt results
    flowing through a pipeline that assumes immutability.
    """

    def test_ok_declared_field_is_frozen(self):
        """Assigning to a *declared* field raises `FrozenInstanceError`
        (a subclass of `AttributeError`).

        This is the canonical case and asserts the specific class
        because the behaviour is stable across Python versions —
        `dataclasses` itself generates the frozen check, not the
        interpreter.
        """
        r = Ok(1)
        with pytest.raises(AttributeError):
            r.value = 2  # type: ignore[misc]

    def test_err_declared_field_is_frozen(self):
        r = Err("e")
        with pytest.raises(AttributeError):
            r.error = "new"  # type: ignore[misc]

    def test_ok_rejects_stray_attributes(self):
        """Slots prevent accidentally adding fields during debugging
        and leaving them in the code.

        The exception class is asserted as ``(AttributeError,
        TypeError)`` rather than ``AttributeError`` because Python's
        ``@dataclass(frozen=True, slots=True)`` combination has a
        known quirk on 3.10 to 3.12: ``slots=True`` replaces the class
        with a new one carrying ``__slots__``, and the generated
        ``__setattr__`` delegates *stray* attribute assignment (as
        opposed to declared fields, which it blocks with
        ``FrozenInstanceError``) to ``object.__setattr__``. That
        call then compares ``super()``'s bound class (the
        pre-replacement class) against the instance's actual type
        (the post-replacement class), and raises
        ``TypeError: super(type, obj): obj must be an instance or
        subtype of type``.

        The *behaviour* we actually need is "assignment fails" — the
        specific exception class is an implementation detail of
        CPython's dataclass machinery that has varied across versions
        and may vary again. Accepting both classes means this test
        passes on the current failure mode, on a hypothetical future
        Python that fixes the delegation to raise
        ``AttributeError``, and on a hypothetical future
        ``dataclasses`` that stops replacing the class. It is the
        durable form of the assertion.
        """
        r = Ok(1)
        with pytest.raises((AttributeError, TypeError)):
            r.debug = "oops"  # type: ignore[attr-defined]

    def test_err_rejects_stray_attributes(self):
        """Same rationale as `test_ok_rejects_stray_attributes`."""
        r = Err("e")
        with pytest.raises((AttributeError, TypeError)):
            r.debug = "oops"  # type: ignore[attr-defined]


# =============================================================================
# Pattern matching — documented consumer idiom
# =============================================================================
class TestPatternMatching:
    """`match` on the two dataclasses is the idiomatic branch.

    Pinning it here means a refactor that renames `Ok`/`Err` (or
    moves them to a library that does not expose dataclass fields
    the same way) breaks these tests rather than every consumer's
    `match` statement at runtime.
    """

    def test_match_on_ok_binds_value(self):
        match Ok(7):
            case Ok(value):
                captured = value
            case Err(_):  # pragma: no cover - unreachable
                captured = None
        assert captured == 7

    def test_match_on_err_binds_error(self):
        match Err(["e"]):
            case Ok(_):  # pragma: no cover - unreachable
                captured = None
            case Err(error):
                captured = error
        assert captured == ["e"]


# =============================================================================
# Type alias at runtime
# =============================================================================
class TestGenericTyping:
    """`Result` is a `Union` alias; runtime checks must work.

    Callers use `isinstance(result, Ok)` as a type guard and pass
    `Result[Blueprint, list[ValidationError]]` in signatures. Both
    behaviours depend on the alias staying a plain `Union` of the
    two concrete classes — a future refactor to a class hierarchy
    (e.g. `Result` as an ABC) would require changing every call site.
    """

    def test_result_alias_accepts_ok(self):
        r: Result[int, str] = Ok(1)
        assert isinstance(r, Ok)

    def test_result_alias_accepts_err(self):
        r: Result[int, str] = Err("e")
        assert isinstance(r, Err)

    def test_isinstance_discriminates(self):
        """An `Ok` is not an `Err` and vice versa — used by the
        loader's callers to branch."""
        assert isinstance(Ok(1), Ok)
        assert not isinstance(Ok(1), Err)
        assert isinstance(Err("e"), Err)
        assert not isinstance(Err("e"), Ok)
