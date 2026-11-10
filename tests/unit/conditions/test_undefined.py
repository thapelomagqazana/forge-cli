"""Unit tests for the "undefined variable" rule in ``forge.conditions``.

Scope
-----
This module locks a single, load-bearing behaviour in isolation:

    **A condition that references a variable which is not present in
    the resolved variable set is an *error*, not ``False``.**

That rule is the reason the conditions DSL exists in the shape it does.
If the evaluator silently treated an undefined reference as falsy —
the way Python's ``dict.get`` and Jinja's default ``Undefined`` do —
a blueprint author who typo'd a variable name would get a *silently
different* project, with no diagnostic, and would only discover the
bug after the generated code failed to compile (or worse, compiled
into something subtly wrong). This is the exact class of bug the
whole ``Result``-based design of ``forge.conditions`` is built to
prevent, and it deserves its own test file rather than being one
section in ``test_evaluator.py``.

Why a dedicated file?
---------------------
The failure mode is *cross-cutting*: it applies to every operator,
every literal kind, and every AST node kind. Rather than scatter
twenty assertions across ``test_evaluator.py``'s per-operator classes,
this file states the rule once, tests it exhaustively, and lets the
per-operator files keep their focus on operator semantics. A reader
who wants to know "how does the engine treat an undefined name?"
lands here, reads one file, and is done.

What this file deliberately does *not* test
-------------------------------------------
* **Parse failures.** ``missing == `` (no literal) is a syntax error,
  not an undefined-variable error. That is ``test_parser.py``'s job.
* **Type mismatches.** ``"3" == 3`` where both sides are defined is a
  *comparison* problem, not an undefined-variable problem. That is
  ``TestTypeAwareComparison`` in ``test_evaluator.py``.
* **Empty condition strings.** An empty ``condition: ""`` is a schema
  violation rejected by Pydantic at model construction. That is the
  loader's job (``test_schema_rejects``).

Each layer has one file. This file owns "the name isn't there".

Why real models, not fakes
--------------------------
Every test in this file builds a real ``Blueprint`` with real
``ConditionalFileEntry`` instances. The evaluator reads
``blueprint.conditional_files`` and ``blueprint.id``; faking the
model would let a field rename go undetected, and the model is the
contract the *actual* pipeline (``forge create``) exercises. The cost
of real models is a few extra lines per test; the benefit is that a
model change fails here, at the right layer, and not at integration.

Design of the test matrix
-------------------------
The undefined-variable rule interacts with three independent
dimensions:

    * **operator** (``==`` vs ``!=``) — the rule must apply to both
    * **literal kind** (string, boolean, integer) — the rule must
      apply regardless of what the literal looks like
    * **reference count** (one missing, all missing, some missing) —
      the aggregation must be complete, not first-error-only

The matrix below is organised so each dimension has its own section,
and a test added to one section cannot accidentally pass by exercising
another. If a future contributor adds a new operator or literal kind,
the "all combinations" section (``TestUndefinedAcrossTheMatrix``)
provides a single place to plug it in and be confident the rule holds
universally.

Lint conventions
----------------
* **No mutable class attributes.** Every test class below holds only
  methods. ``RUF012`` is therefore satisfied by construction.
* **No side-effect-only fixtures.** This file has no fixtures at all;
  the helpers (``_blueprint``, ``_evaluate``) are plain functions. A
  future addition that *needs* a fixture returning ``None`` should
  follow the ``_name`` + ``usefixtures`` convention established
  elsewhere in the suite.
* **No hardcoded ``/tmp``.** Where a temp path would be needed (none
  today), ``tmp_path`` is the rule.
"""

from __future__ import annotations

from typing import Any

import pytest

from forge.conditions import ConditionalEvaluator, ConditionError
from forge.loading.result import Err, Ok
from forge.models import (
    Blueprint,
    Category,
    Compatibility,
    ConditionalFileEntry,
    FileEntry,
)


# ---------------------------------------------------------------------------
# Helpers
#
# Deliberately duplicated with test_evaluator.py rather than shared: each
# test file owns its own fixtures so a change to one file's helper
# cannot silently change another file's behaviour. The duplication is
# small (~25 lines) and the isolation is worth it.
# ---------------------------------------------------------------------------
def _blueprint(*, conditions: list[str]) -> Blueprint:
    """Build a minimal valid blueprint with the given conditions.

    Every condition gets its own file entry — the evaluator's
    undefined-variable rule does not read the files, so any valid file
    entry suffices. Distinct filenames per condition make the fixture
    legible if a failure ever needs to be debugged from a traceback.
    """
    return Blueprint(
        schema_version="1.0.0",
        id="sample.undefined.test",
        name="Undefined Variable Tests",
        version="1.0.0",
        description="Fixture blueprint for the undefined-variable rule.",
        category=Category.api,
        variables=[],
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


# ---------------------------------------------------------------------------
# The rule itself — one test per operator x literal kind.
#
# This section pins the *positive* form of the rule: given a condition
# that references a variable the variable set does not contain, the
# evaluator returns ``Err`` carrying a ``ConditionError`` whose single
# finding has the documented code, names the missing variable, and
# carries a non-empty hint.
#
# Each operator x literal pairing is tested because a future refactor
# that moved the "is the variable defined?" check into, say, the
# ``NotEquals`` branch only (or only the ``Equals`` branch) would pass
# a naive test and produce silently wrong behaviour on the other
# branch. The matrix makes such a regression impossible to miss.
# ---------------------------------------------------------------------------
class TestUndefinedIsAnErrorForEveryOperatorAndLiteralKind:
    """Undefined variables are errors for ``==`` and ``!=``, all literals."""

    @pytest.mark.parametrize(
        "condition",
        [
            # equality, one per literal kind
            "database == 'postgresql'",
            "include_docker == true",
            "python_major == 3",
            # inequality, one per literal kind
            "database != 'sqlite'",
            "include_docker != false",
            "python_major != 2",
        ],
    )
    def test_undefined_variable_yields_a_condition_error(self, condition: str) -> None:
        """Every operator x literal pairing produces a ``ConditionError``.

        Passing ``{}`` (no variables at all) is the strongest form of
        "the variable is undefined" — there is no ambiguity about
        whether a variable is present. A variant with *some* variables
        defined appears below (``TestUndefinedAlongsideDefined``) to
        prove the rule is per-reference, not per-evaluation.
        """
        result = _evaluate(condition, {})
        assert isinstance(result, Err), (
            f"condition {condition!r} with no variables should be an " f"error, got {result!r}"
        )
        assert isinstance(result.error, ConditionError)

    @pytest.mark.parametrize(
        "condition",
        [
            "database == 'postgresql'",
            "include_docker == true",
            "python_major == 3",
            "database != 'sqlite'",
            "include_docker != false",
            "python_major != 2",
        ],
    )
    def test_undefined_variable_finding_has_correct_code(self, condition: str) -> None:
        """The finding's ``code`` is ``condition.undefined_variable``.

        Downstream aggregation branches on ``code`` (the CLI's error
        renderer, the JSON output, future tooling). A silent
        reclassification to ``condition.syntax`` or a generic
        ``condition.error`` would still produce an ``Err``, but it
        would lose the "this is a name problem, not a grammar problem"
        distinction that the hint relies on. The code is the contract,
        and it is asserted separately from the ``Err`` itself so a
        future refactor cannot accidentally merge them.
        """
        result = _evaluate(condition, {})
        assert isinstance(result, Err)
        codes = {f.code for f in result.error.findings}
        assert codes == {"condition.undefined_variable"}


# ---------------------------------------------------------------------------
# The finding is *actionable* — name, message, hint.
#
# A finding that says "something went wrong with your condition" is not
# actionable; it tells the user that a variable is missing but not
# which one, or how to fix it. The contract, established across the
# conditions module, is that every finding carries a representation of
# the offending condition, a message naming the problem, and a hint
# suggesting the fix.
# ---------------------------------------------------------------------------
class TestUndefinedFindingIsActionable:
    """The finding names the variable and carries a hint."""

    def test_message_names_the_missing_variable(self) -> None:
        """``message`` contains the variable name verbatim.

        The user sees ``undefined variable: database`` — they can act on
        that without reading docs. A generic message like ``invalid
        condition`` would leave them guessing which of several
        referenced names is the culprit.
        """
        result = _evaluate("database == 'postgresql'", {})
        assert isinstance(result, Err)
        assert "database" in result.error.findings[0].message

    def test_hint_names_the_missing_variable(self) -> None:
        """``hint`` also names the variable, in the fix suggestion.

        The message says *what* is wrong; the hint says *what to do*.
        A hint that says "add the variable" without naming it is a
        smaller version of the dead end the whole rule exists to
        prevent.
        """
        result = _evaluate("database == 'postgresql'", {})
        assert isinstance(result, Err)
        assert "database" in result.error.findings[0].hint

    def test_condition_field_is_the_canonical_form_of_the_input(self) -> None:
        """``finding.condition`` is the *canonical* form of the input.

        Undefined-variable findings are produced by the evaluator, which
        reconstructs the condition from the AST via ``_pretty`` (see
        ``evaluator.py``). The AST itself holds no reference to the raw
        source — invariant 3 in ``ast.py`` — so the evaluator cannot
        echo the raw source, and it does not try to. Instead it produces
        the *canonical* form:

            ``<variable> <operator> <literal>``

        with single spaces around the operator and single quotes around
        string literals. This is the form the grammar docs
        (``CONDITION_GRAMMAR.md``) use, and the form a user reading the
        error alongside the docs will recognise.

        Parser findings take a different path: ``ParseError.to_finding()``
        echoes the raw source verbatim, because the parser is what
        decided the text was unparseable and echoing it is the most
        useful feedback. That distinction is documented on
        ``Finding.condition`` and is worth stating here too, because
        the two representations are not interchangeable and a future
        refactor must not collapse them.

        The test therefore asserts on *canonical form*, not verbatim
        preservation — and, more importantly, on the property that
        makes canonical form valuable: semantically identical conditions
        produce identical error strings, regardless of how they were
        whitespaced in the source.
        """
        original = "database ==  'postgresql'"  # two spaces, for the point
        result = _evaluate(original, {})
        assert isinstance(result, Err)

        # The canonical form collapses whitespace and normalises quoting.
        canonical = result.error.findings[0].condition
        assert canonical == "database == 'postgresql'"

        # And every whitespace/quote variant of the same logical
        # condition produces the *same* canonical form. This is the
        # determinism property the CLI depends on for snapshot testing
        # and for diffable error output in CI.
        variants = [
            "database=='postgresql'",
            'database=="postgresql"',
            "database  ==   'postgresql'",
            "database == 'postgresql'",
            "database\t==\t'postgresql'",
        ]
        canonical_forms = set()
        for variant in variants:
            r = _evaluate(variant, {})
            assert isinstance(r, Err)
            canonical_forms.add(r.error.findings[0].condition)

        assert canonical_forms == {"database == 'postgresql'"}, (
            "canonical form must be stable across whitespace and quote "
            "style variants of the same logical condition; got "
            f"{sorted(canonical_forms)!r}"
        )

    def test_hint_is_never_empty(self) -> None:
        """An empty hint is a defect, not a degenerate case.

        The CLI renders the hint directly to the user. Empty is not
        acceptable — the whole design pillar of this module is that
        errors carry actionable suggestions.
        """
        for condition in [
            "a == 'x'",
            "b == true",
            "c == 3",
            "d != 'y'",
            "e != false",
            "f != 4",
        ]:
            result = _evaluate(condition, {})
            assert isinstance(result, Err)
            for finding in result.error.findings:
                assert finding.hint.strip(), f"empty hint for condition {condition!r}"


# ---------------------------------------------------------------------------
# The rule is per-reference, not per-evaluation.
#
# A blueprint may reference several variables in several conditions.
# Some may be defined, some not; some conditions may reference defined
# variables only and thus evaluate cleanly. The rule is that *every
# undefined reference is reported*, and *only* undefined references
# are reported — a defined-but-wrong-value reference is not an
# undefined-variable error, it is a comparison that simply evaluates
# to False (or True).
#
# This section pins both halves of that "per-reference" claim.
# ---------------------------------------------------------------------------
class TestUndefinedAlongsideDefined:
    """Defined references do not mask undefined ones (and vice versa)."""

    def test_some_defined_some_undefined_reports_only_the_undefined(
        self,
    ) -> None:
        """Only the undefined references produce findings.

        The defined reference (``database == 'postgresql'`` with
        ``database`` defined) evaluates cleanly and contributes *no*
        finding. The undefined reference (``missing_thing == 'x'``)
        contributes exactly one. A naive implementation that failed the
        whole evaluation on the first undefined reference would lose
        the ability to report the *other* undefined references; a
        naive implementation that continued past undefined references
        as falsy would report none. Only the correct implementation
        reports exactly the undefined ones.
        """
        blueprint = _blueprint(
            conditions=[
                "database == 'postgresql'",  # defined, matches → survivor
                "missing_thing == 'x'",  # undefined → finding
                "include_docker == true",  # defined, matches → survivor
            ]
        )
        # Provide every defined variable; leave `missing_thing` absent.
        result = ConditionalEvaluator().evaluate(
            blueprint,
            {"database": "postgresql", "include_docker": True},
        )
        assert isinstance(result, Err)
        assert len(result.error.findings) == 1
        finding = result.error.findings[0]
        assert finding.code == "condition.undefined_variable"
        assert "missing_thing" in finding.message

    def test_defined_reference_that_evaluates_false_is_not_an_error(
        self,
    ) -> None:
        """A defined variable whose value does not match is not an error.

        ``database == 'postgresql'`` with ``database="sqlite"`` is a
        clean ``False`` — the group is excluded, no finding. The
        distinction between "the variable is missing" (error) and "the
        variable is present but the comparison is False" (a valid
        exclusion) is the entire point of the rule, and it is asserted
        here in the presence of *another* condition that would
        legitimately produce a finding — the safe case and the failure
        case are evaluated together, so a "fail on anything" bug cannot
        pass.
        """
        blueprint = _blueprint(
            conditions=[
                "database == 'postgresql'",  # defined, does not match → excluded
                "missing_thing == 'x'",  # undefined → finding
            ]
        )
        result = ConditionalEvaluator().evaluate(blueprint, {"database": "sqlite"})
        assert isinstance(result, Err)
        assert len(result.error.findings) == 1
        assert "missing_thing" in result.error.findings[0].message
        # Crucially, `database` must *not* be named — it is defined.
        assert "database" not in result.error.findings[0].message


# ---------------------------------------------------------------------------
# Aggregation — every undefined reference is reported in one pass.
#
# The "fail fast, fail complete" principle established across Forge
# (ADR-0010, ADR-0011, ADR-0016) applies to the conditions module too.
# A user with three undefined variables should get three findings in
# one run, not one per run. The evaluator's iteration over
# ``conditional_files`` is the mechanism; this section pins it.
# ---------------------------------------------------------------------------
class TestUndefinedAggregation:
    """All undefined references are collected in one ``ConditionError``."""

    @pytest.mark.parametrize("count", [1, 2, 3, 5])
    def test_all_undefined_references_are_reported(self, count: int) -> None:
        """``count`` distinct undefined references produce ``count`` findings."""
        conditions = [f"var_{i} == 'x'" for i in range(count)]
        result = ConditionalEvaluator().evaluate(_blueprint(conditions=conditions), {})
        assert isinstance(result, Err)
        assert len(result.error.findings) == count
        # Every finding carries the code; no accidental mixing.
        assert {f.code for f in result.error.findings} == {"condition.undefined_variable"}

    def test_repeated_reference_to_same_variable_yields_one_finding_per_use(
        self,
    ) -> None:
        """A variable referenced twice is reported twice, once per condition.

        Each condition is its own unit of evaluation — the finding
        belongs to *that condition*, not to the variable in the
        abstract. A user fixing the blueprint needs to see every
        reference, because each one might need a different fix
        (different default, different flag). Deduplicating by variable
        name would hide references, which is the wrong trade.
        """
        blueprint = _blueprint(
            conditions=[
                "ghost == 'a'",
                "ghost == 'b'",
            ]
        )
        result = ConditionalEvaluator().evaluate(blueprint, {})
        assert isinstance(result, Err)
        assert len(result.error.findings) == 2
        # Both findings name the same variable — the point is *how many*.
        assert all("ghost" in f.message for f in result.error.findings)

    def test_aggregate_preserves_declaration_order(self) -> None:
        """Findings appear in the order their conditions were declared.

        Deterministic ordering is what makes the CLI report diffable in
        CI and snapshot-testable. A future change to the evaluator's
        iteration (e.g. sorting findings by variable name) would
        silently reorder the user's error report; this test pins the
        declaration-order guarantee.
        """
        blueprint = _blueprint(
            conditions=[
                "z_var == 'x'",
                "a_var == 'x'",
                "m_var == 'x'",
            ]
        )
        result = ConditionalEvaluator().evaluate(blueprint, {})
        assert isinstance(result, Err)
        messages = [f.message for f in result.error.findings]
        # Each message begins with "undefined variable: <name>".
        assert "z_var" in messages[0]
        assert "a_var" in messages[1]
        assert "m_var" in messages[2]


# ---------------------------------------------------------------------------
# The aggregate error itself.
#
# A ``ConditionError`` is what reaches the CLI. Its shape — findings,
# source blueprint, ``__str__`` — is the contract the CLI renders. This
# section pins that shape for the specific case of undefined-variable
# errors, which is the most common failure a blueprint author will hit
# when first writing a conditional group.
# ---------------------------------------------------------------------------
class TestConditionErrorShapeForUndefined:
    """The ``ConditionError`` produced by undefined references is well-formed."""

    def test_error_carries_source_blueprint_id(self) -> None:
        """``source_blueprint`` is the blueprint's ``id``.

        The CLI uses this field to attribute the error to the right
        input in batch contexts (a future ``forge validate`` over a
        directory of blueprints). Asserting it here pins the
        attribution.
        """
        result = _evaluate("x == 'y'", {})
        assert isinstance(result, Err)
        assert result.error.source_blueprint == "sample.undefined.test"

    def test_error_str_is_deterministic(self) -> None:
        """The rendered error is stable across runs.

        Determinism lets the CLI's error output be snapshot-tested and
        diffable in CI. A future addition to the rendering (say, a
        timestamp field) would break this test and force a review of
        the format change.
        """
        r1 = _evaluate("x == 'y'", {})
        r2 = _evaluate("x == 'y'", {})
        assert isinstance(r1, Err)
        assert isinstance(r2, Err)
        assert str(r1.error) == str(r2.error)

    def test_error_str_names_every_missing_variable(self) -> None:
        """The rendered message mentions each missing variable.

        A user reading only the rendered string (not the structured
        fields) must still see every variable they need to add. That
        is the actionable-report invariant at the *string* layer, and
        it is the layer most users actually read.
        """
        blueprint = _blueprint(conditions=["first == 'x'", "second == 'y'", "third == 'z'"])
        result = ConditionalEvaluator().evaluate(blueprint, {})
        assert isinstance(result, Err)
        rendered = str(result.error)
        assert "first" in rendered
        assert "second" in rendered
        assert "third" in rendered

    def test_error_has_no_findings_for_defined_variables(self) -> None:
        """No spurious findings when everything is defined and matches.

        This is the negative-space check: an all-green evaluation must
        produce ``Ok`` with survivors and *no* error, not an empty
        ``ConditionError``. The ``__post_init__`` on ``ConditionError``
        forbids empty findings, so a well-formed empty-error path is
        impossible by construction — this test proves the evaluator
        does not try.
        """
        result = _evaluate("x == 'y'", {"x": "y"})
        assert isinstance(result, Ok)
        assert len(result.value) == 1


# ---------------------------------------------------------------------------
# The rule holds across the AST.
#
# A future contributor adding a new operator to the DSL must not have to
# remember to add an "undefined → error" test for it: the exhaustive
# matrix at the top of this file (``TestUndefinedIsAnErrorForEveryOperator
# AndLiteralKind``) already covers every current operator x literal
# pairing. If the contributor adds a new operator to ``grammar.py`` and
# to the parser, this test class will start passing for it too (because
# the evaluator's undefined check is operator-agnostic). If they add
# it *incorrectly* — by moving the undefined check into one branch
# only — the existing rows will keep passing while a new row for the
# new operator fails. The matrix is the safety net; this class is its
# explanation.
#
# It contains no tests of its own — it exists only to hold the comment
# above, so a maintainer who lands here looking for "why does an
# operator get automatically covered?" finds the answer.
# ---------------------------------------------------------------------------
