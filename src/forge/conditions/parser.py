"""Hand-written parser for the condition DSL — no eval, no ast, no shortcuts.

Why hand-written?
-----------------
* **No `eval` / `exec`.** Blueprint content is untrusted input. `eval`
  is how "declarative conditions" quietly become "arbitrary code".
* **No `ast.parse` / `ast.literal_eval`.** Both accept a *superset* of
  our grammar; using them means reviewing the delta forever. Here, the
  accepted language is exactly what `grammar.GRAMMAR` declares — no more,
  no less.
* **No parser generator.** The language has one operator pair and three
  literal kinds; a dependency and a build step would cost more than they
  save, and generated parsers are harder to reason about than a
  sixty-line function a reviewer can read in one screen.

Design contract
---------------
`parse()` is **total**:

    parse(text) -> Expression | ParseError

It never raises, never returns `None`, never leaves partial state. The
caller decides what to do with a `ParseError` (usually aggregate it into
a `ConditionError` alongside other findings). That totality is what
makes the evaluator trivially testable and what keeps error reporting
batch-friendly.

Layering
--------
    grammar.py   → the *data* (rules, regexes, families)
    parser.py    → regex match + literal coercion → typed AST  (this file)
    evaluator.py → comparison against resolved variables
    errors.py    → structured error shape shared with the CLI

Each layer knows nothing about the layers above it. Adding an operator
means editing `grammar.py`, then *this* file's `_coerce_literal`, then
tests and docs — all visible in one PR.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from forge.conditions.ast import Equals, Expression, NotEquals
from forge.conditions.errors import Finding
from forge.conditions.grammar import (
    GRAMMAR,
    VARIABLE_NAME_PATTERN,
    rule_families,
)


# ---------------------------------------------------------------------------
# Result type for parsing
#
# We reuse the app-wide "failure is data" philosophy: parse either yields
# an `Expression` (typed AST node) or a `ParseError` (which knows how to
# become a `Finding` for the aggregate `ConditionError`). No exceptions
# cross the parser boundary, ever.
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ParseError:
    """A condition that could not be parsed into the DSL's AST."""

    condition: str
    message: str
    hint: str
    code: str

    def to_finding(self) -> Finding:
        """Adapt to the app-wide structured error shape.

        Kept explicit rather than letting the parser import `ConditionError`
        directly — that would couple parsing to aggregation and make the
        parser harder to test in isolation.
        """
        return Finding(
            condition=self.condition,
            message=self.message,
            hint=self.hint,
            code=self.code,
        )


# ---------------------------------------------------------------------------
# Module-level caches
#
# * `_VARIABLE_RE` — the identifier regex from the grammar (single source).
# * `_COMPILED` — one compiled regex per rule. Compiled once at import;
#   `re.match` on a compiled pattern is significantly cheaper than on a
#   string, and condition parsing happens for every conditional file
#   group on every run.
# * `_FAMILIES` — rule name → family, so `_coerce_literal` never
#   hardcodes rule names (renaming a rule cannot change semantics).
# ---------------------------------------------------------------------------
_VARIABLE_RE = re.compile(VARIABLE_NAME_PATTERN)
_COMPILED: tuple[tuple[str, re.Pattern[str], str], ...] = tuple(
    (rule.name, re.compile(rule.pattern), rule.family) for rule in GRAMMAR
)
_FAMILIES = rule_families()


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def parse(condition: str | None) -> Expression | ParseError:
    """Parse one condition into a typed AST node, or return a ParseError.

    The function is total: every input yields a defined outcome. Precedence:

        1. ``None`` (YAML ``condition:`` with no value)
             -> ParseError("condition.empty")
        2. Empty / whitespace-only string
             -> ParseError("condition.empty")
        3. Full match on a grammar
             -> Equals / NotEquals
        4. Everything else
             -> ParseError("condition.syntax")

    Rule *order* is preserved from `GRAMMAR`; the first full match wins.
    That means `!=` forms are considered before any partial-match rule
    (already ordered correctly in `GRAMMAR`), so a condition like
    ``x != 'y'`` cannot accidentally fall into an equality branch.

    Why the parameter is ``str | None`` and not ``str``:

    The parser sits at the boundary between *user-authored YAML* and the
    typed AST. A YAML key like ``condition:`` with no value parses to
    ``None`` — not a ``str`` — and the evaluator forwards it verbatim
    before any validation has a chance to reject it. The annotation
    therefore *declares* the runtime reality: the parser's actual domain
    includes ``None``.

    An earlier version of this function annotated the parameter as
    ``str`` and used a runtime ``isinstance(condition, str)`` guard to
    catch ``None`` anyway. That worked at runtime, but mypy strict
    correctly flagged the guard as ``unreachable`` — the annotation
    promised ``str``, so the branch *was* dead code from the type
    checker's perspective. The fix is not to suppress the warning: the
    warning is telling the truth about an inconsistency between the
    declared domain and the runtime domain. The fix is to *align the
    declared domain with the runtime one*, by widening the annotation.

    Note that widening to ``str | None`` does **not** impose a cost on
    callers. Python's type system allows a caller holding a ``str`` to
    pass it to a ``str | None`` parameter without a cast, so existing
    call sites (which all pass ``str``) type-check unchanged. The
    widening is local to this signature and expresses exactly what the
    parser must handle at its boundary.

    The evaluator's callers, in turn, remain fully ``str``-typed — the
    parser is the *only* place the non-``str`` case is visible, and it
    is visible there because that is the only place it *occurs*.
    """
    if condition is None:
        return ParseError(
            condition="",
            message="condition is missing (null/None)",
            hint="Provide a condition string, e.g. `include_docker == true`.",
            code="condition.empty",
        )

    if not condition.strip():
        return ParseError(
            condition=condition,
            message="condition is empty",
            hint="Provide a condition like `include_docker == true`.",
            code="condition.empty",
        )

    for rule_name, pattern, family in _COMPILED:
        match = pattern.match(condition)  # compiled with ^...$ anchors
        if match is None:
            continue

        variable = match.group("var")
        raw_value = match.group("value")

        # Defence in depth: the grammar regex already enforces the
        # variable shape, but if `GRAMMAR` is ever loosened by mistake,
        # this guard keeps the AST's invariant ("variable is a valid
        # identifier") intact. Cheaper than discovering bad nodes
        # downstream.
        if not _VARIABLE_RE.match(variable):
            return ParseError(
                condition=condition,
                message=f"invalid variable name: {variable!r}",
                hint=(
                    "Variable names are lowercase identifiers: letters, "
                    "digits, and underscores, starting with a letter or "
                    "underscore."
                ),
                code="condition.invalid_variable",
            )

        literal = _coerce_literal(rule_name, family, raw_value)
        if isinstance(literal, ParseError):  # pragma: no cover - belt & braces
            # Only reachable if a future rule's family coercion fails.
            # Returning it preserves the parser's totality guarantee.
            return literal

        # Operator selection is driven by *family + rule name*, not by
        # re-inspecting the raw string. Rule names are part of the
        # grammar's public surface, so this stays stable.
        if rule_name.startswith("variable_not_equals"):
            return NotEquals(variable=variable, literal=literal)
        return Equals(variable=variable, literal=literal)

    # No rule matched: the input is syntactically valid Python-ish but
    # not in our DSL. The hint enumerates the language, so users can
    # self-correct without consulting the docs.
    return ParseError(
        condition=condition,
        message="condition does not match any supported syntax",
        hint=(
            "Supported forms: `var == 'string'`, `var != 'string'`, "
            "`var == true|false`, `var == <integer>`. No and/or, no "
            "arithmetic, no function calls."
        ),
        code="condition.syntax",
    )


# ---------------------------------------------------------------------------
# Literal coercion
# ---------------------------------------------------------------------------
def _coerce_literal(
    rule_name: str,
    family: str,
    raw: str,
) -> str | bool | int | ParseError:
    """Turn the raw captured literal into a typed Python value.

    The `family` field on `GrammarRule` is the *authority* for coercion
    — not the rule name. Renaming a rule (e.g. for tidier docs) is
    therefore a safe refactor: only `GRAMMAR` names change, and
    semantics are untouched.

    ``family`` values are validated eagerly: an unknown family returns a
    ``ParseError`` rather than raising, preserving totality. This is a
    bug in the grammar, not user error, but surfacing it through the
    same channel keeps the pipeline uniform.
    """
    if family == "bool":
        # Grammar guarantees exactly "true" or "false" here; no other
        # spellings reach this branch. The strict lookup keeps us from
        # silently accepting "TRUE", "1", "yes", etc. — those are
        # *deliberately* syntax errors, documented in CONDITION_GRAMMAR.md.
        return {"true": True, "false": False}[raw]

    if family == "int":
        # Grammar guarantees an optional sign and digits; `int()` is
        # sufficient. We still wrap in try/except to keep totality if the
        # regex is ever loosened by mistake.
        try:
            return int(raw, 10)
        except ValueError:  # pragma: no cover - defensive
            return ParseError(
                condition=f"<internal:{rule_name}>",
                message=f"could not parse integer literal: {raw!r}",
                hint="This is a bug in the conditions grammar.",
                code="condition.internal",
            )

    if family == "string":
        # Quotes are already stripped by the regex groups; `raw` is the
        # exact content between them. No escape processing — escapes are
        # a rabbit hole and blueprint conditions have no need for them.
        return raw

    # Unreachable with the shipped grammar; kept for totality.
    return ParseError(  # pragma: no cover - defensive
        condition=f"<internal:{rule_name}>",
        message=f"unknown literal family: {family!r}",
        hint="This is a bug in the conditions grammar.",
        code="condition.internal",
    )


# ---------------------------------------------------------------------------
# Parser invariants (documented here so reviewers can check them at a glance)
# ---------------------------------------------------------------------------
#
# 1. Totality
#    Every input yields `Expression | ParseError`. No exceptions.
#    Verified by property-style tests in `test_parser.py`.
#
# 2. Grammar fidelity
#    The set of accepted strings is *exactly* the set matched by the
#    union of `GRAMMAR` patterns. No pre-/post-processing widens the
#    language. Verified by round-trip tests over the grammar examples
#    and rejection tests over a corpus of invalid strings.
#
# 3. AST purity
#    Parsed nodes contain no references to the original string, no
#    parser state, and no mutable containers. They are frozen dataclasses
#    so downstream caching / equality are safe.
#
# 4. Determinism
#    Same input → same AST (structural equality), forever. Rule order in
#    `GRAMMAR` is fixed; `_COMPILED` is built once at import.
#
# 5. No capability gain
#    No call to `eval`, `exec`, `ast`, `importlib`, or the filesystem
#    from this module. Grep-enforced by a lint rule in CI
#    (see `scripts/lint_no_eval.py`).
#
# If any invariant needs to change, it must change in this docstring
# first and then be implemented — keeping the spec and the code in one
# file, reviewed together.
