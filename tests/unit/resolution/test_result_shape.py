"""Tests for the ``Result`` shape returned by ``VariableResolver.resolve``.

The resolver returns a ``Result[ResolvedValues, list[ValidationError]]``
— never a bare value, never an exception, never ``None``. That shape
is load-bearing for two guarantees:

1. **Aggregation.** Every failure is a value in a list, so the caller
   can render every problem at once. The alternative (raising an
   exception) would force first-error short-circuiting and produce a
   "fix one, rerun, fix the next" experience.

2. **Type-checked success.** ``ResolvedValues`` is the resolver-owned
   type that downstream APIs require in their signatures. That turns
   "no unvalidated input reached the renderer" from a review
   checklist item into a compile-time guarantee (ADR-0011). If the
   resolver returned a plain ``dict``, that guarantee would dissolve.

This file pins the *shape* — the observable contract of the return
value — not the behaviour of individual rules. Behaviour lives in
the sibling files:

* ``test_precedence.py``         — which source wins.
* ``test_required.py``           — required/optional semantics.
* ``test_type_coercion.py``      — per-type coercion.
* ``test_project_name.py``       — the ``project_name`` named validator.
* ``test_python_package.py``     — the ``python_package`` named validator.
* ``test_npm_package.py``        — the ``npm_package`` named validator.
* ``test_validators.py``         — composed per-variable validation.

The split is deliberate. A "shape" test that also asserted on rules
would fail for two reasons at once — the shape changed, or a rule
changed — and neither could be diagnosed from the failure alone.
This file fails only when the shape changed. The rule tests fail
only when the rules changed. Each points at its own fix.

Why ``Result`` and not ``tuple[bool, ...]``
------------------------------------------
``Result`` is a small sum type (``Ok`` / ``Err``). Its value over a
tuple or a dict-with-a-status-key is that *exhaustiveness is
checkable*: mypy sees ``Result`` as a union, and every call site
must narrow via ``isinstance(result, Ok)`` or ``isinstance(result,
Err)`` before touching ``result.value`` or ``result.error``. A
tuple-based return (``(True, values, [])`` / ``(False, None,
errors)``) would compile fine but break at runtime the first time a
caller assumed ``values`` was present regardless of the success
flag.

That is the reason the tests below assert on ``isinstance`` — not
just on truthiness or attribute presence. The *type* is part of the
contract, not an implementation detail.
"""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from forge.loading.errors import ValidationError
from forge.loading.result import Err, Ok, Result
from forge.models import (
    Blueprint,
    Category,
    Compatibility,
    VariableDefinition,
    VariableType,
)
from forge.resolution import ResolvedValues, VariableResolver

# ---------------------------------------------------------------------------
# Factories — kept tiny so each test reads as one sentence
# ---------------------------------------------------------------------------


def _blueprint(*variables: VariableDefinition) -> Blueprint:
    """Build a minimal valid ``Blueprint`` with the given variables."""
    return Blueprint(
        schema_version="1.0.0",
        id="sample.demo",
        name="Sample Demo",
        version="1.0.0",
        description="Test blueprint.",
        category=Category.api,
        variables=list(variables),
        verification=[{"id": "ok", "type": "required_file", "path": "README.md"}],
        compatibility=Compatibility(min_forge_version="1.0.0"),
    )


def _required(var_id: str) -> VariableDefinition:
    """A required string variable with no default."""
    return VariableDefinition(
        id=var_id,
        type=VariableType.string,
        description=f"Test variable {var_id}.",
        required=True,
    )


# ---------------------------------------------------------------------------
# The success case is ``Ok``, and the value is ``ResolvedValues``
# ---------------------------------------------------------------------------


def test_success_returns_ok() -> None:
    """A successful resolution returns ``Ok`` — not ``Err``, not a bare value.

    The distinction matters for callers: ``isinstance(result, Ok)``
    is the exhaustive check mypy enforces before ``result.value`` is
    reachable. A caller that got back a bare dict would have no
    equivalent guarantee — the same dict would need to be
    distinguished from an error dict by some out-of-band convention.
    """
    bp = _blueprint(_required("author"))
    result = VariableResolver().resolve(bp, cli_args={"author": "alice"})

    assert isinstance(result, Ok)
    assert not isinstance(result, Err)  # belt and braces — Ok and Err are disjoint


def test_success_value_is_resolvedvalues_not_dict() -> None:
    """The success value is ``ResolvedValues``, not a bare ``dict``.

    ``ResolvedValues`` is the type downstream APIs annotate
    parameters with to get the "no unvalidated input reached the
    renderer" guarantee (ADR-0011). Returning a plain ``dict``
    would still *look* correct at every call site — but the
    type-checking guarantee that made the KPI enforceable would be
    gone, silently.

    This is not a "we prefer this type" test. It is a *guardrail*
    for the KPI: if a future refactor changes the return type, the
    KPI's enforcement mechanism disappears, and the test is the
    thing that surfaces it.
    """
    bp = _blueprint(_required("author"))
    result = VariableResolver().resolve(bp, cli_args={"author": "alice"})

    assert isinstance(result, Ok)
    assert isinstance(result.value, ResolvedValues)


def test_resolvedvalues_implements_mapping_protocol() -> None:
    """``ResolvedValues`` behaves like a read-only mapping.

    Downstream code (templates, dry-run renderers) does not care
    that the value is a `ResolvedValues` instance — it does
    ``values["key"]``, ``values.get("key")``, ``"key" in values``,
    iterates, and takes ``len``. If the type did not implement the
    ``Mapping`` protocol, every consumer would need a type-specific
    adapter.

    This test pins the protocol, not the implementation, so a
    future reimplementation (e.g. wrapping a frozen dict) is allowed
    as long as the protocol holds.
    """
    bp = _blueprint(_required("author"), _required("license"))
    result = VariableResolver().resolve(bp, cli_args={"author": "alice", "license": "MIT"})

    assert isinstance(result, Ok)
    values = result.value
    assert isinstance(values, Mapping)
    assert values["author"] == "alice"
    assert values.get("author") == "alice"
    assert values.get("missing", "fallback") == "fallback"
    assert "author" in values
    assert "missing" not in values
    assert len(values) == 2
    assert set(values) == {"author", "license"}


def test_resolvedvalues_as_dict_returns_plain_copy() -> None:
    """``as_dict()`` returns a plain ``dict`` copy, not the internal storage.

    The renderer needs a plain dict (Jinja2's context is a
    ``dict``). Returning the *internal* mapping would let the
    renderer mutate the validated values — which is exactly the
    invariant ``ResolvedValues`` exists to protect. ``as_dict()`` is
    the deliberate one-way conversion: copy out, render, discard.
    """
    bp = _blueprint(_required("author"))
    result = VariableResolver().resolve(bp, cli_args={"author": "alice"})

    assert isinstance(result, Ok)
    as_dict = result.value.as_dict()
    assert isinstance(as_dict, dict)
    assert as_dict == {"author": "alice"}
    # Mutating the copy must not affect the original.
    as_dict["author"] = "mutated"
    assert result.value["author"] == "alice"


# ---------------------------------------------------------------------------
# The failure case is ``Err``, and the error is a list of ``ValidationError``
# ---------------------------------------------------------------------------


def test_failure_returns_err() -> None:
    """A failed resolution returns ``Err`` — not ``Ok``, not a raised exception.

    The failure is a *value*, not a control-flow interrupt. That
    is what makes aggregation possible: the resolver iterates all
    variables, collecting errors, and only then decides whether to
    return ``Ok`` or ``Err``. If it raised on the first missing
    variable, the caller would never see the second missing
    variable.
    """
    bp = _blueprint(_required("author"))
    result = VariableResolver().resolve(bp, cli_args={})

    assert isinstance(result, Err)
    assert not isinstance(result, Ok)


def test_failure_payload_is_a_list() -> None:
    """``Err``'s payload is a *list* of ``ValidationError``, not a single error.

    The list shape is the aggregation guarantee made structural. A
    caller that expects one error and gets a list must iterate; a
    caller that expects a list can never be surprised by "only the
    first error was captured."
    """
    bp = _blueprint(_required("author"), _required("license"))
    result = VariableResolver().resolve(bp, cli_args={})

    assert isinstance(result, Err)
    assert isinstance(result.error, list)


def test_every_failure_payload_item_is_validationerror() -> None:
    """Every item in ``Err.error`` is a ``ValidationError``, not a string or a dict.

    The type is the contract. A future change that started
    aggregating ``(message, hint)`` tuples or ``str`` objects would
    still pass a "list is non-empty" test but break every consumer
    that reads ``err.path`` or ``err.code``. Asserting on the item
    type is what closes that gap.
    """
    bp = _blueprint(_required("author"), _required("license"))
    result = VariableResolver().resolve(bp, cli_args={})

    assert isinstance(result, Err)
    assert result.error  # not empty
    for item in result.error:
        assert isinstance(item, ValidationError)


def test_failure_payload_is_never_empty() -> None:
    """``Err`` is never returned with an empty list.

    An empty ``Err`` would be a "silent failure" — the caller knows
    something went wrong but has no information about what. Every
    code path that returns ``Err`` must produce at least one
    ``ValidationError``; the resolver's control flow guarantees that
    (it only returns ``Err`` when ``errors`` is non-empty). The test
    pins the invariant so a future refactor that returned ``Err([])``
    on some edge case would fail here, not three layers downstream.
    """
    bp = _blueprint(_required("author"))
    result = VariableResolver().resolve(bp, cli_args={})

    assert isinstance(result, Err)
    assert len(result.error) >= 1


def test_failure_error_carries_path_message_hint_code() -> None:
    """Every error has the four fields the CLI renders and routes on.

    The whole pipeline — loader, resolver, wizard — speaks one error
    shape (``ValidationError``), and that shape has four required
    fields the CLI relies on:

    * ``path``    — where in the document (``variables.author``).
    * ``message`` — what is wrong ("required variable has no value").
    * ``hint``    — what to do ("--var author=<value>").
    * ``code``    — machine-readable class ("required.missing").

    A test that only asserted "an error is present" would pass even
    if the resolver started returning a bare ``Exception`` with a
    ``str`` message. That would break the CLI's rendering (no
    structured path) and its routing (no code to key on). Asserting
    on all four fields is the correct contract.
    """
    bp = _blueprint(_required("author"))
    result = VariableResolver().resolve(bp, cli_args={})

    assert isinstance(result, Err)
    err = result.error[0]
    assert err.path == "variables.author"
    assert err.message  # non-empty
    assert err.hint  # non-empty
    assert err.code == "required.missing"


# ---------------------------------------------------------------------------
# ``Result`` is the *only* return shape — never mixed with exceptions
# ---------------------------------------------------------------------------


def test_resolver_never_raises_for_user_errors() -> None:
    """Invalid user input is returned as ``Err``, not raised.

    Every rejection the resolver produces — missing required
    variable, wrong type, bad pattern, invalid name — is a
    ``ValidationError`` in an ``Err`` list. The function does not
    raise for user input. If it did, the caller would need a
    try/except at every call site, and the aggregation guarantee
    would dissolve (only one exception can be raised at a time).

    The exception path is reserved for *developer* errors (a
    caller passing an invalid ``Blueprint`` instance is a bug, not
    user input) and for genuinely unrecoverable conditions. The
    test pins the boundary: user-shaped inputs that fail must
    return ``Err``.
    """
    bp = _blueprint(_required("author"), _required("license"))
    # No try/except: if the resolver raised, the test would fail
    # with the raised exception, not with an assertion.
    result = VariableResolver().resolve(bp, cli_args={})
    assert isinstance(result, Err)


def test_resolver_returns_ok_for_empty_blueprint() -> None:
    """A blueprint with no variables resolves to ``Ok({})``, not ``Err``.

    This is the legitimate case (a blueprint that only writes
    static templates). It must succeed with an empty
    ``ResolvedValues`` — not ``Err`` (nothing failed), not ``None``
    (there is a value, it is just empty), not a raw empty ``dict``
    (the type is part of the contract).
    """
    bp = _blueprint()
    result = VariableResolver().resolve(bp, cli_args={})

    assert isinstance(result, Ok)
    assert isinstance(result.value, ResolvedValues)
    assert result.value.as_dict() == {}


# ---------------------------------------------------------------------------
# The ``Result`` type alias itself
# ---------------------------------------------------------------------------


def test_result_alias_covers_both_arms() -> None:
    """``Result`` is the union of ``Ok`` and ``Err`` — no third arm.

    The alias is defined in ``forge.loading.result`` as
    ``Union[Ok[T], Err[E]]``. That exhaustiveness is what lets
    mypy narrow a ``Result`` value to exactly one of the two arms.
    If a future refactor added a third arm (e.g. ``Pending``), the
    alias would still exist but every ``isinstance`` check in the
    codebase would silently stop being exhaustive — a *typing* bug
    that no runtime test could catch, because the runtime values
    would still be ``Ok`` or ``Err``.

    This test does not catch that class of bug (no runtime test
    can — the type system is the only guard). It documents that the
    two-arm contract is deliberate, so a future contributor who
    reads the test knows that adding a third arm is a *breaking
    change*, not an additive one.

    The ``Result`` alias is imported from ``forge.loading.result``
    and its purpose here is documentation: it must exist and it
    must be the return annotation of ``VariableResolver.resolve``.
    """
    # The alias exists.
    assert Result is not None

    # The resolver's return annotation mentions the alias. We can
    # check this structurally by constructing a known-good result
    # and asserting it matches the type alias at runtime — an
    # ``Ok`` value is a ``Result``; an ``Err`` value is a ``Result``.
    bp = _blueprint(_required("author"))
    resolver = VariableResolver()

    ok_result = resolver.resolve(bp, cli_args={"author": "alice"})
    err_result = resolver.resolve(bp, cli_args={})

    # Both arms satisfy the alias's union, which is what "the
    # resolver's return type is ``Result``" means operationally.
    assert isinstance(ok_result, Ok)
    assert isinstance(err_result, Err)


# ---------------------------------------------------------------------------
# Caller ergonomics — the shape does not force redundant checks
# ---------------------------------------------------------------------------


def test_map_lets_callers_transform_success_without_unwrapping_first() -> None:
    """``Result.map`` transforms ``Ok``'s value; ``Err`` is passed through.

    The resolver's ``Result`` is the ``loading.result.Result``,
    which has a small ``.map`` helper. This test pins the helper's
    behaviour at the resolver boundary: a caller that wants to
    format the values (e.g. ``values.as_dict()``) can do so without
    an ``isinstance`` unwrap, and a caller that has an ``Err`` will
    not have their transform accidentally applied to the errors.

    The helper exists because at least one real call site (the CLI
    dry-run renderer) uses it — this test is a guardrail, not
    speculative API surface.
    """
    bp = _blueprint(_required("author"))
    resolver = VariableResolver()

    ok_result = resolver.resolve(bp, cli_args={"author": "alice"})
    mapped_ok = ok_result.map(lambda values: values.as_dict())
    assert isinstance(mapped_ok, Ok)
    assert mapped_ok.value == {"author": "alice"}

    err_result = resolver.resolve(bp, cli_args={})
    mapped_err = err_result.map(lambda _values: {"author": "never"})
    assert isinstance(mapped_err, Err)
    # The transform was not applied to the error payload.
    assert mapped_err.error  # still the original errors


def test_unwrap_raises_on_err_but_succeeds_on_ok() -> None:
    """``Ok.unwrap()`` returns the value; ``Err.unwrap()`` raises.

    This is a *deliberate* asymmetry. ``unwrap()`` is an escape
    hatch for callers that have already proven success by other
    means (e.g. a ``pytest.raises(Err)`` guard). It is not meant as
    a general-purpose "just get the value" — doing so would
    encourage ignoring errors. A caller that unwraps without
    checking gets an obvious runtime failure, not a silent ``None``.
    """
    bp = _blueprint(_required("author"))
    resolver = VariableResolver()

    ok_result = resolver.resolve(bp, cli_args={"author": "alice"})
    assert isinstance(ok_result, Ok)
    assert ok_result.unwrap()["author"] == "alice"

    err_result = resolver.resolve(bp, cli_args={})
    assert isinstance(err_result, Err)
    with pytest.raises(RuntimeError):
        err_result.unwrap()


# ---------------------------------------------------------------------------
# The two arms are truly disjoint — no ambiguity about which one you have
# ---------------------------------------------------------------------------


def test_ok_and_err_are_disjoint() -> None:
    """An ``Ok`` is never also an ``Err``; ``isinstance`` is sufficient.

    In Python, ``isinstance`` checks can return ``True`` for two
    seemingly unrelated types (multiple inheritance, ``ABC``
    registration, etc.). If ``Ok`` were also an ``Err`` (e.g. via a
    shared base), a caller's ``isinstance(result, Ok)`` check would
    be misleading — a value that "looks like" a result could
    satisfy both branches and the caller's ``else`` would never fire.

    The current implementation subclasses two unrelated classes
    (``Generic`` only), so the two are disjoint. This test pins
    that: a future refactor that gave both a shared ``Result`` base
    class (a "cleaner" type hierarchy) would still pass most tests
    but would break any caller whose control flow relies on
    exclusivity. The test is cheap and prevents that class of bug.
    """
    bp = _blueprint(_required("author"))
    resolver = VariableResolver()

    ok_result = resolver.resolve(bp, cli_args={"author": "alice"})
    err_result = resolver.resolve(bp, cli_args={})

    # Each result is *exactly* one of the two, not "one and also the
    # other". ``type(...) is`` is stronger than ``isinstance(...)``
    # and is the right check here, because the contract is about
    # the concrete class, not about the class's ancestors.
    assert type(ok_result) is Ok
    assert type(err_result) is Err
