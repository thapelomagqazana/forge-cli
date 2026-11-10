"""Condition evaluation against resolved variables.

Role
----
This module turns parsed condition AST nodes into a verdict: *include the
group* or *skip the group*. It does nothing else: no parsing, no I/O, no
filesystem, no template rendering. Its entire input is:

    * a blueprint's ``conditional_files`` list, and
    * the resolved variable mapping produced by ``VariableResolver``.

Its entire output is either the list of surviving groups (each carrying
its already-parsed AST so downstream code never re-parses) or a
``ConditionError`` that aggregates every finding in one pass — matching
the ``Result`` discipline used by the loader, resolver, and planner.

Semantics (documented in CONDITION_GRAMMAR.md, tested exhaustively)
-------------------------------------------------------------------
* **Type-aware comparison.** ``"3" == 3`` is ``False`` and ``True == 1``
  is ``False``. Silent cross-type equality is the root of "why did my
  boolean blueprint match an integer check?" bugs, and we refuse to have
  them.
* **Undefined variables are errors, not falsy.** A condition referencing
  a variable the blueprint never declared — or one that wasn't resolved —
  is a blueprint bug, and we surface it loudly instead of silently
  enabling or disabling a feature.
* **Totality.** Every condition yields either a boolean verdict or a
  ``Finding``. No other outcome is possible; no exceptions escape.

Deliberate non-goals
--------------------
* **No short-circuiting across groups.** Every group is evaluated even if
  an earlier one errors, so the user gets *every* finding in one run.
* **No caching.** The evaluator is a pure function over its inputs;
  caching belongs to the caller if it ever needs it.
* **No mutation.** The blueprint and the variable mapping are read-only.
  Group ordering is preserved from the blueprint so the plan stays
  deterministic.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from forge.conditions.ast import (
    OPERATOR_EQUALS,
    OPERATOR_NOT_EQUALS,
    Equals,
    Expression,
    NotEquals,
)
from forge.conditions.errors import ConditionError, Finding
from forge.conditions.parser import ParseError, parse
from forge.loading.result import Err, Ok, Result

# ---------------------------------------------------------------------------
# Public type aliases
#
# Naming the intermediate types keeps `evaluate`'s signature readable at
# call sites and lets future refactors (e.g. returning a richer survival
# record) change one alias, not every caller.
# ---------------------------------------------------------------------------
SurvivingGroup = tuple[Expression, list[Any]]
"""A conditional-files group that survived evaluation.

The first element is the *parsed* AST node, so downstream code (planner,
dry-run, logging) never re-parses the condition string. The second
element is the group's file entries, in the order the blueprint declared
them — preserved for determinism.
"""

EvaluationResult = Result[list[SurvivingGroup], ConditionError]


class ConditionalEvaluator:
    """Evaluate blueprint condition groups against resolved variables.

    Stateless. Safe to reuse across calls and threads. All policy — the
    grammar, the comparison rules, the error shape — lives in sibling
    modules; this class exists to orchestrate them in one obvious order.
    """

    def evaluate(
        self,
        blueprint: Any,
        variables: Mapping[str, Any],
    ) -> EvaluationResult:
        """Return the surviving groups, or a ``ConditionError``.

        Parameters
        ----------
        blueprint
            A validated ``forge.models.Blueprint``. Only
            ``conditional_files`` and ``id`` are read; anything else is
            left untouched. Read-only.
        variables
            The resolved variable mapping from ``VariableResolver``.
            Values are already typed (``str``, ``bool``, ``int``, etc.);
            the evaluator never coerces them.

        Returns
        -------
        Ok(list[SurvivingGroup])
            Groups whose condition evaluated ``True``. Order matches the
            blueprint's declaration order, so the plan downstream is
            deterministic.
        Err(ConditionError)
            Every parse error and every undefined-variable error found
            during this run, aggregated. No group is silently dropped.
        """
        findings: list[Finding] = []
        survivors: list[SurvivingGroup] = []

        # `conditional_files` may be missing or empty on a blueprint with
        # no conditional groups — treat both identically. Using `or []`
        # instead of a bare `getattr(..., [])` covers Pydantic models
        # where the field default is `[]` but may be `None` from a future
        # schema change.
        groups = getattr(blueprint, "conditional_files", None) or []

        for group in groups:
            condition_str = getattr(group, "condition", "")
            files = list(getattr(group, "files", []) or [])

            parsed = parse(condition_str)
            if isinstance(parsed, ParseError):
                findings.append(parsed.to_finding())
                continue

            verdict = self._evaluate(parsed, variables)
            if isinstance(verdict, Finding):
                findings.append(verdict)
                continue

            if verdict:
                survivors.append((parsed, files))

        if findings:
            return Err(
                ConditionError(
                    findings=tuple(findings),
                    source_blueprint=getattr(blueprint, "id", "<blueprint>"),
                )
            )
        return Ok(survivors)

    # -----------------------------------------------------------------
    # Internals
    # -----------------------------------------------------------------

    def _evaluate(
        self,
        expr: Expression,
        variables: Mapping[str, Any],
    ) -> bool | Finding:
        """Evaluate a single AST node to a boolean, or report a finding.

        Total: every AST node kind (there are two) yields a boolean or a
        ``Finding``. An unknown node kind cannot be produced by the
        parser; if one appears, it means the AST was constructed by hand
        somewhere else, which the module boundary forbids.
        """
        if expr.variable not in variables:
            return Finding(
                condition=self._pretty(expr),
                message=f"undefined variable: {expr.variable}",
                hint=(
                    f"Add '{expr.variable}' to the blueprint's variables, "
                    "or provide a value at create time."
                ),
                code="condition.undefined_variable",
            )

        actual = variables[expr.variable]
        expected = expr.literal

        # Type-aware comparison — see module docstring. When types are
        # incompatible, equality is False and inequality is True; this is
        # the rule that stops `True == 1` and `"3" == 3` from being
        # surprising.
        same_type = _type_compatible(actual, expected)
        matches = same_type and actual == expected

        if isinstance(expr, NotEquals):
            return not matches
        if isinstance(expr, Equals):
            return matches

        # Exhaustive in theory; reachable only if the AST gains a node
        # kind without this function being updated. Kept as a Finding
        # rather than an exception so the pipeline stays total when that
        # happens.
        #
        # The ``# type: ignore[unreachable]`` is deliberately narrow: it
        # suppresses the exact warning that fires because ``Expression``
        # is currently ``Equals | NotEquals``, and only that warning.
        # When the AST gains a third node kind, ``Expression`` widens,
        # mypy no longer narrows ``expr`` to ``Never`` after the two
        # ``isinstance`` branches, the ignore becomes unused, and
        # mypy's ``unused-ignore`` check (enabled under strict mode)
        # will force the marker off in the same PR that adds the node
        # — which is precisely the "force a review at the type-checker
        # layer" property we want.
        return Finding(  # type: ignore[unreachable]
            condition=self._pretty(expr),
            message="unknown expression kind",
            hint="This is a bug in the conditions module.",
            code="condition.internal",
        )

    def _pretty(self, expr: Expression) -> str:
        """Reconstruct the condition string for error messages.

        Reconstructed rather than stored so the AST holds no reference to
        user input (invariant 3 in ast.py). The output matches the
        canonical form the user likely wrote, so error messages read
        naturally.
        """
        if expr.kind == OPERATOR_NOT_EQUALS:
            op = "!="
        elif expr.kind == OPERATOR_EQUALS:
            op = "=="
        else:  # pragma: no cover - AST guarantees only two kinds
            op = "=="
        literal = expr.literal
        if isinstance(literal, bool):
            return f"{expr.variable} {op} {str(literal).lower()}"
        if isinstance(literal, str):
            return f"{expr.variable} {op} '{literal}'"
        return f"{expr.variable} {op} {literal}"


# ---------------------------------------------------------------------------
# Type compatibility
#
# This is the single place that decides whether two values are *comparable*
# for our DSL. Centralising it means:
#
# * the rule is unit-testable in isolation,
# * the evaluator's two branches (Equals, NotEquals) cannot diverge,
# * adding a new literal kind (if ever) touches exactly this function.
# ---------------------------------------------------------------------------
def _type_compatible(actual: object, expected: object) -> bool:
    """Return True iff `actual` and `expected` may be compared for equality.

    Special-cased: ``bool`` is a subclass of ``int`` in Python, so a naive
    ``isinstance(actual, type(expected))`` would wrongly accept
    ``True == 1``. We explicitly require both sides to be booleans when
    either side is a boolean.

    Everything else follows the rule "same runtime type". This is stricter
    than Python's ``==`` and is deliberate: a user passing ``--flag 3`` on
    the command line gets a string, and it must not silently compare equal
    to an integer condition.
    """
    if isinstance(actual, bool) or isinstance(expected, bool):
        return isinstance(actual, bool) and isinstance(expected, bool)
    return isinstance(actual, type(expected))


__all__ = ["ConditionalEvaluator", "EvaluationResult", "SurvivingGroup"]
