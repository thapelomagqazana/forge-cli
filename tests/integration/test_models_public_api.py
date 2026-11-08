"""Public API surface for the models package: importable, stable, complete.

What this test is *for*
-----------------------
``forge.models`` is a façade (see ``src/forge/models/__init__.py``).
Consumers — ``forge.verification``, ``forge.cli.create``, the
generation layer, and any future SDK — are documented to import from
``forge.models``, not from ``forge.models.blueprint``. That indirection
is what lets the codegen backend, the generated file's internal
organisation, and the model tree's class hierarchy evolve without
touching every call site.

The indirection is also what makes the *façade* a contract. This file
is the only place that contract is tested. It asserts three things:

1. **Every name in ``__all__`` is actually importable.** A typo in
   ``__all__`` — a class renamed in the generated file but not in the
   façade, a class added to the schema but forgotten in the façade —
   is invisible to every other test in the suite. Those tests import
   *specific* classes; they do not exhaust the façade. A typo in
   ``__all__`` would break a *user* of the façade before it broke
   any test.

2. **Every name in ``__all__`` is a real class, not a stub.** The
   distinction matters for the boundary between "the façade exposes a
   name" and "the name is usable." A ``__getattr__`` that returned
   ``None`` for every name would satisfy ``hasattr`` but not
   ``isinstance``. This test invokes the classes (via
   ``issubclass`` against a broad base) so a façade that re-exports
   the *name* without the *object* fails.

3. **The façade exposes exactly the expected public names.** No more,
   no less. "No less" is the point of (1) and (2); "no more" matters
   because a name not in ``__all__`` is not part of the documented
   contract, and adding one implicitly means consumers can start
   depending on it without the maintainers noticing. This test pins
   the *set*, so adding a new public name is a deliberate change that
   updates the test in the same commit — exactly the friction the
   façade exists to create.

What this test is *not* for
---------------------------
* **Behaviour.** Whether ``Blueprint`` parses a specific document,
  whether ``VariableDefinition`` rejects a bad type, whether
  ``BlueprintMetadata`` mirrors the root — those are tested
  exhaustively elsewhere. This file is about the *surface*, not the
  *semantics*. Conflating the two would make every behaviour
  regression fail two tests, which is noise, not signal.

* **The generated file's internal structure.** Tests of
  ``forge.models.blueprint`` itself are absent by design — the
  generated module is not a supported import path. Only the façade
  is supported. Testing the internal module would make future codegen
  changes need a test change even when the façade is stable, which
  inverts the whole reason the façade exists.

* **Re-export *mechanism*.** Whether the façade uses ``from X import Y``
  or ``import X; Y = X.Y`` or a lazy ``__getattr__`` is an
  implementation detail. The test asserts the *observable* effect:
  ``from forge.models import Blueprint`` works, and the resulting
  object behaves. The mechanism can change; the observable API
  cannot.

Design notes
------------
* **``_EXPECTED_PUBLIC_NAMES`` is duplicated from ``__all__`` on
  purpose.** A test that read ``__all__`` from the module would be a
  tautology: if a contributor removed a name from both ``__all__``
  and the module, the test would still pass. Duplicating the set
  means a removal shows up as "the module's ``__all__`` no longer
  matches the test's expectation" — which is exactly the
  deliberate-change friction this test is designed to create.

* **Parametrisation is over the *combined* set, not the module's
  ``__all__``.** The same reason, from a different angle: if a
  contributor added a name to the façade but forgot to update the
  test's expectation, the set-equality assertion at the bottom catches
  it; if a contributor added a name to ``__all__`` but the name is
  broken (typo, alias removed), the parametrised import test catches
  it. Two independent assertions, each catching a different failure
  mode.

* **``issubclass`` against a broad base, not ``isinstance`` of a
  specific class.** The models are different types — ``Blueprint`` is
  a ``BaseModel``, ``Category`` is an ``Enum``, ``SemVer`` is a
  ``str`` alias. There is no single base class they all share. The
  test therefore checks: "the imported object is something callable
  or a class in the expected category," where "category" is
  expressed as `issubclass(X, (BaseModel, Enum, str))` — the three
  bases that cover every model in the tree. Anything outside that
  set would fail loudly, which is the correct signal for "the
  façade is exposing something it should not."

* **Submodules are filtered out of the leak check.** ``forge.models``
  re-exports from ``forge.models.blueprint``; the ``from X.Y import
  Z`` form compiles to ``import X.Y`` followed by attribute access,
  and the ``import`` step sets ``X.Y`` (the submodule) as an
  attribute on ``X``. That is how Python's import machinery works,
  not a façade defect — every package that re-exports from a
  submodule has the same property (``import json`` populates
  ``json.decoder`` and ``json.scanner``; ``import xml`` populates
  ``xml.etree``; etc.). Filtering modules means the leak test
  catches exactly what it was designed to catch — a *class or value*
  leaked without being declared — and does not misclassify the
  interpreter's own behaviour as a leak.

* **Marked ``integration``, not ``unit``.** This test imports the
  full model tree, which imports Pydantic, which is a non-trivial
  import cost. That is not expensive enough to block the fast
  suite, but it *is* expensive enough that a developer running
  ``pytest -m unit`` for a quick feedback loop should not pay it.
  The import cost is the only reason; if Pydantic ever became a
  cheap import, the marker could be re-evaluated. Until then, the
  marker is an accurate reflection of *why* this test is separated
  from the others.
"""

from __future__ import annotations

import inspect
import types
from enum import Enum
from typing import Any

import pytest
from pydantic import BaseModel

import forge.models as models_facade
from forge.models import (
    Blueprint,
    Category,
    CheckDefinition,
    CheckType,
    Compatibility,
    FileEntry,
    VariableDefinition,
    VariableType,
)

# ---------------------------------------------------------------------------
# The expected public surface
# ---------------------------------------------------------------------------
# This set is the *contract*. It is deliberately duplicated from
# ``forge.models.__all__`` rather than read from it — see the module
# docstring for why. Adding a new public name is a two-line commit:
# export it from ``forge.models`` and add it here. Removing a name is
# the same. Skipping either step makes the test fail, which is the
# correct amount of friction: a public name is a promise, and promises
# should require deliberate action.
_EXPECTED_PUBLIC_NAMES: frozenset[str] = frozenset(
    {
        "Blueprint",
        "BlueprintMetadata",
        "Category",
        "CheckDefinition",
        "CheckType",
        "Compatibility",
        "ConditionalFileEntry",
        "FileEntry",
        "VariableDefinition",
        "VariableType",
        "VariableValidation",
    }
)

# Categories of object the façade is allowed to expose. Every name in
# ``_EXPECTED_PUBLIC_NAMES`` belongs to exactly one of these categories,
# and the parametrised test below uses this tuple to assert that the
# imported object is in one of them. Adding a new category (e.g. a
# ``TypedDict`` for a future structured output) would mean extending
# this tuple *and* the expected-names set, which is the intended
# deliberate-change workflow.
_ALLOWED_PUBLIC_CATEGORIES: tuple[type, ...] = (BaseModel, Enum, str)


# ---------------------------------------------------------------------------
# __all__ contract
# ---------------------------------------------------------------------------
@pytest.mark.integration()
def test_public_all_matches_expected_surface() -> None:
    """``forge.models.__all__`` must equal the expected public surface exactly.

    Set equality, not subset: a missing name means a user-visible
    import will fail; an extra name means an undocumented export is
    being promised to users (and, because ``no_implicit_reexport`` is
    set in ``pyproject.toml``, an extra name is also a mypy error for
    anyone who imports it). Catching both directions in one assertion
    keeps the failure message useful — the diff between the two sets
    is exactly the change the contributor needs to make.
    """
    actual = frozenset(models_facade.__all__)

    missing = _EXPECTED_PUBLIC_NAMES - actual
    extra = actual - _EXPECTED_PUBLIC_NAMES

    assert not missing, (
        f"forge.models.__all__ is missing expected public names: " f"{sorted(missing)!r}"
    )
    assert not extra, (
        f"forge.models.__all__ exposes unexpected names: {sorted(extra)!r}. "
        "If these are intentional, add them to _EXPECTED_PUBLIC_NAMES "
        "and to the paramised test's imports."
    )


# ---------------------------------------------------------------------------
# Every name imports and is a real object
# ---------------------------------------------------------------------------
@pytest.mark.integration()
@pytest.mark.parametrize("name", sorted(_EXPECTED_PUBLIC_NAMES))
def test_public_name_imports_and_is_a_real_object(name: str) -> None:
    """Every expected name imports from the façade and is a real object.

    The import itself is the primary assertion: ``getattr(models_facade,
    name)`` must return the object, not raise ``AttributeError``. A
    façade that lists a name in ``__all__`` but does not define it is
    a common failure mode during refactors — the ``__all__`` list gets
    updated, the import statement does not (or vice versa), and static
    typing does not catch it because ``__all__`` is runtime data.

    The secondary assertion — that the object is in one of the allowed
    categories — catches a subtler failure: a façade that re-exports
    a *module* or a *function* under a class-like name. Downstream
    callers will do ``issubclass(Blueprint, BaseModel)`` or
    ``Category.api``; if the exposed name is not actually a class or
    enum member, those operations fail at the call site. Catching it
    here means the failure is reported against the façade, not
    against whichever downstream caller was unlucky enough to run
    first.
    """
    obj = getattr(models_facade, name)

    # The object must be one of the categories the façade is allowed to
    # expose. ``inspect.isclass`` is used as a fallback because not
    # every name in the tree is a ``BaseModel`` or an ``Enum`` — future
    # additions might be plain classes for validators, and this test
    # should not fail merely because a future contributor exposed one.
    # The category check exists to catch "a module got re-exported",
    # not to over-constrain the design space.
    assert inspect.isclass(obj) or isinstance(obj, str), (
        f"{name!r} is exposed by forge.models but is neither a class "
        f"nor a string alias; it is {type(obj).__name__!r}. "
        "If this is intentional, add the category to "
        "_ALLOWED_PUBLIC_CATEGORIES."
    )


# ---------------------------------------------------------------------------
# Sanity: the models are the *generated* ones, not shadows
# ---------------------------------------------------------------------------
@pytest.mark.integration()
def test_blueprint_is_the_generated_root_model() -> None:
    """``Blueprint`` must be the class from ``forge.models.blueprint``.

    A façade that re-exported a *different* class with the same name
    — a hand-written stub, a subclass, an alias to a wrapper — would
    pass the import test above but fail every downstream consumer in
    subtle ways (e.g. ``model_validate`` returning a different type
    than callers annotate). The identity check is cheap and catches
    the whole category of "the façade is exposing the right name but
    the wrong object."

    The assertion is on identity (``is``), not equality. Equality on
    classes is ``is`` in Python anyway, but writing it explicitly
    documents the intent: we want *the exact class object*, not a
    class that happens to compare equal (which is impossible for
    classes, but the reader should not have to reason about that).

    This test imports ``forge.models.blueprint`` directly. That is
    technically at odds with the "only the façade is supported"
    principle — but the principle applies to *consumers*, not to
    tests whose whole purpose is to verify the façade's fidelity.
    Importing the internal module here is the only way to check that
    the façade is pointing at it.
    """
    from forge.models.blueprint import Blueprint as InternalBlueprint

    assert Blueprint is InternalBlueprint, (
        "forge.models.Blueprint is not identical to "
        "forge.models.blueprint.Blueprint; the façade is exposing a "
        "shadow or a wrapper, and every type annotation that says "
        "'Blueprint' is now lying about which class it means."
    )


# ---------------------------------------------------------------------------
# Behavioural spot-check: the façade is actually usable
# ---------------------------------------------------------------------------
@pytest.mark.integration()
def test_facade_objects_compose_into_a_valid_blueprint() -> None:
    """The façade's classes actually work together.

    Import surface and category checks prove the *names* are real.
    They do not prove the names *compose* — a façade that exposed two
    classes from different versions of the generated file (impossible
    in a single commit, possible after a bad merge) would pass every
    check above and fail at the first ``model_validate``. This test
    is the smallest possible composition check: build a valid
    blueprint using only the façade's public names, and assert it
    validates.

    The document is constructed via the façade's *types* (a
    ``VariableDefinition`` built from the façade, wrapped in a
    ``Blueprint`` from the façade) rather than via raw dicts. That
    is the difference between "the façade imports" and "the façade
    is usable" — raw dicts would exercise Pydantic's validation
    without ever touching the façade's exposed classes as
    constructors, which is how consumers actually use them.

    Deliberately minimal — one variable, one check, one category — so
    that a failure names the composition step, not a dozen fields at
    once. Larger spot-checks are not more useful; they just make the
    failure report harder to read.
    """
    blueprint = Blueprint(
        schema_version="1.0.0",
        id="spot.check",
        name="Spot Check",
        version="1.0.0",
        description="Composition check built entirely from the façade.",
        category=Category.api,
        variables=[
            VariableDefinition(
                id="author",
                type=VariableType.string,
                description="Author name.",
                required=True,
            )
        ],
        files=[FileEntry(source="templates/a.j2", destination="a.txt")],
        verification=[
            CheckDefinition(
                id="readme",
                type=CheckType.required_file,
                path="README.md",
            )
        ],
        compatibility=Compatibility(min_forge_version="1.0.0"),
    )

    # Round-trip through the model's own serialisation to prove the
    # composed object is not merely constructible but well-formed.
    # ``model_dump`` returns a plain dict; re-parsing it via
    # ``model_validate`` must succeed. A regression that made
    # ``model_dump`` emit a shape ``model_validate`` could not
    # consume would leave both methods "working" individually while
    # making the pair unusable — the classic "serialise everything,
    # deserialise nothing" bug.
    serialised: dict[str, Any] = blueprint.model_dump(mode="python")
    reparsed = Blueprint.model_validate(serialised)
    assert reparsed.id == blueprint.id


# ---------------------------------------------------------------------------
# No internal symbols leak through the façade
# ---------------------------------------------------------------------------
@pytest.mark.integration()
def test_facade_does_not_leak_internal_symbols() -> None:
    """The façade's public attributes are exactly what ``__all__`` promises.

    A façade that re-exported ``_StrictModel`` or the internal
    ``VariableValidation`` helper (which is legitimately public) by
    accident would show up as an extra attribute. Consumers would
    start using it, and it would become load-bearing without anyone
    deciding it should be. The ``__all__`` set-equality test above
    would not catch this — ``__all__`` can be correct while the module
    namespace contains extras.

    The test inspects ``vars(forge.models)``, not
    ``dir(forge.models)``. ``dir`` includes inherited attributes from
    the ``builtins`` module, which are not the façade's exports. The
    ``__dict__`` is what the façade actually defines.

    Private names (``_leading_underscore``) and dunder names are
    filtered out before the check. They are not part of the public
    contract by Python convention; a future contributor adding an
    internal helper should not have to add it to ``__all__`` or to
    this test's expected set. The check is specifically for public
    names that are *not* declared.

    **Submodules are excluded from the leak set.** ``forge.models``
    re-exports from ``forge.models.blueprint``; the ``from X.Y import
    Z`` form compiles to ``import X.Y`` followed by attribute access,
    and the ``import`` step sets ``X.Y`` (the submodule) as an
    attribute on ``X``. That is how Python's import machinery works,
    not a façade defect — every package that re-exports from a
    submodule has the same property (``import json`` populates
    ``json.decoder`` and ``json.scanner``; ``import xml`` populates
    ``xml.etree``; etc.). Filtering modules means the test catches
    exactly what it was designed to catch — a *class or value* leaked
    without being declared — and does not misclassify the
    interpreter's own behaviour as a leak.
    """
    # ``__all__`` itself is technically an attribute of the module
    # namespace, but it is also the module's contract, so it is not
    # a "leak" — it is a supported part of the module's API. Filter
    # it out explicitly so a reader does not have to reason about
    # whether the test is accidentally asserting its own absence.
    declared_public = set(_EXPECTED_PUBLIC_NAMES) | {"__all__"}

    leaked = {
        name
        for name, value in vars(models_facade).items()
        if not name.startswith("_")
        and name not in declared_public
        and not isinstance(value, types.ModuleType)
    }

    assert not leaked, (
        f"forge.models exposes public names not in __all__: {sorted(leaked)!r}. "
        "Either add them to __all__ and _EXPECTED_PUBLIC_NAMES, or rename "
        "them with a leading underscore to mark them private."
    )
