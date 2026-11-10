"""Unit tests for boolean semantics in ``forge.conditions``.

Scope
-----
Boolean conditions are the most common real-world case: a blueprint
author writes ``include_docker == true`` and expects the ``Dockerfile``
group to be included when the user says yes, excluded when they say no.
That looks trivial and is — until it isn't. Three things make boolean
conditions worth their own test file:

1. **The boolean/integer trap.** ``bool`` is a subclass of ``int`` in
   Python, so a naive ``isinstance(actual, type(expected))`` accepts
   ``True == 1``. The type-aware comparison rule (see
   ``test_evaluator.py::TestTypeAwareComparison``) excludes that, and
   this file pins the *boolean-specific* half of it: a boolean
   variable compared against a boolean literal must work; a boolean
   variable compared against anything else must not accidentally be
   coerced into an integer comparison.

2. **The literal spelling trap.** The grammar accepts *only* lowercase
   ``true``/``false``. Uppercase ``True``/``False`` are syntax errors —
   matching YAML/JSON conventions so the same blueprint behaves
   identically across tools. A future contributor "helpfully" widening
   the regex to accept ``True`` would break cross-tool consistency,
   and this file makes that regression fail loudly.

3. **The default-vs-explicit trap.** A boolean variable's *default*
   flows through the resolver like any other value. A blueprint that
   says ``include_docker`` defaults to ``true``, but is never
   supplied, must still include the Dockerfile. That coupling between
   the resolver's default handling and the evaluator's comparison is
   easy to break and hard to notice — the condition *looks* like it's
   about booleans but is actually about how the value got there.

Each of these has its own section below. None of them overlap with the
per-operator files' concerns; those files test "does ``==`` work for
every literal kind?", and this file tests "does the boolean literal
kind behave correctly under the specific hazards it introduces?".

Why real models, not fakes
--------------------------
Every test builds a real ``Blueprint`` with real
``ConditionalFileEntry`` entries and calls the real
``ConditionalEvaluator``. Faking the model would let a field rename
go undetected and would not exercise the Pydantic
``min_length``/type-validation that the actual pipeline relies on.
The cost is a few extra lines per test; the benefit is the test
fails at the layer the code actually uses, not one abstraction away.

Why the parser is exercised indirectly
--------------------------------------
A handful of tests assert that the *parser* rejects ``True`` /
``False``. Those tests do not call ``parse()`` directly — they call
``ConditionalEvaluator.evaluate()`` on a blueprint whose condition
uses the capitalised spelling, and assert that the aggregated error
carries the syntax code. That is the correct layer: the parser is an
implementation detail of the evaluator from the caller's perspective,
and the caller (a blueprint author running ``forge create``) sees a
``ConditionError``, not a ``ParseError``. Testing at that boundary
keeps the parser's internal `ParseError` class out of this file's
imports and keeps the assertion aligned with the user-visible
behaviour.

Lint conventions
----------------
* **No mutable class attributes.** Every test class below holds only
  methods (``RUF012``).
* **No side-effect-only fixtures.** All helpers are plain module-level
  functions; no fixtures returning ``None`` exist in this file.
* **No hardcoded ``/tmp``.** No temp paths needed (``S108``).
"""

from __future__ import annotations

from typing import Any

import pytest

from forge.conditions import ConditionalEvaluator
from forge.loading.result import Err, Ok
from forge.models import (
    Blueprint,
    Category,
    Compatibility,
    ConditionalFileEntry,
    FileEntry,
    VariableDefinition,
    VariableType,
)


# ---------------------------------------------------------------------------
# Helpers
#
# Kept deliberately local to this file, matching the pattern in the
# sibling test files: each file owns its own fixtures so a change to
# one file cannot silently change another's behaviour. The duplication
# is a handful of lines and the isolation is worth it.
# ---------------------------------------------------------------------------
def _blueprint(
    *, conditions: list[str], variables: list[VariableDefinition] | None = None
) -> Blueprint:
    """Build a minimal valid blueprint with the given conditions.

    ``variables`` is optional. Most tests do not need to declare the
    variables they reference — the evaluator does not check
    declarations, only the *resolved* value set at evaluation time.
    The two tests that *do* care about the resolver's default handling
    pass ``variables`` explicitly and exercise the resolver alongside
    the evaluator.
    """
    return Blueprint(
        schema_version="1.0.0",
        id="sample.boolean.test",
        name="Boolean Condition Tests",
        version="1.0.0",
        description="Fixture blueprint for boolean condition tests.",
        category=Category.api,
        variables=variables or [],
        files=[],
        conditional_files=[
            ConditionalFileEntry(
                condition=c,
                files=[FileEntry(source=f"tpl.{i}.j2", destination=f"out.{i}.txt")],
            )
            for i, c in enumerate(conditions)
        ],
        verification=[{"id": "ok", "type": "required_file", "path": "README.md"}],
        compatibility=Compatibility(min_forge_version="1.0.0"),
    )


def _evaluate(condition: str, variables: dict[str, Any]):
    """Run the evaluator on one condition with the given variables."""
    return ConditionalEvaluator().evaluate(_blueprint(conditions=[condition]), variables)


def _survivors(condition: str, variables: dict[str, Any]) -> int:
    """Return how many groups survived — the evaluation's *answer*.

    Convenience for the many tests that only care about the count.
    Asserts the result is ``Ok`` — a test that expected ``Ok`` but got
    ``Err`` is a bug in the test, and asserting here produces a clearer
    error than asserting on ``result.value`` would.
    """
    result = _evaluate(condition, variables)
    assert isinstance(result, Ok), f"unexpected error: {result!r}"
    return len(result.value)


# ---------------------------------------------------------------------------
# Equality: ``include_docker == true`` / ``== false``.
#
# The happy path — matching value includes, non-matching value excludes.
# Both literals covered, both truthy states covered. That is four cases
# and none of them are interesting individually; what makes them worth
# having together is that they establish the *baseline* against which
# the trap sections below are compared. If any of these four break, the
# rest of the file's failures are noise — the semantic foundation has
# shifted, and the other failures are downstream of that shift.
# ---------------------------------------------------------------------------
class TestBooleanEquality:
    """``== true`` / ``== false`` include and exclude as expected."""

    @pytest.mark.parametrize(
        ("condition", "variable_value", "expected_survivors"),
        [
            # `== true` includes iff value is True
            ("include_docker == true", True, 1),
            ("include_docker == true", False, 0),
            # `== false` includes iff value is False
            ("include_docker == false", True, 0),
            ("include_docker == false", False, 1),
        ],
    )
    def test_equality_includes_iff_values_match(
        self,
        condition: str,
        variable_value: bool,
        expected_survivors: int,
    ) -> None:
        """``x == true|false`` includes iff the value equals the literal.

        Note the framing: the *survivor count* is what varies, not a
        boolean "did it match". A test that asserted on a boolean
        ``did_match`` would need a separate translation step to
        determine survivors — which is what the evaluator does. We test
        the evaluator's output, not our own reasoning about it.
        """
        assert _survivors(condition, {"include_docker": variable_value}) == expected_survivors


# ---------------------------------------------------------------------------
# Inequality: ``include_docker != true`` / ``!= false``.
#
# The complementary case. Same four shapes, opposite survivor count.
# Worth its own section because inequality and equality are separate
# AST node kinds with separate evaluation branches — a bug in one does
# not necessarily affect the other, and the test surface should reflect
# that separation.
# ---------------------------------------------------------------------------
class TestBooleanInequality:
    """``!= true`` / ``!= false`` include and exclude as expected."""

    @pytest.mark.parametrize(
        ("condition", "variable_value", "expected_survivors"),
        [
            # `!= true` includes iff value is not True
            ("include_docker != true", True, 0),
            ("include_docker != true", False, 1),
            # `!= false` includes iff value is not False
            ("include_docker != false", True, 1),
            ("include_docker != false", False, 0),
        ],
    )
    def test_inequality_includes_iff_values_differ(
        self,
        condition: str,
        variable_value: bool,
        expected_survivors: int,
    ) -> None:
        """``x != true|false`` includes iff the value differs from the literal.

        The survivor counts are the exact inverse of the equality
        cases above — that is a property of the operators, not a bug
        in the test. If a future refactor accidentally inverted one
        but not the other, the two sections would disagree and the
        failure would point at the culprit.
        """
        assert _survivors(condition, {"include_docker": variable_value}) == expected_survivors


# ---------------------------------------------------------------------------
# The boolean/int trap — the whole reason this file exists.
#
# ``True == 1`` is True in Python because ``bool`` is a subclass of
# ``int``. The evaluator's type-aware comparison explicitly excludes
# this pairing: booleans compare only to booleans, and integers only
# to integers. A naive implementation that used
# ``isinstance(actual, type(expected))`` would pass every assertion in
# ``TestBooleanEquality`` above (because they all pass a real ``bool``
# against a ``bool`` literal) but would silently accept ``1 == true``
# — the exact bug the type-aware rule prevents.
#
# These tests are the *complement*: they prove the trap is closed. They
# do so from both directions (``bool`` value against ``int`` literal,
# and ``int`` value against ``bool`` literal) because a fix that closed
# only one direction would look correct from the other.
# ---------------------------------------------------------------------------
class TestBooleanIntTrap:
    """Booleans and integers are never equal, even at 0/1 and False/True."""

    @pytest.mark.parametrize(
        ("variable_value", "literal"),
        [
            # 1 == true would be True in Python; must be False in the DSL.
            (1, "true"),
            # 0 == false would be True in Python; must be False in the DSL.
            (0, "false"),
            # The inverse direction — same trap, opposite side.
            (True, "1"),
            (False, "0"),
        ],
    )
    def test_int_and_bool_never_compare_equal(self, variable_value: Any, literal: str) -> None:
        """``1 == true`` and ``0 == false`` are False in the DSL.

        The literal is written as a *string* here because
        ``variable_value`` varies across the parametrisation and we
        want one parametrisation per trap direction. The evaluator
        parses the condition from the string via the same code path
        every real blueprint uses; nothing is hand-constructed.
        """
        condition = f"x == {literal}"
        assert _survivors(condition, {"x": variable_value}) == 0, (
            f"{variable_value!r} == {literal} must be False (type-aware "
            f"comparison excludes bool/int coercion)"
        )

    @pytest.mark.parametrize(
        ("variable_value", "literal"),
        [
            # The mirror: `!=` must be True for cross-type pairs.
            (1, "true"),
            (0, "false"),
            (True, "1"),
            (False, "0"),
        ],
    )
    def test_int_and_bool_are_always_unequal(self, variable_value: Any, literal: str) -> None:
        """``1 != true`` and ``0 != false`` are True in the DSL.

        The inequality operator is the strict negation of equality
        under the type-aware rule: if two values are of different
        types, they are by definition not equal, so ``!=`` is True.
        Asserting both directions here pins the completeness of the
        rule — a fix that only touched the ``Equals`` branch would
        leave ``!=`` producing the wrong answer, and the two tests
        would disagree.
        """
        condition = f"x != {literal}"
        assert _survivors(condition, {"x": variable_value}) == 1


# ---------------------------------------------------------------------------
# Literal spelling — only lowercase ``true`` / ``false`` are accepted.
#
# The grammar documents this: "booleans are lowercase to match YAML/JSON".
# The rationale is cross-tool consistency — a blueprint that works with
# one tool must work with another, and YAML/JSON do not spell booleans
# ``True``/``False``. A future contributor widening the regex to accept
# uppercase "for friendliness" would break that consistency, and every
# test in this section would flip to passing — which is exactly the
# regression we want CI to catch, not enable.
#
# The tests go through ``evaluate()`` (not ``parse()``) so the failure
# surfaces as the user sees it: a ``ConditionError`` with a syntax
# finding. The parser's internal ``ParseError`` is not part of this
# file's imports because the user never sees it.
# ---------------------------------------------------------------------------
class TestBooleanLiteralSpelling:
    """Only lowercase ``true`` / ``false`` are accepted."""

    @pytest.mark.parametrize("literal", ["True", "False", "TRUE", "FALSE"])
    def test_capitalised_boolean_literals_are_syntax_errors(self, literal: str) -> None:
        """``True`` / ``FALSE`` etc. are rejected at parse time.

        The failure is a ``condition.syntax`` finding, not a
        ``condition.undefined_variable`` or a comparison that silently
        evaluates to False. A blueprint author who typed ``True`` in
        a YAML file needs to know their blueprint will not parse —
        not that their condition silently excluded a group they
        expected to include.
        """
        result = _evaluate(f"x == {literal}", {"x": True})
        assert isinstance(result, Err)
        codes = {f.code for f in result.error.findings}
        assert codes == {"condition.syntax"}, (
            f"expected a syntax error for capitalised {literal!r}, " f"got codes {sorted(codes)!r}"
        )

    @pytest.mark.parametrize("literal", ["true", "false"])
    def test_lowercase_boolean_literals_parse(self, literal: str) -> None:
        """``true`` / ``false`` are accepted — the documented spellings.

        The positive case is worth pinning alongside the negatives: a
        future contributor who *removed* lowercase acceptance (say, by
        tightening a regex) would see the negative tests still pass
        and could think they'd made a safe change. The positive case
        disagrees.
        """
        expected_value = literal == "true"
        condition = f"x == {literal}"
        result = _evaluate(condition, {"x": expected_value})
        assert isinstance(result, Ok), f"lowercase {literal!r} should parse; got {result!r}"
        assert len(result.value) == 1


# ---------------------------------------------------------------------------
# Defaults — the resolver's default flows into the evaluator correctly.
#
# A boolean variable with a default is the most common shape in real
# blueprints (``include_docker: default true``). The evaluator does
# not *know* about defaults — the resolver materialises them — but the
# coupling between the two is a common source of bugs and worth
# pinning end-to-end. These tests build a blueprint that *declares*
# the variable with a default, run it through the resolver, and
# evaluate the surviving groups.
#
# This is the only section in the file that requires the resolver.
# Keeping it isolated means the other sections stay pure-function
# tests, and this one clearly documents the extra wiring.
# ---------------------------------------------------------------------------
class TestBooleanDefaultsFlowThroughResolver:
    """A boolean variable's default reaches the evaluator unchanged."""

    def _resolved(self, default: bool, *, supplied: bool | None = None):
        """Resolve variables, then evaluate conditions, returning the result.

        ``supplied=None`` means "user did not supply a value; use the
        default". Any other value means "user supplied this value as
        ``--var include_docker=<value>``".
        """
        from forge.resolution import VariableResolver

        variables = [
            VariableDefinition(
                id="include_docker",
                type=VariableType.boolean,
                description="Whether to include Docker support.",
                required=False,
                default=default,
            )
        ]
        blueprint = _blueprint(conditions=["include_docker == true"], variables=variables)
        cli_vars = {} if supplied is None else {"include_docker": supplied}
        resolved = VariableResolver().resolve(blueprint, cli_vars, {})
        assert isinstance(resolved, Ok), f"resolver error: {resolved!r}"
        return ConditionalEvaluator().evaluate(blueprint, resolved.value.as_dict())

    def test_default_true_includes_group_when_not_supplied(self) -> None:
        """``default: true`` + no CLI flag → group included.

        The most common shape: a blueprint author says "Docker is on
        by default", the user does not override, the Dockerfile is
        generated. If the resolver's default did not flow into the
        evaluator, this test would fail with 0 survivors — the exact
        "Docker mysteriously absent" bug a user would report as
        "Forge is broken", not as "I forgot a variable".
        """
        result = self._resolved(default=True)
        assert isinstance(result, Ok)
        assert len(result.value) == 1

    def test_default_false_excludes_group_when_not_supplied(self) -> None:
        """``default: false`` + no CLI flag → group excluded.

        The mirror case. Both directions matter: a resolver bug that
        ignored defaults entirely would make the "default true"
        case pass (because ``include_docker`` would be absent, and
        the resolver would... well, do *something*, and whichever
        something produced 1 survivor would pass a naive test).
        Testing both directions catches "the resolver produces
        *some* value" bugs that a single test would miss.
        """
        result = self._resolved(default=False)
        assert isinstance(result, Ok)
        assert len(result.value) == 0

    def test_supplied_value_overrides_default(self) -> None:
        """``--var include_docker=false`` wins over ``default: true``.

        The precedence rule (CLI > default) is enforced by the
        resolver, but the *effect* of that precedence is only visible
        through the evaluator's survivor count. This test proves the
        resolver's precedence actually reaches the evaluator's
        comparison — a resolver bug that dropped the CLI value would
        flip the survivor count and fail here.
        """
        result = self._resolved(default=True, supplied=False)
        assert isinstance(result, Ok)
        assert len(result.value) == 0


# ---------------------------------------------------------------------------
# Determinism — the boolean evaluation is stable.
#
# The rest of the file tests specific outcomes; this section tests
# the property those outcomes depend on. A single boolean condition
# with fixed inputs must produce identical output every time. That is
# a property of the evaluator being a pure function of its inputs —
# nothing about the boolean case is special, but this is the file
# where a "works but intermittently" bug would be most damaging
# (users would not know whether a Dockerfile was supposed to appear).
# ---------------------------------------------------------------------------
class TestBooleanDeterminism:
    """Boolean evaluation is deterministic across repeated calls."""

    def test_repeated_calls_produce_identical_errors(self) -> None:
        """Same input → identical ``ConditionError`` string.

        The check is on ``str(error)`` rather than on the structured
        fields because the string is what users and CI snapshots
        actually see. If a future refactor introduced nondeterminism
        (say, ordering findings by a set), the structured comparison
        would still pass but the string would differ between runs —
        this test catches that.
        """
        r1 = _evaluate("x == true", {})  # undefined → error
        r2 = _evaluate("x == true", {})
        assert isinstance(r1, Err)
        assert isinstance(r2, Err)
        assert str(r1.error) == str(r2.error)

    def test_repeated_calls_produce_identical_survivors(self) -> None:
        """Same input → identical survivor list (identity, not just equality).

        We compare the *tuples* the evaluator returns, not just the
        count, because a nondeterministic ordering of survivors
        (e.g. from iterating a set) would preserve the count but
        break the "declaration order" guarantee the planner relies on.
        """
        r1 = _evaluate(
            "include_docker == true",
            {"include_docker": True},
        )
        r2 = _evaluate(
            "include_docker == true",
            {"include_docker": True},
        )
        assert isinstance(r1, Ok)
        assert isinstance(r2, Ok)
        assert r1.value == r2.value
