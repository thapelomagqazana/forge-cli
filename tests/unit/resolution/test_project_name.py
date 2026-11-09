"""Tests for the ``project_name`` named validator.

The ``project_name`` validator is the single source of truth for what
counts as a legal project name across Forge. It is referenced from:

* ``resolution.validators`` when the resolver validates a variable
  whose id is ``project_name``;
* ``cli.commands.create`` when the wizard prompts for a project name
  and wants real-time feedback;
* any future entry point (a non-CLI wrapper, an IDE integration) that
  needs to know "is this a legal project name".

Because the validator is a **pure function** — no I/O, no state, no
environment — the tests below do not need to construct a blueprint, a
resolver, or a command. They call the function directly. That is the
point of the layer: the rule lives in one place, and the tests pin it
in one place.

Scope
-----
These tests cover the rule's behaviour *as it stands*. The rule's
*policy* (which characters are allowed, why underscores are
rejected, etc.) is documented in ``resolution.names.project_name``'s
docstring. If the project ever decides to widen or narrow the rule,
this file's parametrised tables must change with it — that is
deliberate: a change to the rule should be visible in a diff of *the
tests for that rule*, not buried in a resolver test or a CLI test
that happens to exercise the validator indirectly.

Why the file is structured around parametrised tables
-----------------------------------------------------
The validator is a categorisation function: it accepts some strings
and rejects others, with a specific message-and-hint per rejection
class. The natural test shape is a table:

    candidate  |  expected  |  reason
    -----------+------------+-------------------------
    "my-api"   |  accepted  |  canonical valid form
    "My-API"   |  rejected  |  uppercase
    ...

Parametrising means:

* Each rejected candidate is a *named test* in the pytest output, so
  a failure says "the `My-API` case broke" rather than "the
  parametrised test broke". That is the difference between a bug
  report and a debugging session.

* Adding a new candidate is one line, not a new test function. The
  rule's coverage can grow without growing the boilerplate.

* The *reason* column is a string in the ``ids=`` argument, so a
  maintainer reading a CI failure sees the categorisation at a
  glance.

The accepted cases use ``None`` as the expected value, matching the
validator's contract (``None`` on success, ``(message, hint)`` tuple
on failure). This is documented in ``resolution.names`` and enforced
by the type annotation on every validator.
"""

from __future__ import annotations

import pytest

from forge.resolution.names import project_name

# ---------------------------------------------------------------------------
# Accepted forms
#
# These are the canonical shapes the rule is designed to accept. A
# change to the acceptance set is a change to the *rule*, not a bug
# in the validator — the tests exist so that change is visible.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "myapi",
        "my-api",
        "api",
        "a",
        "x1",
        "svc-2",
        "my-service-2024",
        "a1",
        "svc",
        "a1b2",
    ],
)
def test_accepts_valid_names(value: str) -> None:
    """These are legal project names and must validate.

    The set spans the range of accepted shapes:

    * single-letter names (``"a"``) — the rule does not impose a
      minimum length beyond non-empty;
    * letter-only names (``"myapi"``) — the simplest valid form;
    * names with digits (``"x1"``, ``"svc-2"``, ``"my-service-2024"``);
    * names that begin with a digit immediately after the first
      letter — proving the "must start with a letter" rule is about
      the *first character*, not about digits appearing anywhere
      after it.

    A regression that broke any of these would break real
    blueprints. Each failure names the exact candidate.
    """
    assert project_name(value) is None


# ---------------------------------------------------------------------------
# Rejected forms — empty / whitespace
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "",
        " ",
        "   ",
        "\t",
        "\n",
    ],
)
def test_rejects_empty_and_whitespace_only(value: str) -> None:
    """Whitespace-only input is empty from a *user* perspective.

    The validator does not strip; stripping is the *prompt engine's*
    job (``cli.prompts.ask_text`` strips and rejects empties before
    the validator ever runs). But the validator must not accept
    strings that are semantically empty — a caller that skips the
    prompt engine (a future API, a direct library use) must not
    silently pass ``"   "`` into a project name.
    """
    assert project_name(value) is not None


# ---------------------------------------------------------------------------
# Rejected forms — case
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "My-API",
        "MYAPI",
        "MyApi",
        "mY-aPi",
    ],
)
def test_rejects_uppercase(value: str) -> None:
    """Uppercase is rejected — the rule is lowercase-only.

    The reasoning is practical: the project name is reused as an npm
    package name (and typically as a URL slug, a Docker image tag,
    etc.), all of which have their own case conventions. Lowercase
    is the intersection that avoids a rename at every boundary.

    The test does not just assert "rejected" — it exists as a
    separate parametrisation from the "leading digit" and "special
    characters" cases so that a future maintainer can see, at a
    glance, which categories are being tested and where a rule
    change would need to touch the file.
    """
    assert project_name(value) is not None


# ---------------------------------------------------------------------------
# Rejected forms — leading digit
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "1api",
        "2fast",
        "9-service",
        "123",
    ],
)
def test_rejects_leading_digit(value: str) -> None:
    """A project name must start with a letter.

    The rule is not about "digits are bad"; ``"x1"`` and ``"svc-2"``
    are accepted (see ``test_accepts_valid_names``). It is about the
    *first* character — which many ecosystems (Python identifiers,
    some package managers) require to be a letter.
    """
    assert project_name(value) is not None


# ---------------------------------------------------------------------------
# Rejected forms — trailing hyphen
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "my-",
        "api-",
        "svc-2-",
        "a-",
    ],
)
def test_rejects_trailing_hyphen(value: str) -> None:
    """A project name must not end with a hyphen.

    Trailing hyphens are rejected by npm, are awkward in URLs, and
    are frequently a sign of a template that expected the user to
    append something. Rejecting them at the boundary is cheaper than
    discovering the problem three layers down.
    """
    assert project_name(value) is not None


# ---------------------------------------------------------------------------
# Rejected forms — special characters
#
# The rule accepts exactly: lowercase letters, digits, and hyphens.
# Every other character is rejected. The parametrisation names the
# common cases a user is likely to try, so the *reason* for the
# rejection is legible in a CI failure.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        ("my project", "space"),
        ("my_project", "underscore — npm/URL convention prefers hyphens"),
        ("my.api", "dot"),
        ("my/api", "slash"),
        ("my\\api", "backslash"),
        ("my@api", "at-sign"),
        ("my:api", "colon"),
        ("my api", "space (again, deliberately)"),
        ("my!api", "exclamation"),
        ("café", "non-ASCII letter"),
        ("my😀api", "emoji"),
    ],
    ids=[
        "space",
        "underscore",
        "dot",
        "slash",
        "backslash",
        "at-sign",
        "colon",
        "space-again",
        "exclamation",
        "non-ascii",
        "emoji",
    ],
)
def test_rejects_special_characters(value: str, reason: str) -> None:
    """Non-alphanumeric, non-hyphen characters are rejected.

    The ``reason`` parameter is documentation-in-the-signature: it
    makes the test's intent readable in the parametrisation table,
    and it gives a maintainer who is debugging a CI failure a
    one-line explanation of *why* the candidate is expected to fail.

    The underscore case is called out explicitly because it is the
    one users most often expect to be accepted (Python packages use
    underscores) — the validator deliberately rejects it for
    project names, and this test is the counterpart to the
    ``test_accepts_common_valid_forms`` in
    ``test_create_dual_mode.py`` that documents the same rule from
    the CLI side.
    """
    assert project_name(value) is not None


# ---------------------------------------------------------------------------
# The error must be *actionable*
# ---------------------------------------------------------------------------


def test_error_message_names_the_problem() -> None:
    """The returned message names what is wrong (not just "invalid").

    A message like "invalid" is a dead end — it tells the user that
    they are wrong but not *why*, and the fix requires guessing.
    The message must name the specific rule broken (uppercase,
    leading digit, trailing hyphen, special character, etc.).
    """
    problem = project_name("My-API")
    assert problem is not None
    message, _hint = problem
    assert "uppercase" in message.lower() or "lowercase" in message.lower()


def test_error_hint_provides_a_concrete_example() -> None:
    """The hint gives the user something concrete to try.

    A hint that says "use a valid name" is not a hint. A hint that
    says "Example: 'my-service'" gives the user a template to copy
    and modify. Every rejection class here must produce a hint that
    contains a concrete example (or, for the empty case, a concrete
    instruction).
    """
    problem = project_name("12345")
    assert problem is not None
    _message, hint = problem
    # The hint must contain *something actionable*. The specific
    # word "example" is a useful proxy: it is what distinguishes a
    # hint from a restatement of the message.
    assert "example" in hint.lower() or "e.g." in hint.lower()


@pytest.mark.parametrize(
    "value",
    [
        "My-API",
        "1api",
        "my-",
        "my api",
        "my_api",
    ],
)
def test_every_rejection_carries_a_hint(value: str) -> None:
    """Every rejection returns a non-empty hint.

    The ``Project`` validator's contract is
    ``(message, hint) | None``. A ``(message, "")`` return would
    satisfy the *type* but violate the *contract* the callers rely
    on (they render the hint to the user; an empty one is a UX dead
    end). This test pins the contract structurally, one candidate
    per rejection class.
    """
    problem = project_name(value)
    assert problem is not None
    _message, hint = problem
    assert hint  # non-empty


# ---------------------------------------------------------------------------
# Purity — the validator is a pure function of its argument
# ---------------------------------------------------------------------------


def test_validator_is_pure() -> None:
    """Repeated calls produce identical results; no hidden state.

    A validator that cached results, mutated a module-level default,
    or relied on the environment would be a liability: the CLI could
    give different answers to the same input on different calls.
    This test is trivially satisfied by the current implementation
    (which is pure), but it is the guardrail for a future
    optimisation that adds caching.

    It also documents, in the test suite, that purity is a
    *contract* of this module — not an accident.
    """
    value = "my-service"
    results = [project_name(value) for _ in range(5)]
    assert results == [None] * 5

    value = "My-API"
    results = [project_name(value) for _ in range(5)]
    assert len(set(results)) == 1
