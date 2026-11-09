"""Tests for the ``python_package`` named validator.

The ``python_package`` validator is the single source of truth for what
counts as a legal Python package name across Forge. It is referenced
from:

* ``resolution.validators`` when the resolver validates a variable
  whose id is ``python_package``;
* ``cli.commands.create`` when the wizard prompts for a Python
  package name and wants real-time feedback;
* any future entry point (a non-CLI wrapper, an IDE integration) that
  needs to know "is this a legal Python package name".

The rule is stricter than ``project_name`` on purpose:

- **No hyphens.** A Python package name is a directory that must be
  importable (``import my_package``), and ``my-package`` is not a
  valid identifier.
- **No leading digit.** ``123abc`` is not a valid Python identifier
  (also enforced by ``isidentifier()``, which we use as the
  authority).
- **No keywords.** ``class`` and ``import`` are valid identifiers
  syntactically but cannot be used as module names.

The validator delegates identifier-shape decisions to
``str.isidentifier()`` — the stdlib's authority on the question —
rather than reimplementing Unicode identifier rules via regex. That
is a deliberate choice, documented in ``resolution.names``: Python's
identifier rules are Unicode-aware (``café`` is a valid identifier),
and a homegrown regex would silently drift from the language's
actual rules.

Scope
-----
These tests cover the rule's behaviour *as it stands*. The rule's
*policy* (why underscores are allowed but hyphens are not, why
keywords are rejected, what happens with Unicode) is documented in
``resolution.names.python_package``'s docstring.

Why the file is structured around parametrised tables
-----------------------------------------------------
The validator is a categorisation function: it accepts some strings
and rejects others, with a specific message-and-hint per rejection
class. The natural test shape is a table, and parametrising gives:

* Named test cases in the pytest output ("the ``123abc`` case
  broke"), not opaque line numbers.
* Additive coverage — a new candidate is one line, not a new test
  function.
* A clear signal that the *set* of tested forms is intentional, not
  accidental.

The accepted cases use ``None`` as the expected value, matching the
validator's contract (``None`` on success, ``(message, hint)`` tuple
on failure), documented in ``resolution.names``.
"""

from __future__ import annotations

import keyword

import pytest

from forge.resolution.names import python_package

# ---------------------------------------------------------------------------
# Accepted forms
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        # Canonical identifier shapes.
        "my_package",
        "mypackage",
        "pkg",
        "_internal",
        "api",
        # With digits — but not *starting* with a digit.
        "pkg2",
        "api_v2",
        "v1alpha",
        # Underscores at any non-leading position.
        "my_long_package_name",
        "a_b_c",
        # Unicode identifiers — the validator uses `isidentifier()`,
        # which is Unicode-aware.
        "café",
        "naïve",
    ],
)
def test_accepts_valid_identifiers(value: str) -> None:
    """These are legal Python package names and must validate.

    The set spans the range of accepted shapes:

    * leading underscore (``"_internal"``) — a valid Python
      identifier, and a legitimate convention for "private" packages;
    * Unicode letters (``"café"``) — Python identifiers allow them,
      and rejecting them would be a bug, not a feature;
    * digits in non-leading positions.

    A regression that broke any of these would break real
    blueprints. Each failure names the exact candidate.
    """
    assert python_package(value) is None


# ---------------------------------------------------------------------------
# Rejected forms — empty
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "",
        " ",
        "   ",
    ],
)
def test_rejects_empty_and_whitespace_only(value: str) -> None:
    """Whitespace-only input is semantically empty.

    The validator does not strip; stripping is the prompt engine's
    job (``cli.prompts.ask_text``). But the validator must not
    accept strings that are empty from a user's perspective — a
    direct library caller would otherwise pass ``"   "`` into a
    package name and discover the problem two layers downstream.
    """
    assert python_package(value) is not None


# ---------------------------------------------------------------------------
# Rejected forms — leading digit
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "1package",
        "2api",
        "9svc",
        "123",
    ],
)
def test_rejects_leading_digit(value: str) -> None:
    """A Python identifier must start with a letter or underscore.

    ``"123abc"`` is the canonical example from the acceptance
    criteria; the rest of the set proves the rule is about the
    *first* character, not about digits appearing anywhere in the
    name (``"pkg2"`` is accepted, see the previous test).
    """
    assert python_package(value) is not None


# ---------------------------------------------------------------------------
# Rejected forms — hyphens
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "my-package",
        "my-service",
        "api-v2",
        "a-b",
    ],
)
def test_rejects_hyphens(value: str) -> None:
    """Hyphens are not valid in Python identifiers.

    This is the difference between ``project_name`` (which *requires*
    hyphens) and ``python_package`` (which *forbids* them). The
    validator's hint must steer the user toward the underscore
    spelling, since the fix is a one-character substitution.
    """
    assert python_package(value) is not None


# ---------------------------------------------------------------------------
# Rejected forms — spaces and other invalid characters
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        ("my package", "space"),
        ("my.package", "dot — not an identifier character"),
        ("my/package", "slash"),
        ("my\\package", "backslash"),
        ("my@package", "at-sign"),
        ("my:package", "colon"),
        ("my!package", "exclamation"),
        ("my😀package", "emoji — emoji are not identifier characters"),
        ("€package", "currency symbol"),
    ],
    ids=[
        "space",
        "dot",
        "slash",
        "backslash",
        "at-sign",
        "colon",
        "exclamation",
        "emoji",
        "currency",
    ],
)
def test_rejects_non_identifier_characters(value: str, reason: str) -> None:
    """Characters that are not part of Python's identifier grammar are rejected.

    The ``reason`` parameter is documentation-in-the-signature: it
    makes the test's intent readable in the parametrisation table,
    and it gives a maintainer debugging a CI failure a one-line
    explanation of *why* the candidate is expected to fail.

    The emoji case is included because paste-from-mobile and
    copy-paste from rich text commonly introduce them; the "silent
    success" of such names would be a nasty downstream crash
    (``import my😀package`` is a ``SyntaxError``). Rejecting them at
    the boundary converts that crash into an actionable message.
    """
    assert python_package(value) is not None


# ---------------------------------------------------------------------------
# Rejected forms — Python keywords
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        # A representative sample of Python's keywords. The full set
        # is in `keyword.kwlist`; the test below cross-checks every
        # keyword, so this parametrisation is for the *human
        # reader* — it shows the common ones by name.
        "class",
        "import",
        "return",
        "if",
        "def",
        "lambda",
        "True",
        "False",
        "None",
    ],
)
def test_rejects_python_keywords(value: str) -> None:
    """Python keywords are syntactically valid identifiers but cannot be module names.

    ``isidentifier()`` returns ``True`` for ``"class"`` — it *is* a
    valid identifier. But ``import class`` is a ``SyntaxError``
    because the parser treats it as a keyword. The validator must
    therefore check ``keyword.iskeyword()`` *in addition to*
    ``isidentifier()``. This test pins that extra check.
    """
    assert python_package(value) is not None


def test_every_python_keyword_is_rejected() -> None:
    """Cross-check against the stdlib's full keyword list.

    Hardcoding keywords in the test would duplicate the validator's
    own check — a maintenance liability that would drift whenever
    Python adds or removes a keyword. Iterating ``keyword.kwlist``
    instead means the test stays correct across Python versions
    automatically.

    The failure mode this catches is subtle: a future "simplification"
    of the validator that drops the ``iskeyword`` check would still
    pass the parametrised test above (which only covers a sample),
    but fail *this* test immediately, with the exact keyword that
    broke.
    """
    for kw in keyword.kwlist:
        assert python_package(kw) is not None, f"{kw!r} should be rejected"


# ---------------------------------------------------------------------------
# The error must be *actionable*
# ---------------------------------------------------------------------------


def test_error_message_names_the_problem() -> None:
    """The returned message names the specific rule broken.

    A generic "invalid package name" message is a dead end. The
    message must distinguish at least the common cases:

    * leading digit;
    * hyphen;
    * keyword;
    * generic non-identifier.

    Each case has a different fix (prepend a letter, replace with
    underscore, choose another name, remove the offending character),
    so the message must not be collapsed into one.
    """
    leading_digit_msg, _ = _split(python_package("123abc"))
    assert "digit" in leading_digit_msg.lower() or "start" in leading_digit_msg.lower()

    hyphen_msg, _ = _split(python_package("my-package"))
    assert "hyphen" in hyphen_msg.lower() or "underscore" in hyphen_msg.lower()

    keyword_msg, _ = _split(python_package("class"))
    assert "keyword" in keyword_msg.lower()


def test_error_hint_provides_a_concrete_fix() -> None:
    """The hint names a concrete action, not just a restatement.

    For the hyphen case (the most common typo), the hint must
    suggest the underscore substitution — the specific fix, not "use
    a valid name".
    """
    _message, hint = _split(python_package("my-package"))
    assert "underscore" in hint.lower() or "_" in hint


def test_leading_digit_hint_suggests_prepending() -> None:
    """The hint for a leading digit suggests prepending a letter/underscore.

    "Prepend a letter or underscore" is a concrete action; "start
    with a letter" is a restatement of the rule. The user needs the
    former.
    """
    _message, hint = _split(python_package("123abc"))
    assert "prepend" in hint.lower() or "underscore" in hint.lower()


@pytest.mark.parametrize(
    "value",
    [
        "123abc",
        "my-package",
        "class",
        "my package",
        "",
    ],
)
def test_every_rejection_carries_a_hint(value: str) -> None:
    """Every rejection returns a non-empty hint.

    The ``python_package`` contract is ``(message, hint) | None``. A
    ``(message, "")`` return would satisfy the *type* but violate
    the *contract* the callers rely on. This test pins the contract
    structurally, one candidate per rejection class.
    """
    problem = python_package(value)
    assert problem is not None
    _message, hint = problem
    assert hint


# ---------------------------------------------------------------------------
# Purity
# ---------------------------------------------------------------------------


def test_validator_is_pure() -> None:
    """Repeated calls produce identical results; no hidden state.

    A validator that cached results, mutated a module-level default,
    or relied on the environment would be a liability: the CLI could
    give different answers to the same input on different calls.
    This test is the guardrail for a future optimisation that adds
    caching.

    It also documents, in the test suite, that purity is a
    *contract* of this module — not an accident.
    """
    value = "my_package"
    results = [python_package(value) for _ in range(5)]
    assert results == [None] * 5

    value = "123abc"
    results = [python_package(value) for _ in range(5)]
    assert len(set(results)) == 1


# ---------------------------------------------------------------------------
# Cross-check: python_package is stricter than project_name
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "project_ok_python_bad",
    [
        # Hyphens are *required* by project_name (npm convention) and
        # *forbidden* by python_package (identifier grammar).
        "my-service",
        # Leading digits are *forbidden* by both, but the messages
        # differ (see below).
    ],
    ids=["hyphen"],
)
def test_python_package_rejects_project_name_hyphens(
    project_ok_python_bad: str,
) -> None:
    """The two validators disagree on hyphens, deliberately.

    ``project_name`` accepts hyphens (npm/URL convention);
    ``python_package`` rejects them (Python identifier rule). The
    disagreement is the *whole point* of having two validators
    rather than one — a user who is told "pick a project name" and
    "pick a Python package name" must get different rules, and
    different error hints, or the second validator is just noise.
    """
    assert python_package(project_ok_python_bad) is not None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _split(problem: tuple[str, str] | None) -> tuple[str, str]:
    """Assert the problem is non-``None`` and return ``(message, hint)``.

    The validator's contract allows ``None`` (success) or a tuple
    (failure). Most rejection tests want to assert on the tuple's
    contents, and a bare ``assert problem is not None`` followed by
    ``message, hint = problem`` would work, but the two-line form is
    noisy enough that a helper is worth it. The helper also makes
    the test body read as ``message, hint = _split(...)``, which is
    the intent.
    """
    assert problem is not None, "expected a problem tuple, got None"
    return problem
