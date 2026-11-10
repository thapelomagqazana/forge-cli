"""The formal grammar for conditions — the *one* source of truth.

Why keep the grammar as *data* and not prose or scattered regexes?
------------------------------------------------------------------
1. **Auditability.** A reviewer can see every supported form in one
   screen. Anything not in `GRAMMAR` is a syntax error by definition.
2. **Doc-sync.** `docs/reference/CONDITION_GRAMMAR.md` is tested against
   this module (see `tests/unit/conditions/test_grammar_docs.py`), so the
   documentation cannot silently drift from the parser.
3. **Extensibility with friction.** Adding an operator means editing this
   file, the parser, the tests, and the doc — all visible in one PR.
   That friction is the point: expressiveness grows deliberately, not
   by accident.
4. **Tooling.** Future autocompletion, error hints, and migration
   tooling can read the grammar without re-implementing it.

Grammar shape
-------------
The language is deliberately tiny — one operator per condition, no
`and`/`or`, no arithmetic, no function calls. Each rule is a full-match
regex with two named groups:

    var    — the variable name, always `[a-z_][a-z0-9_]*`
    value  — the literal, interpreted according to the rule family
             (string / bool / int) by the parser.

Rule *order* matters for parsing: the parser iterates top-to-bottom and
returns the first full match. `!=` forms must be listed before any rule
that could partially match them; quoted-string variants are listed
before the bare-token variants for the same reason.

Safety note
-----------
These regexes never execute blueprint content — they only *match* it.
The parser (not this module) turns matches into typed AST nodes, and the
evaluator (not this module) compares values. Keeping matching, parsing,
and evaluation separate is what makes the whole DSL auditable in one
sitting.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

# ---------------------------------------------------------------------------
# Variable name grammar — shared with `VariableDefinition.id`.
#
# Keeping these identical is what lets the evaluator distinguish
# "well-formed reference to a variable that doesn't exist" (a
# `condition.undefined_variable` error) from "typo that isn't even a
# valid identifier" (a `condition.syntax` error). That distinction is the
# difference between an actionable hint and a confusing one.
# ---------------------------------------------------------------------------
VARIABLE_NAME_PATTERN: Final[str] = r"^[a-z_][a-z0-9_]*$"


# ---------------------------------------------------------------------------
# The complete, ordered grammar.
#
# Every rule has a stable `name` — tests and docs reference rules by name,
# so renaming a rule is an intentional, visible change.
#
# The `pattern` is an *anchored full-match* regex. Group names are fixed:
#   var   — required
#   value — required
# Anything else (and/or, operators like <, function calls) is unmatched
# by design and falls through to a syntax error.
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class GrammarRule:
    """One supported condition form.

    Attributes
    ----------
    name
        Stable identifier used by tests, docs, and error messages.
    pattern
        Anchored regex with named groups ``var`` and ``value``.
    example
        A minimal, canonical example. Rendered in the doc and asserted
        by the doc-sync test.
    description
        Human-readable one-liner used by the doc generator.
    family
        One of ``"string"``, ``"bool"``, ``"int"``. Tells the parser how
        to coerce ``value`` — never inferred from the rule name, so
        renaming a rule cannot change semantics.
    """

    name: str
    pattern: str
    example: str
    description: str
    family: str  # "string" | "bool" | "int"


GRAMMAR: tuple[GrammarRule, ...] = (
    # -- String equality (single-quoted) -----------------------------------
    GrammarRule(
        name="variable_equals_string_single",
        pattern=r"^\s*(?P<var>[a-z_][a-z0-9_]*)\s*==\s*'(?P<value>[^']*)'\s*$",
        example="database == 'postgresql'",
        description="Equality between a variable and a single-quoted string.",
        family="string",
    ),
    # -- String equality (double-quoted, semantically identical) ----------
    GrammarRule(
        name="variable_equals_string_double",
        pattern=r'^\s*(?P<var>[a-z_][a-z0-9_]*)\s*==\s*"(?P<value>[^"]*)"\s*$',
        example='database == "postgresql"',
        description="Equality between a variable and a double-quoted string.",
        family="string",
    ),
    # -- String inequality (single-quoted) --------------------------------
    GrammarRule(
        name="variable_not_equals_string_single",
        pattern=r"^\s*(?P<var>[a-z_][a-z0-9_]*)\s*!=\s*'(?P<value>[^']*)'\s*$",
        example="database != 'sqlite'",
        description="Inequality between a variable and a single-quoted string.",
        family="string",
    ),
    # -- String inequality (double-quoted) --------------------------------
    GrammarRule(
        name="variable_not_equals_string_double",
        pattern=r'^\s*(?P<var>[a-z_][a-z0-9_]*)\s*!=\s*"(?P<value>[^"]*)"\s*$',
        example='database != "sqlite"',
        description="Inequality between a variable and a double-quoted string.",
        family="string",
    ),
    # -- Boolean equality --------------------------------------------------
    # Lowercase on purpose: matches YAML/JSON conventions so the same
    # blueprint behaves identically across tools. Uppercase `True`/`False`
    # is a syntax error — a documented, tested choice (see CONDITION_GRAMMAR.md).
    GrammarRule(
        name="variable_equals_bool",
        pattern=r"^\s*(?P<var>[a-z_][a-z0-9_]*)\s*==\s*(?P<value>true|false)\s*$",
        example="include_docker == true",
        description="Boolean equality; values are lowercase (matching YAML/JSON).",
        family="bool",
    ),
    # -- Boolean inequality ------------------------------------------------
    GrammarRule(
        name="variable_not_equals_bool",
        pattern=r"^\s*(?P<var>[a-z_][a-z0-9_]*)\s*!=\s*(?P<value>true|false)\s*$",
        example="include_docker != true",
        description="Boolean inequality; values are lowercase.",
        family="bool",
    ),
    # -- Integer equality --------------------------------------------------
    # Ints only — no floats. Float equality is a footgun and the DSL gains
    # nothing from it (blueprints branch on category-like values, not
    # measurements).
    GrammarRule(
        name="variable_equals_int",
        pattern=r"^\s*(?P<var>[a-z_][a-z0-9_]*)\s*==\s*(?P<value>-?\d+)\s*$",
        example="python_major == 3",
        description="Integer equality (no floats; fewer surprises).",
        family="int",
    ),
    # -- Integer inequality ------------------------------------------------
    GrammarRule(
        name="variable_not_equals_int",
        pattern=r"^\s*(?P<var>[a-z_][a-z0-9_]*)\s*!=\s*(?P<value>-?\d+)\s*$",
        example="python_major != 2",
        description="Integer inequality.",
        family="int",
    ),
)


# ---------------------------------------------------------------------------
# Public helpers
#
# Kept tiny and side-effect-free. Downstream consumers (parser, tests,
# doc-sync) read the grammar through these helpers so the module's shape
# stays free to evolve without breaking them.
# ---------------------------------------------------------------------------
def rule_families() -> dict[str, str]:
    """Return ``{rule_name: family}`` for parser dispatch.

    The parser uses this to pick a coercion strategy without hardcoding
    rule names — if a rule is renamed, only its entry in ``GRAMMAR``
    changes.
    """
    return {rule.name: rule.family for rule in GRAMMAR}


def documented_examples() -> dict[str, str]:
    """Return ``{rule_name: example}`` for the doc-sync test and docs.

    The doc test asserts every example appears in
    ``docs/reference/CONDITION_GRAMMAR.md``; adding a rule without
    updating the doc therefore fails CI, not code review.
    """
    return {rule.name: rule.example for rule in GRAMMAR}


def supported_operators() -> frozenset[str]:
    """Return the set of comparison operators the grammar exposes.

    Kept as a function (not a constant) so the operators are derived from
    the rules themselves — the grammar stays the *one* source of truth.
    """
    ops: set[str] = set()
    for rule in GRAMMAR:
        # Operator lives between the variable and the literal in every
        # rule; the pattern is the authority, not a parallel table.
        if "!=" in rule.pattern:
            ops.add("!=")
        if "==" in rule.pattern:
            ops.add("==")
    return frozenset(ops)
