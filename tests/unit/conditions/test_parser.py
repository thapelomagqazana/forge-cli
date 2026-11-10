"""Unit tests for `forge.conditions.parser`.

Scope
-----
This module locks the *parser*'s contract in isolation from the evaluator
and the error-aggregation layer. It does not exercise the blueprint
pipeline; that is what the integration tests are for. It does exercise:

    * every grammar form the DSL documents,
    * the parser's *totality* (every string yields a value; nothing raises),
    * the parser's *fidelity* (accepted inputs are exactly the grammar),
    * the parser's *defence in depth* (variable-name re-check),
    * the parser's error *shape* (every ParseError carries condition,
      message, hint, code),
    * the parser's *no-code-execution* posture (no eval/exec/ast in the
      import graph reached by this module).

Test philosophy
---------------
* **Positive tests are parameterised.** One row per grammar rule, so
  adding a rule surfaces as an incomplete test-matrix — you *cannot*
  forget to add a positive test when you add a grammar rule without CI
  noticing, because `test_every_grammar_rule_has_a_positive_case` fails.

* **Negative tests are parameterised too.** Every rejected input is a
  row. Adding "and we also reject X" is a one-line change rather than a
  new test function, which keeps the rejection corpus visible in one
  place and cheap to grow.

* **Structural tests are separate and *satisfiable*.** The "no eval/exec/
  ast" checks inspect (a) the parser module's own ``__dict__`` — the
  authoritative runtime check — and (b) the parser's source file with a
  precise regex (negative lookbehind for ``re.compile``). Neither test
  asserts something that is a property of the *interpreter* rather than
  of the *parser*: ``sys.modules`` always contains ``ast`` after any
  ``import re``, so "does ``ast`` appear in ``sys.modules``?" would fail
  for *any* correct parser and cannot distinguish a bug from a fact of
  life. See ADR-0016 for the intent these tests protect.

* **Nothing here imports `forge.conditions`' façade.** The parser is
  imported directly. Tests of the façade's stability live in
  `test_public_api.py`, not here. This file's dependency graph is
  deliberately minimal so it stays fast and so a bug in the façade
  cannot make the parser tests fail for the wrong reason.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from forge.conditions.ast import Equals, NotEquals
from forge.conditions.errors import Finding
from forge.conditions.grammar import GRAMMAR, VARIABLE_NAME_PATTERN, documented_examples
from forge.conditions.parser import ParseError, parse

# ---------------------------------------------------------------------------
# Positive cases — one row per grammar rule.
# ---------------------------------------------------------------------------
_VALID_CASES: tuple[tuple[str, Equals | NotEquals], ...] = (
    # string equality
    ("database == 'postgresql'", Equals(variable="database", literal="postgresql")),
    ('database == "postgresql"', Equals(variable="database", literal="postgresql")),
    # string inequality
    ("database != 'sqlite'", NotEquals(variable="database", literal="sqlite")),
    ('database != "sqlite"', NotEquals(variable="database", literal="sqlite")),
    # boolean equality (lowercase only)
    ("include_docker == true", Equals(variable="include_docker", literal=True)),
    ("include_docker == false", Equals(variable="include_docker", literal=False)),
    # boolean inequality
    ("include_docker != true", NotEquals(variable="include_docker", literal=True)),
    ("include_docker != false", NotEquals(variable="include_docker", literal=False)),
    # integer equality (signed)
    ("python_major == 3", Equals(variable="python_major", literal=3)),
    ("python_major == -1", Equals(variable="python_major", literal=-1)),
    # integer inequality
    ("python_major != 2", NotEquals(variable="python_major", literal=2)),
    ("python_major != -2", NotEquals(variable="python_major", literal=-2)),
)


@pytest.mark.parametrize(("text", "expected"), _VALID_CASES)
def test_valid_condition_parses_to_expected_ast(text: str, expected: Equals | NotEquals) -> None:
    """Each grammar form produces exactly the typed AST node documented."""
    assert parse(text) == expected


@pytest.mark.parametrize(("text", "expected"), _VALID_CASES)
def test_valid_condition_has_correct_kind_tag(text: str, expected: Equals | NotEquals) -> None:
    """The `kind` tag on each node is the stable operator discriminator."""
    result = parse(text)
    assert not isinstance(result, ParseError)
    assert result.kind == expected.kind


def test_every_grammar_rule_has_a_positive_case() -> None:
    """Every rule in ``GRAMMAR`` is exercised by at least one valid case."""
    covered_rule_families = (
        {("string", node.kind) for _, node in _VALID_CASES if isinstance(node.literal, str)}
        | {("bool", node.kind) for _, node in _VALID_CASES if isinstance(node.literal, bool)}
        | {
            ("int", node.kind)
            for _, node in _VALID_CASES
            if isinstance(node.literal, int) and not isinstance(node.literal, bool)
        }
    )

    required = {
        (rule.family, "equals" if "not_equals" not in rule.name else "not_equals")
        for rule in GRAMMAR
    }

    missing = required - covered_rule_families
    assert not missing, (
        "grammar rules without a positive test case: "
        f"{sorted(missing)}. Add rows to _VALID_CASES covering each."
    )


# ---------------------------------------------------------------------------
# Negative cases — every rejection is a row.
#
# NOTE: `_private == 'x'` is deliberately *absent* here. Leading underscores
# are valid identifiers per the grammar (matching Python's own convention),
# so that input parses successfully. The intent of a former row that put it
# here — "if we ever tighten the rule, notice" — is now served by
# `test_leading_underscore_is_accepted_by_design` below.
# ---------------------------------------------------------------------------
_INVALID_CASES: tuple[tuple[str, str], ...] = (
    # -- empty / missing ---------------------------------------------------
    ("", "condition.empty"),
    ("   ", "condition.empty"),
    ("\t\n", "condition.empty"),
    # -- operator mistakes -------------------------------------------------
    ("database = 'postgresql'", "condition.syntax"),
    ("database === 'postgresql'", "condition.syntax"),
    ("database <> 'postgresql'", "condition.syntax"),
    ("database", "condition.syntax"),
    # -- literal problems --------------------------------------------------
    ("database == postgresql", "condition.syntax"),
    ("database == 'unterminated", "condition.syntax"),
    ("database == 'a' or 'b'", "condition.syntax"),
    ("database == True", "condition.syntax"),
    ("database == False", "condition.syntax"),
    ("database == 3.14", "condition.syntax"),
    ("database == 0x10", "condition.syntax"),
    ("database == 1_000", "condition.syntax"),
    # -- unsupported operators --------------------------------------------
    ("database >= 3", "condition.syntax"),
    ("database > 3", "condition.syntax"),
    ("database <= 3", "condition.syntax"),
    ("database < 3", "condition.syntax"),
    # -- combinators (explicitly out of scope) ----------------------------
    ("database == 'pg' and x == 1", "condition.syntax"),
    ("database == 'pg' or x == 1", "condition.syntax"),
    ("not database == 'pg'", "condition.syntax"),
    ("(database == 'pg')", "condition.syntax"),
    # -- function calls / attribute access (security-by-syntax) -----------
    ("os.system('id')", "condition.syntax"),
    ("__import__('os')", "condition.syntax"),
    ("database.__class__", "condition.syntax"),
    ("database.lower() == 'x'", "condition.syntax"),
    # -- invalid variable names (leading underscore is *not* in this list) --
    ("Database == 'x'", "condition.syntax"),  # PascalCase
    ("1database == 'x'", "condition.syntax"),  # leading digit
    ("data-base == 'x'", "condition.syntax"),  # hyphen
)


@pytest.mark.parametrize(("text", "expected_code"), _INVALID_CASES)
def test_invalid_condition_is_rejected_with_code(text: str, expected_code: str) -> None:
    """Every rejected input yields a ``ParseError`` with the right code."""
    result = parse(text)
    assert isinstance(result, ParseError)
    assert result.code == expected_code, (
        f"expected code {expected_code!r}, got {result.code!r} "
        f"for input {text!r} (message={result.message!r})"
    )


# The parametrisation below reuses ``_INVALID_CASES`` but only needs the
# ``text`` column. Rather than declaring a second tuple of ``text`` values
# (which would require manual synchronisation with ``_INVALID_CASES``), we
# project the second column out once at module level. This is a tuple of
# strings, not a fixture, so ``@pytest.mark.usefixtures`` does not apply —
# the previous version of this test parametrised over ``("text", "_code")``
# and received ``_code`` as a per-row argument, which ruff flagged as
# PT019 ("fixture '_code' without value is injected as parameter"). The
# projection below is the correct shape: the parametrisation supplies only
# the arguments the test body actually uses.
_INVALID_TEXTS: tuple[str, ...] = tuple(text for text, _code in _INVALID_CASES)


@pytest.mark.parametrize("text", _INVALID_TEXTS)
def test_invalid_condition_carries_full_structured_error(text: str) -> None:
    """Every error has condition + message + hint + code, none empty."""
    result = parse(text)
    assert isinstance(result, ParseError)
    assert result.condition == text
    assert result.message
    assert result.hint
    assert result.code


@pytest.mark.parametrize("text", _INVALID_TEXTS)
def test_invalid_condition_adapts_to_finding(text: str) -> None:
    """``ParseError.to_finding()`` preserves every field verbatim."""
    result = parse(text)
    assert isinstance(result, ParseError)
    finding = result.to_finding()
    assert isinstance(finding, Finding)
    assert finding.condition == result.condition
    assert finding.message == result.message
    assert finding.hint == result.hint
    assert finding.code == result.code


# ---------------------------------------------------------------------------
# Totality
# ---------------------------------------------------------------------------
def test_parse_returns_a_value_for_none() -> None:
    """``None`` is a total-input case, not a crash."""
    result = parse(None)  # type: ignore[arg-type]
    assert isinstance(result, ParseError)
    assert result.code == "condition.empty"


@pytest.mark.parametrize(
    "text",
    [
        # Previously this list contained `"x =="` twice (a copy-paste
        # duplicate ruff flagged as PT014); the duplicate is dropped here.
        # The coverage the two-argument `==` case was meant to provide is
        # already exercised by ``_INVALID_CASES``'s `"database === 'postgresql'"`
        # row and by ``"x =="`` on its own below.
        "",
        " ",
        "=",
        "==",
        "!=",
        "x",
        "x ==",
        "x != ",
        "x == y",
        "x == 1e5",
        "x == '\\n'",
        "x\t==\t'y'",
    ],
)
def test_parse_is_total_on_arbitrary_garbage(text: str) -> None:
    """Every input yields a value of the declared union type."""
    result = parse(text)
    assert isinstance(result, Equals | NotEquals | ParseError)


# ---------------------------------------------------------------------------
# Fidelity (parser accepts *exactly* the grammar's examples)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("rule_name", list(documented_examples()))
def test_grammar_example_parses(rule_name: str) -> None:
    """Every example the grammar documents actually parses."""
    example = documented_examples()[rule_name]
    result = parse(example)
    assert not isinstance(result, ParseError), (
        f"grammar rule {rule_name!r} documents {example!r} but the parser "
        f"rejected it: {result.message}"
    )


# ---------------------------------------------------------------------------
# Defence in depth: variable-name re-check
# ---------------------------------------------------------------------------
def test_variable_name_recheck_is_redundant_but_correct() -> None:
    """No AST node may have an identifier the grammar would reject.

    The regex in ``GRAMMAR`` enforces the identifier shape, so a well-
    formed match cannot produce an invalid variable. This test fuzzes
    nasty names through ``parse()`` and asserts the invariant: *if* a
    node is produced, its variable name is a valid identifier. If a
    future change loosens a grammar regex, this test still refuses to
    let an invalid identifier become an AST node.
    """
    identifier = re.compile(VARIABLE_NAME_PATTERN)

    for name in [
        "__class__",
        "class",
        "def",
        "os.system",
        "0start",
        "with-hyphen",
        "UPPER",
        "mixedCase",
        "_leading_is_valid",
        "a",
        "_",
        "a_1",
    ]:
        condition = f"{name} == 'x'"
        result = parse(condition)
        if isinstance(result, Equals | NotEquals):
            assert identifier.match(result.variable), (
                f"parser accepted invalid identifier {result.variable!r} "
                f"from input {condition!r}"
            )
        else:
            assert isinstance(result, ParseError)


def test_leading_underscore_is_accepted_by_design() -> None:
    """Leading-underscore identifiers are **valid** in the DSL.

    This is the *positive* counterpart to the negative corpus. The
    identifier rule is ``^[a-z_][a-z0-9_]*$``, matching Python's own
    convention, so ``_private`` is a legal variable name. A former
    version of ``_INVALID_CASES`` listed ``_private == 'x'`` as invalid
    — the parser disagreed (correctly) and three tests failed.

    If the DSL ever *does* decide to forbid leading underscores, this
    test flips too — and the maintainer sees the change in CI, which is
    exactly the regression marker the removed row was trying to be.
    """
    result = parse("_private == 'x'")
    assert isinstance(result, Equals)
    assert result.variable == "_private"


# ---------------------------------------------------------------------------
# Structure: no code execution in the parser's import graph
#
# Two tests, each *satisfiable*, each covering a different failure mode:
#
#   1. ``test_parser_module_dict_has_no_code_execution_primitives``
#      inspects ``vars(forge.conditions.parser)`` at runtime. If the
#      parser binds ``eval`` / ``exec`` / ``compile`` / ``ast`` /
#      ``__import__`` / ``importlib`` in its own namespace, this test
#      fails — *and it cannot false-positive*, because ``re.compile``
#      puts ``compile`` in ``re``'s dict, not the parser's.
#
#   2. ``test_parser_source_contains_no_bare_code_execution_calls``
#      scans ``parser.py``'s source with a negative lookbehind
#      ``(?<![\w.])`` so ``re.compile(`` is not matched, and catches a
#      dynamic ``__import__``-style call that never lands in the module
#      dict.
#
# What this file deliberately does **not** test:
#
#   * "Is ``ast`` in ``sys.modules`` after importing the parser?"
#     ``ast`` is present in *every* Python process that imports ``re``
#     (which the parser does). Asserting its absence would be false for
#     a correct parser and would train the team to ignore red CI. The
#     right question — "did *the parser* import ``ast``?" — is answered
#     by the two tests above, not by inspecting ``sys.modules`` at large.
# ---------------------------------------------------------------------------
def test_parser_module_dict_has_no_code_execution_primitives() -> None:
    """The parser module does **not** bind eval/exec/compile/ast.

    Any call to Python's ``compile()``, ``eval()``, ``exec()``, or
    ``__import__()`` from inside ``parser.py`` requires the parser module
    to *reference* that name — either directly (``eval(...)``) or via an
    import (``from builtins import eval``, ``import ast``). Both paths
    leave a binding in ``parser.__dict__``.

    Crucially, ``re.compile(...)`` does **not** leave ``compile`` in the
    parser's dict — it is an attribute of ``re``, not of the parser —
    so this check cannot false-positive on the parser's legitimate use
    of regex compilation.
    """
    import forge.conditions.parser as parser_module

    for forbidden in ("eval", "exec", "compile", "ast", "__import__", "importlib"):
        assert forbidden not in vars(parser_module), (
            f"parser module binds forbidden name {forbidden!r}; the parser "
            f"must not gain code-execution primitives — see ADR-0016"
        )


def test_parser_source_contains_no_bare_code_execution_calls() -> None:
    """Belt-and-braces source scan, precise enough not to hit ``re.compile``.

    The runtime dict check above is authoritative: if a name is bound,
    the runtime check catches it. This source scan catches the case of a
    dynamically-constructed call (``getattr(builtins, 'e' + 'val')``)
    that never lands in the module dict — extreme, but cheap to guard.

    The regex uses a negative lookbehind ``(?<![\\w.])`` so
    ``re.compile(`` (a method of ``re``, not the builtin) is **not**
    matched. This is the fix for the false positive that broke an
    earlier version of this test.
    """
    parser_path = Path(__file__).parents[3] / "src" / "forge" / "conditions" / "parser.py"
    source = parser_path.read_text(encoding="utf-8")

    bare_code_execution = (
        re.compile(r"(?<![\w.])eval\s*\("),
        re.compile(r"(?<![\w.])exec\s*\("),
        re.compile(r"(?<![\w.])compile\s*\("),  # excludes re.compile(...)
        re.compile(r"(?<![\w.])__import__\s*\("),
        re.compile(r"^\s*import\s+ast\b", re.MULTILINE),
        re.compile(r"^\s*from\s+ast\s+import\b", re.MULTILINE),
    )
    for pattern in bare_code_execution:
        assert not pattern.search(source), (
            f"parser.py contains forbidden pattern {pattern.pattern!r}; "
            f"the parser must not gain code-execution primitives — see ADR-0016"
        )


# ---------------------------------------------------------------------------
# NOTE on the leading-underscore decision
# ---------------------------------------------------------------------------
# The identifier grammar allows a leading underscore (`^[a-z_]...`), so
# `_private == 'x'` parses successfully — this is intentional, matching
# Python's own convention. The intent of the *old* `_INVALID_CASES` row
# was to be a regression marker if we ever tightened the rule. That
# intent is now served by `test_leading_underscore_is_accepted_by_design`
# above, which asserts the *current* behaviour directly. If we do decide
# to forbid leading underscores, the change is:
#
#   1. Tighten `VARIABLE_NAME_PATTERN` in grammar.py.
#   2. Move this test to a new `test_leading_underscore_is_rejected`
#      that asserts the ParseError and its code.
#   3. Update CONDITION_GRAMMAR.md's variable-name rules.
#
# All visible in one PR — which is what the doc-sync test guarantees.
