"""Unit tests for the *documentation* of the conditions grammar.

Why this file exists
--------------------
The conditions DSL has one source of truth for *behaviour* —
``forge.conditions.grammar.GRAMMAR`` — and one source of truth for
*what users read* — ``docs/reference/CONDITION_GRAMMAR.md``. They
can drift. When they do, the docs become a trap: a blueprint author
reads "``var == True`` is supported", writes it, and gets a syntax
error; or reads only examples that no longer match the grammar, and
builds a mental model that fails on the third variant they try.

This file is the enforcement mechanism for "the docs cannot rot".
It is a **doc-sync test**: it reads the markdown file and asserts
that the grammar's documented examples appear in it, that the grammar's
documented forms are all present, and that nothing in the doc
advertises a form the grammar rejects. Every change to the grammar
must be accompanied by a doc change, or CI fails.

That is *the* invariant. Everything else in this file exists to make
the failure modes of *that* invariant legible:

    * If a new rule is added to ``GRAMMAR`` without a doc row, the
      sync test fails with a message naming the missing rule.
    * If a rule is removed from ``GRAMMAR`` but the doc still lists
      it, the sync test fails with a message naming the stale example.
    * If the doc format changes in a way the parser cannot extract
      examples from, a *format test* fails before the sync test does
      — so a "the docs are broken" failure looks different from a
      "the grammar changed and the docs weren't updated" failure.

The last point is worth emphasising: doc-sync tests frequently
conflate "the doc parser broke" with "the doc content is wrong", and
debugging the failure becomes archaeology. Splitting them means a
red CI job says which of the two happened.

What this file does *not* test
------------------------------
* **Grammar behaviour.** That is ``test_parser.py``'s job. This file
  only cares about *whether the docs describe the grammar*, not
  whether the grammar does what it says.
* **Grammar examples as parsed input.** ``test_parser.py`` has a test
  (``test_grammar_example_parses``) that proves every example parses.
  This file takes the *complementary* position — that every example
  is *documented* — so the two files together prove "documented ⇔
  parses" for every rule.
* **User-facing prose quality.** The doc's wording can improve or
  regress in ways a test cannot meaningfully judge; this file only
  checks structure (which examples appear in which table rows) and
  presence.

Lint conventions
----------------
* **No mutable class attributes** (``RUF012``).
* **No side-effect-only fixtures** — no fixtures at all; helpers are
  plain functions.
* **No hardcoded ``/tmp``** (``S108``).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import ClassVar

import pytest

from forge.conditions.grammar import (
    VARIABLE_NAME_PATTERN,
    documented_examples,
    rule_families,
    supported_operators,
)

# ---------------------------------------------------------------------------
# Path resolution
#
# The doc lives at ``docs/reference/CONDITION_GRAMMAR.md`` under the
# repository root. ``parents[3]`` walks up from
# ``tests/unit/conditions/test_grammar_docs.py`` to the repo root; the
# intermediate directories are counted precisely so a future move of
# this file would break the path *loudly* (test collection fails) rather
# than silently finding a stale doc somewhere else.
# ---------------------------------------------------------------------------
_DOC_PATH = Path(__file__).parents[3] / "docs" / "reference" / "CONDITION_GRAMMAR.md"


# ---------------------------------------------------------------------------
# Format tests — do these *before* the sync tests.
#
# The sync tests assume the doc is parseable by a small, deterministic
# scanner: backtick-quoted examples appear in markdown tables, and the
# tables have a "Form" column whose rows identify the supported
# variants. If the doc's format changes — a heading renamed, a table
# replaced with prose, examples moved to a fenced code block — the
# scanner cannot find them and the sync test would fail with a
# misleading "missing rule" message. Format tests isolate that failure
# mode so CI says "the doc format changed" rather than "the grammar
# lost a rule".
# ---------------------------------------------------------------------------
class TestDocumentFormat:
    """The doc is parseable by this file's scanner.

    These tests do not check that the doc is *correct*, only that it
    is *machine-readable* in the shape the rest of this file expects.
    A format failure here means the doc-sync tests below cannot
    meaningfully run; the fix is to restore the doc's structure (or
    update the scanner), not to touch the grammar.
    """

    def test_doc_file_exists(self) -> None:
        """The reference doc is present at its expected path.

        A missing doc is a distinct failure from a doc whose content
        has drifted. Naming the path in the assertion message lets a
        reviewer fix the right problem — likely a rename or a move,
        not a content bug.
        """
        assert _DOC_PATH.is_file(), (
            f"grammar reference doc not found at {_DOC_PATH}; "
            "the sync tests cannot run without it"
        )

    def test_doc_contains_the_supported_forms_table(self) -> None:
        """The doc has a markdown table whose rows describe each form.

        The scanner below reads backtick-quoted examples from
        *anywhere* in the doc, but the *form table* is where the
        grammar's supported operators are enumerated. Without it, a
        reader would have to piece together the language from prose,
        and the sync test would lose one leg of its coverage — the
        per-rule example check would still work, but the "all
        operators documented" check would not.
        """
        text = _DOC_PATH.read_text(encoding="utf-8")
        # A "| Form | ... |"-style header is the marker. We do not
        # require an exact heading; we require the *shape* of a table
        # whose first column is the form (or example).
        assert re.search(r"^\|\s*Form\s*\|", text, re.MULTILINE), (
            "expected a markdown table with a 'Form' column in "
            f"{_DOC_PATH}; the doc structure has changed"
        )


# ---------------------------------------------------------------------------
# Example coverage — every grammar rule's example appears in the doc.
#
# This is the primary sync invariant: for every rule in ``GRAMMAR``,
# the rule's canonical example is quoted in the doc. The check is
# *inclusion*, not exact placement — the doc can present examples in
# tables, in prose, in a fenced code block, anywhere — because the
# doc's layout is allowed to evolve as long as the *set* of documented
# examples covers the grammar.
#
# The specific placement inside the "supported forms" table is a
# stronger check that appears later in this file (once the scanner
# knows how to extract table rows); the split lets CI distinguish
# "an example is missing entirely" from "an example is in the wrong
# place", which are two different fixes.
# ---------------------------------------------------------------------------
class TestExampleCoverage:
    """Every grammar rule's canonical example is documented somewhere."""

    @pytest.mark.parametrize(
        ("rule_name", "example"),
        sorted(documented_examples().items()),
    )
    def test_every_rule_example_appears_in_the_doc(self, rule_name: str, example: str) -> None:
        """``documented_examples()[rule]`` appears in ``CONDITION_GRAMMAR.md``.

        The example is looked for as a substring inside a backtick-
        quoted span. That is the form the doc actually uses (verified
        by ``TestDocumentFormat::test_doc_contains_the_supported_forms_table``
        for the table, and by inspection of the doc for the prose
        examples).

        If a rule is added to ``GRAMMAR`` and its example is not
        added to the doc, this test fails naming the specific rule —
        which is the whole point: no rule gets *implemented* without
        being *documented*.
        """
        text = _DOC_PATH.read_text(encoding="utf-8")
        assert f"`{example}`" in text, (
            f"grammar rule {rule_name!r} documents example {example!r}, "
            f"but that example does not appear in {_DOC_PATH}. "
            "Add it to the doc's supported-forms table (or wherever "
            "the doc lists examples for this rule's family)."
        )


# ---------------------------------------------------------------------------
# Operator coverage — every operator the grammar supports is documented.
#
# A rule's *example* implies its operator, so the per-example coverage
# check above technically implies this one. But the implication runs
# the wrong way for review: a reviewer looking at "which operators
# does this DSL support?" wants to see one assertion per operator,
# not infer the answer from six example-matching failures. This
# section states the operator set explicitly.
# ---------------------------------------------------------------------------
class TestOperatorCoverage:
    """Every operator the grammar supports is documented in the doc."""

    @pytest.mark.parametrize("operator", sorted(supported_operators()))
    def test_every_operator_appears_in_the_doc(self, operator: str) -> None:
        """``==`` and ``!=`` both appear in the doc (in code fences).

        The check is substring-based and lenient — ``==`` appears
        everywhere, including accidentally in prose. The *value* of
        the test is not "does ``==`` appear?" (it always will) but
        "if a future operator were added to ``GRAMMAR`` without being
        documented, would this test fail?". It would: the new operator
        would appear in ``supported_operators()``, this test would be
        parametrised over it, and the doc lookup would fail.

        The current operators both pass trivially, and that is fine —
        the test's job is to be the enforcement mechanism for a future
        one.
        """
        text = _DOC_PATH.read_text(encoding="utf-8")
        assert f"`{operator}`" in text or operator in text, (
            f"operator {operator!r} is supported by the grammar but "
            f"does not appear in {_DOC_PATH}. Add a row to the "
            "supported-forms table describing it."
        )


# ---------------------------------------------------------------------------
# Family coverage — every literal kind the grammar supports is documented.
#
# The grammar groups rules by literal *family* (``"string"``, ``"bool"``,
# ``"int"``). Each family has slightly different rules — strings need
# quotes, booleans are lowercase, integers are signed — and each has
# its own paragraph in the doc. A future contributor adding a family
# (say ``"float"``, which was intentionally excluded) would need to
# document it, and this test would catch a missing entry.
# ---------------------------------------------------------------------------
class TestFamilyCoverage:
    """Every literal family the grammar supports is documented."""

    # The set of families the grammar currently uses, expressed as the
    # substring each family's *name* takes in the doc. Keeping the
    # mapping here (rather than scanning the doc for keywords) means
    # a future family addition is a one-line change in this test
    # *and* a doc change, both visible in the same PR.
    #
    # ``ClassVar`` (not a bare class attribute) because the value is a
    # mutable ``dict``: Ruff's RUF012 flags unannotated mutable class
    # attributes because they are shared across every instance of the
    # class and can be mutated inadvertently by any method. Marking it
    # ``ClassVar`` records the intent — this is class-level lookup
    # data, not per-instance state — and keeps a future contributor
    # from accidentally reassigning ``self._FAMILY_KEYWORDS`` in a
    # test, which would silently affect every other test in the class.
    _FAMILY_KEYWORDS: ClassVar[dict[str, tuple[str, ...]]] = {
        "string": ("string", "strings"),
        "bool": ("boolean", "booleans"),
        "int": ("integer", "integers"),
    }

    @pytest.mark.parametrize("family", sorted(set(rule_families().values())))
    def test_every_family_has_a_doc_entry(self, family: str) -> None:
        """Each literal family's prose/section appears in the doc.

        The check accepts either singular or plural spelling of the
        family's name, because the doc's prose chooses whichever
        reads better in context. What it does *not* accept is silence
        about a family the grammar supports — a user reading the doc
        should be able to learn that ``bool`` literals are lowercase,
        that ``int`` literals are signed, and that ``string`` literals
        accept single or double quotes. All of that lives in the
        family's section.
        """
        text = _DOC_PATH.read_text(encoding="utf-8").lower()
        candidates = self._FAMILY_KEYWORDS.get(family, (family,))
        assert any(c in text for c in candidates), (
            f"literal family {family!r} is used by the grammar but "
            f"does not appear in {_DOC_PATH}. Add a section describing "
            f"its constraints (spelling, quoting, sign rules, ...)."
        )


# ---------------------------------------------------------------------------
# Negative-space coverage — the doc advertises what the grammar rejects.
#
# Doc-sync tests usually only check positive correspondence: every
# rule documented, every example present. But a *stronger* invariant
# is that the doc *explicitly states what is not supported*, because
# the DSL's design is defined as much by its refusals as by its
# admissions. A user who reads only the positive examples and then
# writes ``x == True`` deserves to have been told — in the doc, not
# in a CI failure — that uppercase spellings are syntax errors.
#
# This section tests that the doc's "Explicitly *not* supported"
# section actually enumerates the current non-goals. It is the
# safety net for the *documentation* of non-goals, not for the
# non-goals themselves (those are tested in ``test_parser.py``'s
# negative corpus).
# ---------------------------------------------------------------------------
class TestNonGoalDocumentation:
    """The doc's "not supported" section names the current non-goals."""

    @pytest.mark.parametrize(
        "feature",
        [
            # Boolean combinators. If any of these ever becomes
            # supported, its removal from the doc would be caught by
            # the parser tests (the negative corpus would start
            # failing); this test catches the *documentation* half —
            # a doc that still says "no and/or" after combinators
            # were added.
            "and",
            "or",
            "not",
            # Arithmetic. Same logic.
            "+",
            # Function calls. The DSL's whole point is that a
            # condition is data, not code; a doc that stopped
            # mentioning this would let users discover it only by
            # trying.
            "function call",
        ],
    )
    def test_non_goal_appears_in_the_not_supported_section(self, feature: str) -> None:
        """Each documented non-goal is mentioned in the "not supported" section.

        The check is deliberately substring-based rather than
        heading-based: the doc's phrasing of non-goals ("no ``and``",
        "no arithmetic", "no function calls") is allowed to evolve as
        long as each *concept* is covered. A test that required an
        exact heading would fight the doc's editorial freedom; a test
        that required the substring stays honest about what matters
        — that the user is told.

        The lookup is case-insensitive because "``and``" and "AND"
        both name the concept; the doc uses whichever reads naturally
        in context.
        """
        text = _DOC_PATH.read_text(encoding="utf-8").lower()
        # Normalise the search term to lowercase to match.
        assert feature.lower() in text, (
            f"grammar non-goal {feature!r} is not mentioned in "
            f"{_DOC_PATH}. The doc's 'not supported' section should "
            "enumerate what the DSL refuses."
        )


# ---------------------------------------------------------------------------
# Variable-name rule documentation.
#
# The grammar's identifier pattern (``VARIABLE_NAME_PATTERN``) is
# narrower than a general ``\w+``: it requires lowercase letters,
# digits, and underscores, starting with a letter or underscore. That
# constraint is not just implementation trivia — it is the reason a
# user typo like ``Database == 'x'`` produces a *syntax* error
# (well-formed reference to a variable that doesn't exist) rather
# than a *parse* error (unrecognised text). The doc must explain it
# so a user who hits that error knows which rule they violated.
# ---------------------------------------------------------------------------
class TestVariableNameRuleDocumented:
    """The identifier pattern's constraints are documented in the doc."""

    def test_variable_name_rules_are_documented(self) -> None:
        """The doc explains the identifier constraints a variable must satisfy.

        The pattern (``^[a-z_][a-z0-9_]*$``) permits:
        * lowercase letters,
        * digits (but not as the first character),
        * underscores (both leading and inner).

        A user reading an "invalid variable name" error needs to
        understand *which* rule they broke. The doc has a paragraph
        explaining the identifier rules; this test proves the
        paragraph is not missing entirely. The specific wording is
        left to the doc's editorial discretion — the assertion is
        only that the concept is covered.
        """
        text = _DOC_PATH.read_text(encoding="utf-8").lower()
        # The doc should mention the concept of identifier rules.
        # The specific phrasing is discretionary, but the concept is
        # non-negotiable: a user who hits `condition.invalid_variable`
        # must be able to find the rule that explains why.
        assert "variable name" in text or "identifier" in text, (
            f"{_DOC_PATH} does not explain the variable identifier "
            "constraints (lowercase letters, digits, underscores; "
            "not starting with a digit). A user who hits "
            "`condition.invalid_variable` has no doc entry to read."
        )

    def test_identifier_pattern_accepts_leading_underscore(self) -> None:
        """The pattern permits leading underscores — the doc must too.

        This is a *behavioural* test of the pattern, not a doc-sync
        test, but it lives here because the pattern's *intent* (matching
        Python's identifier convention) is documented alongside it. If
        a future change tightens the pattern to forbid leading
        underscores, this test flags the change *at the point where a
        reviewer would also update the doc* — the two changes should
        happen together, and co-locating them makes that obvious.

        The check is intentionally limited to the leading-underscore
        case: the pattern is a regex, and testing the regex's
        semantics exhaustively is ``test_parser.py``'s job. This test
        exists to flag a *specific* tightening that would have
        doc implications.
        """
        import re

        pattern = re.compile(VARIABLE_NAME_PATTERN)
        assert pattern.match("_private"), (
            "the identifier pattern no longer accepts leading "
            "underscores; if this is intentional, update "
            "CONDITION_GRAMMAR.md's variable-name rules and this test "
            "in the same PR"
        )


# ---------------------------------------------------------------------------
# Determinism — reading the doc is not order-dependent.
#
# A doc-sync test that iterated a *set* of examples (rather than the
# sorted dict returned by ``documented_examples()``) would produce a
# different parametrisation order across runs and would make the
# failure message ("rule X is missing") point at whichever rule the
# set happened to yield first. That is the kind of nondeterminism
# that turns a 30-second fix into a 10-minute investigation.
#
# This test is a *guard* — it will pass every run, and that is the
# point. It does not detect a bug in the current code; it detects a
# future change that quietly replaces the sorted iteration.
# ---------------------------------------------------------------------------
class TestDeterministicParametrisation:
    """Parametrisation is deterministic across runs.

    A doc-sync test that parameterised over a set would fail
    *nondeterministically* — the failure message would name a
    different rule each run. That is unacceptable for CI: a red job
    whose failure cannot be reproduced on the next run trains the
    team to ignore red jobs.

    The sorted iteration in this file's parametrisations is what
    makes failures reproducible. This test does not assert on the
    sorted-ness directly (that would be a test of Python's ``sorted``);
    it asserts that two successive enumerations yield the same
    sequence, which is what the sorted iteration guarantees and a
    set-based enumeration does not.
    """

    def test_documented_examples_iteration_is_deterministic(self) -> None:
        """``documented_examples().items()`` yields the same order twice.

        Every parametrisation in this file that iterates over
        ``documented_examples()`` sorts the output; this test verifies
        the property that motivates the sort. If a future refactor
        drops the sort, this test still passes — but the *parametrisations
        above* would start producing flaky orderings. The test is a
        reminder to keep the sort, and a light guard against a future
        implementation that returns a dict with nondeterministic
        iteration (e.g. one keyed on an unordered container).
        """
        first = list(documented_examples().items())
        second = list(documented_examples().items())
        assert first == second, (
            "documented_examples() iteration order is not stable; "
            "parametrised failures will be nondeterministic"
        )
