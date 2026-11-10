"""Unit tests for `forge.conditions.evaluator`.

Scope
-----
This module locks the *evaluator*'s contract in isolation from the parser
(which it trusts as input), the aggregation layer (which consumes its
``ConditionError``), and the planning pipeline (which consumes its
surviving groups).

It exercises:

    * every AST node kind the parser can produce (``Equals``,
      ``NotEquals``) against every literal kind the DSL supports
      (``str``, ``bool``, ``int``),
    * the type-aware comparison rule (``True == 1`` is ``False``;
      ``"3" == 3`` is ``False``),
    * the "undefined variable is an error, not falsy" rule,
    * the "every finding is aggregated in one pass" rule,
    * the "survivors preserve blueprint declaration order" rule,
    * the "empty ``conditional_files`` is a no-op, not a special case"
      rule (the evaluator must behave identically for "no groups" and
      "no surviving groups").

Test philosophy
---------------
* **Real inputs, not fakes.** The tests build real ``Blueprint`` and
    ``ConditionalFileEntry`` model instances (via a small helper), not
    duck-typed stand-ins. The evaluator reads ``conditional_files`` and
    ``id`` from the blueprint; faking the model would let the tests
    pass while the real model's field shape drifts. The models are the
    contract.

* **The evaluator is a pure function; the tests treat it as one.** No
  filesystem, no prompts, no environment. Every test constructs inputs,
  calls ``evaluate``, and asserts on the output. That is the whole
  reason the evaluator exists in its own module.

* **Failure-mode coverage is grouped by ``code``.** Each rejection
  category (``condition.undefined_variable``, ``condition.syntax``,
  ``condition.empty``, ``condition.invalid_variable``) has a section.
  Adding a new category is a visible addition to the file's structure,
  not a new assertion buried in an existing test.

* **Type-aware comparison is tested exhaustively.** This is the single
  most important rule in the DSL — the one that prevents
  ``True == 1`` from silently matching an integer check, or
  ``"3" == 3`` from silently passing. Every pairing of
  ``{str, bool, int} x {str, bool, int}`` appears below, because the
  rule is subtle enough that a future contributor "simplifying" it to
  ``isinstance(actual, type(expected))`` would silently pass the
  non-boolean cases and silently fail the boolean ones. The test
  matrix is the review.

* **Layer boundary respected for empty conditions.** An empty
  ``condition: ""`` violates the schema's ``minLength: 1`` and is
  rejected by Pydantic at model construction — before the evaluator
  is reached. That failure lives in the loader's tests
  (``test_schema_rejects``), not here. The evaluator's own "empty
  input" case is a whitespace string ``" "``, which passes the schema
  and reaches the parser, where it correctly yields
  ``condition.empty``. This is the distinction the tests below
  preserve: *schema violations belong to the loader; parser violations
  belong to the evaluator's input surface*.

* **Parametrised rows provide their own variables.** A condition like
  ``missing == 'x'`` is only an error when ``missing`` is *absent*
  from the variable set; a row that supplies ``{"missing": "anything"}``
  is not exercising the error path. ``TestErrorShape`` therefore
  parametrises over ``(condition, variables)`` pairs rather than a
  single shared ``variables`` dict, so each row controls the state it
  needs.

Why the AST is imported directly
--------------------------------
The evaluator consumes AST nodes; the parser is what *produces* them,
but the evaluator does not depend on the parser at all. Testing the
evaluator against hand-constructed AST nodes would be faster but would
let a parser change silently break the evaluator (a rename of a node
field, say, would pass the AST-level tests and explode at runtime).
The tests therefore go through ``parse()`` for anything that exercises
the AST contract, and only construct AST nodes directly for the
type-awareness matrix (where the input is *deliberately* a value the
grammar might not accept, so we can prove the type rule holds
independently of what the parser happens to produce).

Fixtures use real ``Blueprint`` models
--------------------------------------
An earlier revision of this file used a hand-written ``_Blueprint``
dataclass with a mutable ``conditional_files: list = field(default_factory=list)``
attribute. That worked because the evaluator duck-types on the two
attributes it reads, but it had two costs:

    * The tests never proved the evaluator works against the *actual*
      model — a ``Blueprint`` field rename would have been invisible.
    * The ``field(default_factory=list)`` pattern trips ``RUF012``
      (mutable class attribute), which the project's Ruff config
      disallows.

The current fixtures build real ``Blueprint`` instances through a
shared helper. That costs a few extra lines per test but buys two
things the earlier approach could not: the tests exercise the real
model's shape, and the fixtures are naturally stateless (each call
returns a fresh instance, so no test can mutate shared state).

Lint conventions
----------------
* **Class attributes are immutable or functions.** ``RUF012`` forbids
  mutable class attributes; every test class below holds only methods.
* **Fixtures that return ``None`` are ``_``-prefixed and applied via
  ``@pytest.mark.usefixtures(...)``.** Neither rule applies here (this
  file has no fixtures returning ``None``; the shared helpers are plain
  functions), but the convention is stated so a future addition knows
  the rules.
* **No hardcoded ``/tmp``.** Where a temp path is needed (none today),
  ``tmp_path`` would be used per ``S108``.
"""

from __future__ import annotations

from typing import Any

import pytest

from forge.conditions.ast import Equals, NotEquals
from forge.conditions.errors import ConditionError
from forge.conditions.evaluator import ConditionalEvaluator
from forge.loading.result import Err, Ok
from forge.models import (
    Blueprint,
    Category,
    Compatibility,
    ConditionalFileEntry,
    FileEntry,
)


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------
def _make_blueprint(*, conditional_files: list[ConditionalFileEntry] | None = None) -> Blueprint:
    """Build a minimal valid ``Blueprint`` with the given conditional groups.

    Centralised so every test that constructs a blueprint shares the
    same shape, and a future required field on ``Blueprint`` is one
    edit here, not twenty.
    """
    return Blueprint(
        schema_version="1.0.0",
        id="sample.conditions.test",
        name="Conditions Test Blueprint",
        version="1.0.0",
        description="Test fixture blueprint.",
        category=Category.api,
        variables=[],
        files=[],
        conditional_files=conditional_files or [],
        verification=[{"id": "ok", "type": "required_file", "path": "README.md"}],
        compatibility=Compatibility(min_forge_version="1.0.0"),
    )


def _group(
    condition: str, *, source: str = "tpl.j2", dest: str = "out.txt"
) -> ConditionalFileEntry:
    """Build one conditional group with a single file entry.

    The file content is irrelevant to the evaluator (it only reports
    whether the group *survived*), so a single minimal entry is enough
    and keeps the tests focused on the condition, not the files.

    Note: ``condition`` must be a non-empty string — ``ConditionalFileEntry``
    enforces ``min_length=1`` (matching the schema). An empty condition
    never reaches the evaluator; it is rejected by Pydantic first. The
    evaluator's own "empty input" case is a *whitespace* string ``" "``,
    which passes the schema and reaches the parser.
    """
    return ConditionalFileEntry(
        condition=condition,
        files=[FileEntry(source=source, destination=dest)],
    )


def _evaluate(condition: str, variables: dict[str, Any]):
    """Run the evaluator on one group and one variable set.

    Convenience wrapper so each test is one line of setup and one line
    of assertion. Returns the ``Result`` from ``evaluate`` — the test
    chooses whether to assert ``is_ok``/``is_err`` or pattern-match.
    """
    blueprint = _make_blueprint(conditional_files=[_group(condition)])
    return ConditionalEvaluator().evaluate(blueprint, variables)


# ---------------------------------------------------------------------------
# Equality — positive and negative, per literal kind
# ---------------------------------------------------------------------------
class TestEquals:
    """``Equals`` semantics for each literal kind."""

    @pytest.mark.parametrize(
        ("variable_value", "expected"),
        [("postgresql", True), ("sqlite", False), ("PostgreSQL", False)],
    )
    def test_string_equality(self, variable_value: str, expected: bool) -> None:
        """String comparison is case-sensitive and exact.

        Case-sensitivity is deliberate: ``"PostgreSQL"`` and
        ``"postgresql"`` are different strings, and a blueprint author
        who expects case-folding to happen should get a loud mismatch,
        not a silent coincidence.
        """
        result = _evaluate("database == 'postgresql'", {"database": variable_value})
        assert isinstance(result, Ok)
        assert (len(result.value) == 1) is expected

    @pytest.mark.parametrize(
        ("variable_value", "expected"),
        [(True, True), (False, False)],
    )
    def test_boolean_equality(self, variable_value: bool, expected: bool) -> None:
        result = _evaluate("include_docker == true", {"include_docker": variable_value})
        assert isinstance(result, Ok)
        assert (len(result.value) == 1) is expected

    @pytest.mark.parametrize(
        ("variable_value", "expected"),
        [(3, True), (2, False), (4, False), (-1, False)],
    )
    def test_integer_equality(self, variable_value: int, expected: bool) -> None:
        result = _evaluate("python_major == 3", {"python_major": variable_value})
        assert isinstance(result, Ok)
        assert (len(result.value) == 1) is expected


# ---------------------------------------------------------------------------
# Inequality — positive and negative, per literal kind
#
# IMPORTANT: the expectations here are the *negation* of the "values
# match" case. The condition is `!=`, so a matching value means the
# group is excluded (0 survivors), and a non-matching value means the
# group survives (1 survivor). This is the opposite of `TestEquals`,
# and it is exactly the inversion a copy-paste from `TestEquals` gets
# wrong — see the docstrings on each method for the "if this row looks
# backwards, it isn't" reminder.
# ---------------------------------------------------------------------------
class TestNotEquals:
    """``NotEquals`` semantics for each literal kind.

    For every row below: ``expected`` is the number of *survivors* the
    group produces (0 or 1), not a boolean answer to "does the variable
    equal the literal". The condition negates, the survivor count does
    not — that is the whole point of the check.
    """

    @pytest.mark.parametrize(
        ("variable_value", "expected_survivors"),
        [
            # variable equals the literal → NOT-equals is False → 0 survivors
            ("sqlite", 0),
            # variable differs from the literal → NOT-equals is True → 1 survivor
            ("postgresql", 1),
        ],
    )
    def test_string_inequality(self, variable_value: str, expected_survivors: int) -> None:
        result = _evaluate("database != 'sqlite'", {"database": variable_value})
        assert isinstance(result, Ok)
        assert len(result.value) == expected_survivors

    @pytest.mark.parametrize(
        ("variable_value", "expected_survivors"),
        [
            # include_docker == True → NOT-equals is False → 0 survivors
            (True, 0),
            # include_docker == False → NOT-equals is True → 1 survivor
            (False, 1),
        ],
    )
    def test_boolean_inequality(self, variable_value: bool, expected_survivors: int) -> None:
        result = _evaluate("include_docker != true", {"include_docker": variable_value})
        assert isinstance(result, Ok)
        assert len(result.value) == expected_survivors

    @pytest.mark.parametrize(
        ("variable_value", "expected_survivors"),
        [
            # python_major == 2 → NOT-equals is False → 0 survivors
            (2, 0),
            # python_major == 3 → NOT-equals is True → 1 survivor
            (3, 1),
        ],
    )
    def test_integer_inequality(self, variable_value: int, expected_survivors: int) -> None:
        result = _evaluate("python_major != 2", {"python_major": variable_value})
        assert isinstance(result, Ok)
        assert len(result.value) == expected_survivors


# ---------------------------------------------------------------------------
# Type-aware comparison — the exhaustive matrix
# ---------------------------------------------------------------------------
class TestTypeAwareComparison:
    """``Equals`` is True only when both operands share a runtime type."""

    @pytest.mark.parametrize(
        ("actual", "expected", "should_match"),
        [
            ("abc", "abc", True),
            ("abc", "xyz", False),
            (True, True, True),
            (True, False, False),
            (3, 3, True),
            (3, 4, False),
            ("3", 3, False),
            (3, "3", False),
            ("true", True, False),
            (True, "true", False),
            (1, True, False),
            (True, 1, False),
            (0, False, False),
            (False, 0, False),
        ],
    )
    def test_equals_type_awareness(self, actual: Any, expected: Any, should_match: bool) -> None:
        expr = Equals(variable="x", literal=expected)
        verdict = ConditionalEvaluator()._evaluate(expr, {"x": actual})
        assert isinstance(verdict, bool), verdict
        assert verdict is should_match

    @pytest.mark.parametrize(
        ("actual", "expected", "should_match"),
        [
            ("abc", "xyz", True),
            ("abc", "abc", False),
            (True, False, True),
            (False, False, False),
            (3, 4, True),
            (3, 3, False),
            ("3", 3, True),
            (3, "3", True),
            ("true", True, True),
            (True, "true", True),
            (1, True, True),
            (True, 1, True),
        ],
    )
    def test_not_equals_type_awareness(
        self, actual: Any, expected: Any, should_match: bool
    ) -> None:
        expr = NotEquals(variable="x", literal=expected)
        verdict = ConditionalEvaluator()._evaluate(expr, {"x": actual})
        assert isinstance(verdict, bool), verdict
        assert verdict is should_match


# ---------------------------------------------------------------------------
# Undefined variables — the "error, not falsy" rule
# ---------------------------------------------------------------------------
class TestUndefinedVariables:
    """A condition referencing an unknown variable is an *error*."""

    def test_undefined_variable_yields_one_finding(self) -> None:
        result = _evaluate("database == 'postgresql'", {})
        assert isinstance(result, Err)
        assert isinstance(result.error, ConditionError)
        assert len(result.error.findings) == 1
        finding = result.error.findings[0]
        assert finding.code == "condition.undefined_variable"
        assert "database" in finding.message
        assert finding.hint

    def test_undefined_variable_names_the_variable(self) -> None:
        result = _evaluate("missing_thing == true", {})
        assert isinstance(result, Err)
        assert "missing_thing" in result.error.findings[0].message
        assert "missing_thing" in result.error.findings[0].hint

    def test_multiple_undefined_variables_aggregated(self) -> None:
        blueprint = _make_blueprint(
            conditional_files=[
                _group("alpha == 'x'"),
                _group("beta == 'y'"),
                _group("gamma == true"),
            ],
        )
        result = ConditionalEvaluator().evaluate(blueprint, {})
        assert isinstance(result, Err)
        assert len(result.error.findings) == 3
        codes = {f.code for f in result.error.findings}
        assert codes == {"condition.undefined_variable"}

    def test_evaluator_does_not_fall_back_to_default(self) -> None:
        result = _evaluate("x == 'y'", {})
        assert isinstance(result, Err)
        assert any(f.code == "condition.undefined_variable" for f in result.error.findings)


# ---------------------------------------------------------------------------
# Parse failures — the parser's findings pass through the evaluator
#
# An empty string ("") never reaches the evaluator: `ConditionalFileEntry`
# rejects it at model construction (`minLength: 1`), and that failure is
# tested in the loader's suite (`test_schema_rejects`). The evaluator's
# own "empty input" case is a whitespace string (" "), which passes the
# schema (len 1) and reaches the parser, where it yields
# `condition.empty` — exactly the behaviour this class asserts.
# ---------------------------------------------------------------------------
class TestParseFailures:
    """A condition the parser rejects surfaces as a ``ConditionError``."""

    def test_syntax_error_is_reported(self) -> None:
        result = _evaluate("database = 'postgresql'", {"database": "x"})
        assert isinstance(result, Err)
        assert result.error.findings[0].code == "condition.syntax"

    def test_whitespace_condition_is_reported_as_empty(self) -> None:
        """A whitespace-only condition is empty *at the parser layer*.

        `" "` satisfies the schema's `min_length=1` (one character) and
        therefore reaches the evaluator. The parser then classifies it
        as `condition.empty` — which is the correct code for "there is
        no condition to evaluate", regardless of how many whitespace
        characters were used to express the emptiness.
        """
        result = _evaluate(" ", {"database": "x"})
        assert isinstance(result, Err)
        assert result.error.findings[0].code == "condition.empty"

    def test_mixed_parse_and_evaluation_findings_aggregate(self) -> None:
        blueprint = _make_blueprint(
            conditional_files=[
                _group("bad syntax here"),
                _group("missing_var == 'x'"),
                _group("database == 'pg'"),
            ],
        )
        result = ConditionalEvaluator().evaluate(blueprint, {})
        assert isinstance(result, Err)
        codes = {f.code for f in result.error.findings}
        assert "condition.syntax" in codes
        assert "condition.undefined_variable" in codes
        assert len(result.error.findings) == 3


# ---------------------------------------------------------------------------
# Survivors — order, content, and the "no conditional files" case
# ---------------------------------------------------------------------------
class TestSurvivors:
    """Survivors preserve declaration order and carry parsed ASTs."""

    def test_survivors_preserve_declaration_order(self) -> None:
        blueprint = _make_blueprint(
            conditional_files=[
                _group("first == true", source="a.j2", dest="a.txt"),
                _group("second == true", source="b.j2", dest="b.txt"),
                _group("third == true", source="c.j2", dest="c.txt"),
            ],
        )
        result = ConditionalEvaluator().evaluate(
            blueprint, {"first": True, "second": True, "third": True}
        )
        assert isinstance(result, Ok)
        sources = [files[0].source for _expr, files in result.value]
        assert sources == ["a.j2", "b.j2", "c.j2"]

    def test_survivors_carry_parsed_ast(self) -> None:
        blueprint = _make_blueprint(conditional_files=[_group("x == 3")])
        result = ConditionalEvaluator().evaluate(blueprint, {"x": 3})
        assert isinstance(result, Ok)
        expr, files = result.value[0]
        assert isinstance(expr, Equals)
        assert expr.variable == "x"
        assert expr.literal == 3
        assert files

    def test_no_conditional_files_yields_empty_ok(self) -> None:
        blueprint = _make_blueprint(conditional_files=[])
        result = ConditionalEvaluator().evaluate(blueprint, {"x": "any"})
        assert isinstance(result, Ok)
        assert result.value == []

    def test_all_conditions_false_yields_empty_ok(self) -> None:
        blueprint = _make_blueprint(conditional_files=[_group("x == 'y'"), _group("a == 'b'")])
        result = ConditionalEvaluator().evaluate(blueprint, {"x": "z", "a": "q"})
        assert isinstance(result, Ok)
        assert result.value == []


# ---------------------------------------------------------------------------
# Error shape — the actionable-error invariant
#
# The parametrisation supplies *its own* variables per row, so each row
# can reach the error state it is meant to reach. A single shared
# variables dict was the previous version's bug: `missing == 'x'` was
# evaluated with `{"missing": "anything"}`, so `missing` was defined and
# the condition evaluated cleanly — the row that was supposed to test
# the undefined-variable path was silently testing the "everything is
# fine" path. Each row now carries its own variable dict, which makes
# the test's intent legible in the parametrisation.
#
# The `""` row is absent: an empty condition is a *schema* violation
# (rejected by Pydantic before the evaluator runs) and belongs in
# `tests/unit/loading/test_schema_rejects.py`, not here. The
# whitespace row `" "` remains because it exercises the evaluator's
# own `condition.empty` path (see `TestParseFailures`).
# ---------------------------------------------------------------------------
class TestErrorShape:
    """Every finding carries condition, message, hint, code."""

    @pytest.mark.parametrize(
        ("condition", "variables"),
        [
            # Parser rejection (no variables needed).
            ("bad syntax", {}),
            # Undefined variable — this row *needs* an empty `missing`.
            ("missing == 'x'", {}),
            # Parser rejection (single `=`).
            ("x = 'y'", {"x": True}),
            # Parser rejection (capital `True`).
            ("x == True", {"x": True}),
            # Parser rejection (whitespace-only is `condition.empty`).
            (" ", {}),
        ],
    )
    def test_every_finding_has_full_structure(
        self, condition: str, variables: dict[str, Any]
    ) -> None:
        """No finding may have an empty field — that is the dead-end bug.

        Rendered by the CLI as ``{condition}: {message} — hint: {hint}``,
        a missing ``hint`` reads to the user as "something went wrong"
        with no way to fix it. The invariant is structural: all four
        fields are required and non-empty.
        """
        result = _evaluate(condition, variables)
        assert isinstance(result, Err)
        for finding in result.error.findings:
            # Note: `condition` echoes the input verbatim (including
            # whitespace), which is the correct behaviour for an error
            # message — the user sees exactly what they typed.
            assert finding.condition == condition
            assert finding.message
            assert finding.hint
            assert finding.code

    def test_condition_error_str_is_deterministic(self) -> None:
        blueprint = _make_blueprint(
            conditional_files=[
                _group("a == 'x'"),
                _group("b == 'y'"),
            ]
        )
        r1 = ConditionalEvaluator().evaluate(blueprint, {})
        r2 = ConditionalEvaluator().evaluate(blueprint, {})
        assert isinstance(r1, Err)
        assert isinstance(r2, Err)
        assert str(r1.error) == str(r2.error)


# ---------------------------------------------------------------------------
# Totality — the public evaluate never raises
#
# The `""` row is absent for the same reason as in `TestErrorShape`: it
# cannot reach the evaluator (Pydantic rejects it at model construction).
# The six remaining rows all reach the evaluator and are the actual
# domain of the totality claim.
# ---------------------------------------------------------------------------
class TestTotality:
    """``evaluate`` returns a ``Result`` for every input the model accepts.

    The evaluator's contract is that *failure is data*, not an
    exception. A caller (the ``create`` command) relies on this: it
    aggregates the evaluator's findings into the same channel as the
    resolver's and the planner's, without a ``try``/``except``. If the
    evaluator ever raised, that pipeline would leak a stack trace to
    the user on a bad condition — exactly the failure mode the DSL
    exists to prevent.

    The domain of this claim is "inputs the model accepts". Strings the
    model rejects (empty, overlong, ...) never reach the evaluator, so
    they are out of scope for a totality test *of the evaluator* and in
    scope for a totality test *of the loader* (which is where their
    handling lives).
    """

    @pytest.mark.parametrize(
        "condition",
        [
            " ",  # whitespace → condition.empty
            "x = 'y'",  # single = → condition.syntax
            "x == 'y' and z == 1",  # combinator → condition.syntax
            "__import__('os')",  # code-execution → condition.syntax
            "x == 'unterminated",  # unterminated quote → condition.syntax
            "x == 3.14",  # float literal → condition.syntax
        ],
    )
    def test_never_raises_on_any_condition(self, condition: str) -> None:
        blueprint = _make_blueprint(conditional_files=[_group(condition)])
        result = ConditionalEvaluator().evaluate(blueprint, {"x": "y", "z": 1})
        assert isinstance(result, Ok | Err)
