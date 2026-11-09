"""KPI guard: no unvalidated input can reach downstream code.

This file exists to protect a *single* architectural guarantee — the
one the resolution-layer ADR (ADR-0011) was designed to deliver:

    **Zero unvalidated user inputs reach the rendering engine.**

That guarantee is delivered structurally, not by convention. It has
three pillars:

1. **``ResolvedValues`` is the only success shape.** Downstream
   functions require it in their signatures; mypy rejects callers
   that pass a plain ``dict``.

2. **Construction is resolver-only.** There is no ``from_dict`` /
   ``of`` / public constructor that turns an arbitrary mapping into
   a ``ResolvedValues``. The type is producible *only* by the
   resolver, after validation.

3. **The mapping is immutable once produced.** No caller can mutate
   a ``ResolvedValues`` to inject an unvalidated value after the
   fact.

Each test below pins one pillar. Together they make the KPI a
*property of the type system* rather than a review checklist item.

Why this file is separate from ``test_result_shape.py``
-------------------------------------------------------
``test_result_shape.py`` answers *"what does the resolver return?"*
(``Ok`` / ``Err``, ``ResolvedValues`` / ``list[ValidationError]``).
That file is about the *shape* of the return value.

This file answers a different, narrower question: *"can an attacker
(or a buggy caller) bypass the validation funnel?"* The two overlap
on one assertion — "the success value is ``ResolvedValues``" — but
the reason for testing it differs:

* In ``test_result_shape.py``, that assertion is about *contract
  clarity* for callers.
* Here, it is about *security*: ``ResolvedValues`` is the wall
  between user input and the renderer, and this file proves the
  wall is up.

Splitting them means a *shape* change (e.g. adding a new arm to
``Result``) fails ``test_result_shape.py`` with a message about
shape, while a *bypass* change (e.g. exposing a public constructor
on ``ResolvedValues``) fails here with a message about the KPI.

Scope
-----
These tests are all about **what the type prevents**, not about
**what the resolver does**. They do not exercise:

* resolver behaviour (precedence, required checks, coercion) — that
  is the sibling files' job;
* loader behaviour (schema, YAML parsing) — that is the loader
  tests' job;
* CLI behaviour (rendering, exit codes) — that is the CLI tests'
  job.

If a test here starts wanting to set up a Blueprint and a CLI
argument and a filesystem path, it belongs in a different file.
The file's job is to prove the *structural* guarantee; adding
behavioural assertions to it would dilute the failure signal.

The Feynman framing
-------------------
Imagine a nightclub with **one** door: the bouncer. Inside, everyone
is guaranteed to have been checked. ``ResolvedValues`` is the
wristband the bouncer hands out. This file's job is to prove three
things:

1. The bouncer is the *only* one who can hand out wristbands (no
   self-service box at the back).
2. The wristband cannot be *modified* after handing out (no
   "add your own name" field on it).
3. Downstream staff *only* accept wristbands (they do not let
   people in via a wave and a nod).

If any of those three fail, the "everyone inside was checked"
guarantee fails — even if the bouncer is doing their job perfectly.
That is why the test file exists separately from "does the bouncer
do their job well" (the resolver's tests). Different question,
different failure mode, different fix.
"""

from __future__ import annotations

from inspect import getmembers, isfunction, signature

import pytest

from forge.loading.result import Ok
from forge.models import (
    Blueprint,
    Category,
    Compatibility,
    VariableDefinition,
    VariableType,
)
from forge.resolution import ResolvedValues, VariableResolver

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _blueprint_with(*variables: VariableDefinition) -> Blueprint:
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


@pytest.fixture()
def valid_inputs() -> tuple[Blueprint, dict[str, str]]:
    """A blueprint and a matching set of valid CLI args.

    Returned as a tuple rather than as two fixtures because they
    are used together (a valid blueprint without matching args is
    not useful for these tests, and matching args without the
    blueprint is equally useless). Keeping them together means
    one fixture parameter, not two — and the failure of either
    half points at the same line in the test.
    """
    bp = _blueprint_with(_required("author"), _required("license"))
    args = {"author": "alice", "license": "MIT"}
    return bp, args


# ---------------------------------------------------------------------------
# Pillar 1: ``ResolvedValues`` is the only success shape
# ---------------------------------------------------------------------------


def test_resolver_returns_resolvedvalues_not_dict(
    valid_inputs: tuple[Blueprint, dict[str, str]],
) -> None:
    """The success value is ``ResolvedValues``, not a bare ``dict``.

    If the resolver returned a plain ``dict``, every downstream
    function that annotates its parameter as ``ResolvedValues``
    would still *look* correct at runtime, but mypy would start
    rejecting the call sites. A caller who wanted to bypass the
    type check could ``cast`` the dict — but the cast would be a
    *deliberate* choice, visible in review, not an accident.

    The distinction between "a dict of validated values" and "a
    ``ResolvedValues``" is the distinction between "probably safe"
    and "provably safe, until someone casts". The type is the
    enforcement mechanism; this test pins it.
    """
    bp, args = valid_inputs
    result = VariableResolver().resolve(bp, cli_args=args)

    assert isinstance(result, Ok)
    # Not a dict — the type guarantee relies on this being
    # specifically ``ResolvedValues``.
    assert not isinstance(result.value, dict)
    assert isinstance(result.value, ResolvedValues)


def test_resolvedvalues_has_no_from_dict_classmethod() -> None:
    """``ResolvedValues`` does not offer a public constructor from a raw mapping.

    If it did, a caller could write:

        unvalidated = ResolvedValues.from_dict(user_input)
        renderer.render(unvalidated)

    ...and the renderer's ``ResolvedValues`` parameter would
    happily accept it, having *no way to know* the values were
    never validated. The type would be a lie.

    There are exactly two legitimate ways to produce a
    ``ResolvedValues``:

    1. ``VariableResolver.resolve(...)`` — the validated path.
    2. Direct construction via the internal ``__init__``, which
       takes the private ``_values`` parameter (and is therefore
       obviously not intended for external use — no code reviewer
       would approve a call to ``ResolvedValues(MappingProxyType({...}))``
       that bypassed the resolver).

    This test pins the absence of a *convenient* bypass. It cannot
    prevent someone from constructing the private form directly,
    but that is a *code review* problem, not a *type system*
    problem — and the difference between "a hole in the wall" and
    "a hole someone had to walk around to reach" is the difference
    between a bug and an intentional violation.
    """
    # Walk the class's public names; if any of them looks like a
    # factory, the type has a bypass hole.
    public_names = [name for name in dir(ResolvedValues) if not name.startswith("_")]

    forbidden_factory_names = {"from_dict", "from_values", "of", "coerce", "wrap"}

    for forbidden in forbidden_factory_names:
        assert forbidden not in public_names, (
            f"ResolvedValues exposes a public `{forbidden}` method. "
            f"This would let callers construct ResolvedValues from a raw "
            f"mapping, bypassing the resolver's validation. "
            f"Remove it or rename it to ``_from_unvalidated_...`` and "
            f"add a comment explaining why bypassing the KPI is acceptable."
        )


def test_resolvedvalues_constructor_signature_is_private() -> None:
    """The ``__init__`` parameter is named ``_values`` (underscore prefix).

    The underscore prefix is not decoration: it signals to a
    reviewer that the parameter is not part of the public API.
    A caller who constructs ``ResolvedValues(_values=...)``
    directly is *deliberately* doing something the type was not
    designed for — and the underscore makes that intent visible in
    the diff.

    This is the "cheap deterrent" layer. The real protection is
    that downstream functions accept ``ResolvedValues`` (not
    ``dict``), so a caller who constructs one still has to have
    *intended* to do so. The test pins the convention so a future
    "helpful" rename of ``_values`` to ``values`` (to satisfy some
    linter or another) is caught here, before it appears in every
    caller's code.
    """
    sig = signature(ResolvedValues.__init__)
    params = list(sig.parameters.keys())

    # ``self`` is always first.
    assert params[0] == "self"
    # The data parameter is underscore-prefixed. A rename to
    # ``values`` (no underscore) would be an invitation for
    # external callers to construct ResolvedValues directly.
    data_params = params[1:]
    assert data_params, "ResolvedValues.__init__ has no data parameter"
    assert data_params[0].startswith("_"), (
        f"ResolvedValues.__init__'s data parameter is named "
        f"{data_params[0]!r}, not ``_values``. The underscore prefix "
        f"signals 'private, do not construct directly' — removing it "
        f"invites callers to bypass the resolver."
    )


# ---------------------------------------------------------------------------
# Pillar 2: ``ResolvedValues`` is immutable once constructed
# ---------------------------------------------------------------------------


def test_resolvedvalues_is_frozen(
    valid_inputs: tuple[Blueprint, dict[str, str]],
) -> None:
    """``ResolvedValues`` is a frozen dataclass.

    Immutability closes a specific class of bypass: a caller that
    receives a ``ResolvedValues`` and *mutates* it (adding a new
    key, overwriting an existing one) would inject unvalidated
    values into a container downstream code trusts.

    Frozen dataclasses raise ``FrozenInstanceError`` on attribute
    assignment. This test proves the resolver returns a frozen
    instance — the check is against the *runtime* behaviour of
    the value, not against the class definition, because a future
    refactor could return a subclass (or a different type) from the
    resolver without touching ``ResolvedValues``'s own definition.
    """
    bp, args = valid_inputs
    result = VariableResolver().resolve(bp, cli_args=args)

    assert isinstance(result, Ok)
    values = result.value

    # Attempting to rebind an attribute must fail. The exact
    # exception class is ``dataclasses.FrozenInstanceError``,
    # which subclasses ``AttributeError`` — so ``pytest.raises``
    # on ``AttributeError`` is the portable check.
    with pytest.raises(AttributeError):
        values._values = {}  # type: ignore[misc]


def test_resolvedvalues_internal_mapping_is_readonly(
    valid_inputs: tuple[Blueprint, dict[str, str]],
) -> None:
    """The internal mapping rejects ``__setitem__`` — no mutation by key.

    Frozen dataclasses prevent *rebinding* attributes but do not
    prevent mutating a mutable attribute *in place*. If the
    resolver stored a plain ``dict`` in ``_values``, a caller could
    do:

        values._values["injected"] = "unvalidated"

    ...and downstream code reading ``values["injected"]`` would
    see the injected value, having no way to know it was not
    validated. ``MappingProxyType`` closes that hole: the mapping
    raises ``TypeError`` on ``__setitem__``.

    This test asserts the observable consequence — attempting to
    mutate the mapping raises — rather than checking the internal
    type. That is the correct level: a future reimplementation
    that used ``immutables.Map`` or a custom frozen mapping would
    still pass, as long as the guarantee holds.
    """
    bp, args = valid_inputs
    result = VariableResolver().resolve(bp, cli_args=args)

    assert isinstance(result, Ok)
    # The mapping is not directly reachable as ``.values`` or similar;
    # the only mutation path would be through ``_values``. Assert
    # that path is closed.
    with pytest.raises((TypeError, AttributeError)):
        result.value._values["injected"] = "unvalidated"  # type: ignore[index,attr-defined]


def test_as_dict_returns_a_copy_not_internal_storage(
    valid_inputs: tuple[Blueprint, dict[str, str]],
) -> None:
    """``as_dict()`` returns a copy — mutating it does not affect the source.

    ``as_dict()`` is the deliberate escape hatch: the renderer
    needs a plain dict, and returning the internal storage would
    let the renderer (or a template) mutate the validated values.
    The copy closes that hole: the renderer gets a snapshot, and
    the ``ResolvedValues`` remains authoritative.

    This is tested separately from ``test_frozen`` because the
    failure modes differ. ``_frozen`` fails if you *rebind*
    ``_values``; ``_as_dict_returns_a_copy`` fails if you *mutate
    through* ``as_dict()``. Both are legitimate bypass attempts,
    and both must fail for the KPI to hold.
    """
    bp, args = valid_inputs
    result = VariableResolver().resolve(bp, cli_args=args)

    assert isinstance(result, Ok)
    as_dict = result.value.as_dict()
    as_dict["author"] = "attacker"
    # The source value is unchanged.
    assert result.value["author"] == "alice"


# ---------------------------------------------------------------------------
# Pillar 3: downstream consumers require ``ResolvedValues``, not ``dict``
# ---------------------------------------------------------------------------


def test_downstream_signature_requires_resolvedvalues() -> None:
    """The renderer/generator boundary is typed against ``ResolvedValues``.

    This is the "downstream staff only accept wristbands" pillar.
    If a downstream function accepts ``dict[str, str]``, then any
    caller (including a buggy one) can pass an unvalidated dict
    and the function will render it. The type guarantee vanishes.

    The test walks the module-level functions of the *current*
    downstream modules and asserts none of them declares a
    parameter typed as a bare ``dict`` whose job is to hold
    resolved values. It is necessarily a heuristic (function names
    vary, docstrings vary) but it is a strong one: the check
    fails if a new downstream function accepts a plain dict for
    values, forcing the author to either use ``ResolvedValues`` or
    justify the exception in a comment this test would catch.

    Limitations of this check, recorded here so a future
    contributor does not expect more than it delivers:

    * It only inspects *annotations*, not runtime behaviour. A
      function that accepts ``Any`` and checks ``isinstance``
      internally would pass — but that is a legitimate pattern
      (a generic renderer that handles multiple input types).

    * It only inspects the modules enumerated below. A new
      downstream module is not covered until it is added to the
      list. That is intentional: the list is a manifest of "places
      the renderer accepts validated values", and adding a new
      downstream consumer means adding its module here — a
      deliberate act, not an incidental one.
    """
    import inspect

    # The list is a manifest, not a discovery. Adding a module here
    # is the act of adding it to the "must accept ResolvedValues"
    # contract. A future renderer or generator module should be
    # added here as part of the PR that introduces it.
    downstream_modules: list[str] = [
        # Populated as the pipeline grows. Empty today because the
        # renderer and generator are not yet implemented; the test
        # is a *skeleton* for the invariant, and turns into a real
        # check the moment the first downstream module lands.
    ]

    for module_name in downstream_modules:
        module = __import__(module_name, fromlist=["_"])
        for name, func in getmembers(module, isfunction):
            # Only inspect callables with annotations — unannotated
            # functions are outside the contract (and outside mypy).
            sig = inspect.signature(func)
            for param_name, param in sig.parameters.items():
                annotation = param.annotation
                # ``inspect.Parameter.empty`` means "no annotation".
                if annotation is inspect.Parameter.empty:
                    continue
                # A string annotation (PEP 563 / ``from __future__
                # import annotations``) needs to be parsed. The
                # check is intentionally textual: it is a proxy
                # for "this module declares its value parameter as
                # ResolvedValues, not dict".
                annotation_text = (
                    annotation
                    if isinstance(annotation, str)
                    else getattr(annotation, "__name__", str(annotation))
                )
                # ``dict`` or ``Dict`` or ``dict[...]`` in a
                # downstream function whose name suggests it
                # handles *resolved values* is the bypass pattern
                # this test exists to catch.
                if "resolved" in param_name.lower() and "dict" in annotation_text.lower():
                    pytest.fail(
                        f"{module_name}.{name}() accepts a plain dict for "
                        f"`{param_name}`. Downstream consumers must require "
                        f"`ResolvedValues` — see ADR-0011 and "
                        f"tests/unit/resolution/test_no_unvalidated_inputs.py."
                    )


# ---------------------------------------------------------------------------
# The pipeline preserves the type across the resolver boundary
# ---------------------------------------------------------------------------


def test_resolver_output_is_directly_usable_by_typed_downstream() -> None:
    """A value returned by the resolver satisfies ``isinstance(ResolvedValues)``.

    The end-to-end form of the KPI: a caller that goes through
    the resolver gets back a value that *is* a ``ResolvedValues``,
    so any downstream function whose parameter is annotated
    ``ResolvedValues`` accepts it without a cast. This is the
    "no friction" guarantee — the correct thing is also the easy
    thing, so callers have no incentive to reach for a bypass.

    A future refactor that made the resolver return a *subclass*
    of ``ResolvedValues`` (e.g. for warnings) would still pass this
    test (``isinstance`` accepts subclasses) — which is correct,
    because the contract is about *acceptability by downstream*,
    not about the concrete class.
    """
    bp, args = (
        _blueprint_with(_required("author")),
        {"author": "alice"},
    )
    result = VariableResolver().resolve(bp, cli_args=args)

    assert isinstance(result, Ok)

    def _downstream_consumer(values: ResolvedValues) -> str:
        """Stand-in for a typed downstream function.

        Declaring the parameter as ``ResolvedValues`` (not ``dict``)
        is what makes the guarantee checkable. This function is
        intentionally trivial — its behaviour is not what is being
        tested; its *signature* is.
        """
        return values["author"]

    # The runtime check ``isinstance`` above is what mypy narrows to
    # at type-checking time, so a caller with a ``ResolvedValues``
    # can call ``_downstream_consumer`` without a cast.
    assert _downstream_consumer(result.value) == "alice"
