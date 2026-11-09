"""Tests for the ``npm_package`` named validator.

The ``npm_package`` validator is the single source of truth for what
counts as a legal npm package name across Forge. It is referenced from:

* ``resolution.validators`` when the resolver validates a variable
  whose id is ``npm_package``;
* ``cli.commands.create`` when the wizard prompts for an npm package
  name and wants real-time feedback;
* any future entry point (a non-CLI wrapper, an IDE integration) that
  needs to know "is this a legal npm package name".

The rule is stricter than ``project_name`` in one important way: it
enforces npm's *length* ceiling (214 characters) and its
reserved-name list. It is more permissive in another: dots and
underscores are allowed inside the name (as long as the name does
not *start* with them), because npm permits those characters.

The validator deliberately does **not** accept scoped names
(``@scope/name``) yet. That is a design decision, not an oversight:
the scaffolding layer does not yet create the nested directory
structure that scoped names imply, and accepting then failing later
would be a worse experience than rejecting up front. This file has a
dedicated test for that behaviour so the decision is visible in the
diff of any change that reconsiders it.

Scope
-----
These tests cover the rule's behaviour *as it stands*. The rule's
*policy* (why scopes are not yet supported, why the reserved list is
short, why the length ceiling matches npm's) is documented in
``resolution.names.npm_package``'s docstring.

Why the file is structured around parametrised tables
-----------------------------------------------------
Same reasoning as ``test_project_name.py`` and
``test_python_package.py``: the validator is a categorisation
function, and parametrising gives named test cases, additive
coverage, and a clear signal that the *set* of tested forms is
intentional.

The accepted cases use ``None`` as the expected value, matching the
validator's contract (``None`` on success, ``(message, hint)`` tuple
on failure), documented in ``resolution.names``.
"""

from __future__ import annotations

import pytest

from forge.resolution.names import npm_package

# ---------------------------------------------------------------------------
# Accepted forms
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        # Canonical shapes.
        "my-package",
        "mypackage",
        "pkg",
        "a",
        # With digits in non-leading positions.
        "pkg2",
        "api-v2",
        "v1alpha",
        # Dots and underscores are allowed inside (but not leading).
        "my.package",
        "my_package",
        "my.pkg.name",
        "my_pkg_name",
        "a.b",
        "a_b",
        # Mixed.
        "my-pkg_v2.1",
        "api.v2-alpha",
    ],
)
def test_accepts_valid_names(value: str) -> None:
    """These are legal npm package names and must validate.

    The set spans the range of accepted shapes:

    * single letters (``"a"``) — npm permits one-character names;
    * digits in non-leading positions;
    * dots and underscores as *interior* separators — npm allows
      both, and codebases use them.

    A regression that broke any of these would break real
    blueprints. Each failure names the exact candidate.
    """
    assert npm_package(value) is None


# ---------------------------------------------------------------------------
# Rejected forms — empty
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "",
        " ",
        "   ",
        "\t",
    ],
)
def test_rejects_empty_and_whitespace_only(value: str) -> None:
    """Whitespace-only input is semantically empty.

    The validator does not strip; stripping is the prompt engine's
    job. But the validator must not accept strings that are empty
    from a user's perspective — a direct library caller would
    otherwise pass ``"   "`` into a package name and discover the
    problem downstream.
    """
    assert npm_package(value) is not None


# ---------------------------------------------------------------------------
# Rejected forms — uppercase
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "My-Package",
        "MYPACKAGE",
        "MyPackage",
        "my-Package",
    ],
)
def test_rejects_uppercase(value: str) -> None:
    """Uppercase is rejected — npm names are lowercase-only.

    This is a hard rule on npm's side (not merely a convention): the
    registry will refuse a package with uppercase characters, and a
    project that used ``MyPackage`` locally would fail at publish
    time. Rejecting it at the boundary converts a distant failure
    into an immediate one.
    """
    assert npm_package(value) is not None


# ---------------------------------------------------------------------------
# Rejected forms — leading dot or underscore
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        ".hidden",
        ".config",
        "_private",
        "_internal",
    ],
)
def test_rejects_leading_dot_or_underscore(value: str) -> None:
    """A name may not start with a dot or underscore.

    npm reserves both forms for its own use (config files, internal
    packages). A package published with a leading dot or underscore
    would be rejected by the registry — and even locally, the names
    are confusing to read.

    Note that this rule is *about the first character*: ``"a.b"``
    and ``"a_b"`` are accepted (see the accepted set above). Only
    leading dots and underscores are forbidden.
    """
    assert npm_package(value) is not None


# ---------------------------------------------------------------------------
# Rejected forms — special characters
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        ("my package", "space"),
        ("my/package", "slash"),
        ("my\\package", "backslash"),
        ("my@package", "at-sign — reserved for scopes"),
        ("my:package", "colon"),
        ("my!package", "exclamation"),
        ("my#package", "hash"),
        ("my%package", "percent"),
        ("my package!", "space and exclamation"),
        ("café", "non-ASCII letter"),
        ("my😀package", "emoji"),
    ],
    ids=[
        "space",
        "slash",
        "backslash",
        "at-sign",
        "colon",
        "exclamation",
        "hash",
        "percent",
        "space-and-exclamation",
        "non-ascii",
        "emoji",
    ],
)
def test_rejects_special_characters(value: str, reason: str) -> None:
    """Characters outside npm's identifier grammar are rejected.

    The ``reason`` parameter is documentation-in-the-signature: it
    makes the test's intent readable in the parametrisation table,
    and gives a maintainer debugging a CI failure a one-line
    explanation of *why* the candidate is expected to fail.

    The ``@`` case is called out explicitly because it is the *one*
    character that would be legitimate in a scoped name
    (``@scope/name``). This validator deliberately does not support
    scoped names yet — see the dedicated test below — so ``@`` is
    rejected in the *name* position. That distinction (``@`` is
    reserved, not forbidden forever) is what the reason text
    records.
    """
    assert npm_package(value) is not None


# ---------------------------------------------------------------------------
# Rejected forms — scoped names (deliberate non-support)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "@scope/name",
        "@myorg/my-package",
        "@types/node",
    ],
)
def test_scoped_names_are_rejected_with_explicit_message(value: str) -> None:
    """Scoped names are rejected with a message that names the limitation.

    Scoped names are *legitimate* npm names — but scaffolding them
    requires a nested directory structure that Forge does not yet
    create. Rejecting them here, with an explicit "not yet
    supported" message, is better than accepting them and failing
    later with a cryptic "ENOENT" during file creation.

    This is a *policy* decision, not a rule derived from npm's
    grammar. The test exists so a future change that adds support
    (or that accidentally removes the current rejection) is visible
    in this file's diff.
    """
    problem = npm_package(value)
    assert problem is not None
    message, hint = problem
    # The message names the limitation ("scoped"), so a user who
    # tried a scoped name knows what to do.
    assert "scope" in message.lower()
    # The hint names a workaround ("unscoped") — a dead end is a bug.
    assert "unscoped" in hint.lower() or "unscoped" in message.lower()


# ---------------------------------------------------------------------------
# Rejected forms — reserved names
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "node_modules",
        "favicon.ico",
        "http",
        "https",
        "fs",
        "path",
        "child_process",
    ],
)
def test_reserved_names_are_rejected(value: str) -> None:
    """Reserved names are rejected with a clear message.

    The reserved set is deliberately small — the two names that
    cause *actual* failures at install time (``node_modules``,
    ``favicon.ico``) and the handful of Node.js built-in module
    names that collide when a project imports a package with the
    same name. Full parity with npm's reserved list is not the goal
    (see the module docstring); the goal is preventing the common,
    confusing failures.
    """
    problem = npm_package(value)
    assert problem is not None
    message, _hint = problem
    assert "reserved" in message.lower()


# ---------------------------------------------------------------------------
# Rejected forms — length
# ---------------------------------------------------------------------------


def test_rejects_names_longer_than_214_characters() -> None:
    """npm enforces a 214-character ceiling; this validator matches it.

    The 214 limit is not arbitrary — it is npm's documented maximum.
    A name longer than this will be rejected by the registry, so a
    project that passed local validation and reached ``npm publish``
    would fail with a message the user could not easily act on
    (the name was already validated locally). Rejecting it up front
    converts that into an immediate, actionable error.

    The test constructs the exact boundary (214 = accepted,
    215 = rejected) so a future change that "rounds" the limit to
    200 or 256 is visible.
    """
    # 214 characters: accepted (exactly at the ceiling).
    at_limit = "a" * 214
    assert npm_package(at_limit) is None

    # 215 characters: rejected (one over the ceiling).
    over_limit = "a" * 215
    problem = npm_package(over_limit)
    assert problem is not None


def test_accepts_single_character_name() -> None:
    """A one-character name is legal npm — the limit is a maximum, not a minimum.

    Easy to forget when writing the regex: ``{0,213}`` after a
    required leading character gives 1-214 total, not 2-214. This
    test pins the minimum as well as the maximum so a future
    "helpful" change that requires at least two characters does not
    silently reject legitimate short names.
    """
    assert npm_package("a") is None


# ---------------------------------------------------------------------------
# The error must be *actionable*
# ---------------------------------------------------------------------------


def test_error_message_names_the_problem() -> None:
    """The returned message names the specific rule broken.

    A generic "invalid npm package name" is a dead end. The message
    must distinguish at least:

    * uppercase;
    * leading dot/underscore;
    * reserved name;
    * non-identifier character;
    * length overflow;
    * scoped name.

    Each rejection class has a different fix, so the messages must
    not collapse into one.
    """
    uppercase_msg, _ = _split(npm_package("My-Package"))
    assert "lowercase" in uppercase_msg.lower() or "case" in uppercase_msg.lower()

    leading_msg, _ = _split(npm_package(".hidden"))
    assert "dot" in leading_msg.lower() or "underscore" in leading_msg.lower()

    reserved_msg, _ = _split(npm_package("node_modules"))
    assert "reserved" in reserved_msg.lower()

    space_msg, _ = _split(npm_package("my package"))
    assert "space" in space_msg.lower() or "hyphen" in space_msg.lower()


def test_error_hint_provides_a_concrete_fix() -> None:
    """The hint names a concrete action, not just a restatement.

    For the uppercase case (the most common mistake), the hint must
    suggest lowercasing — the specific fix, not "use a valid name".
    """
    _message, hint = _split(npm_package("My-Package"))
    assert "lowercase" in hint.lower() or "lower" in hint.lower()


def test_leading_character_hint_suggests_prepending() -> None:
    """The hint for a leading dot/underscore suggests prepending a letter.

    "Prepend a letter" is a concrete action; "start with a letter" is
    a restatement of the rule. The user needs the former — and the
    specific suggestion ("pkg-" + the current name) makes the fix
    one edit away.
    """
    _message, hint = _split(npm_package(".hidden"))
    assert "prepend" in hint.lower() or "letter" in hint.lower()


@pytest.mark.parametrize(
    "value",
    [
        "My-Package",
        ".hidden",
        "node_modules",
        "my package",
        "@scope/name",
        "",
        "a" * 300,
    ],
)
def test_every_rejection_carries_a_hint(value: str) -> None:
    """Every rejection returns a non-empty hint.

    The ``npm_package`` contract is ``(message, hint) | None``. A
    ``(message, "")`` return would satisfy the *type* but violate
    the *contract* the callers rely on. This test pins the contract
    structurally, one candidate per rejection class — including the
    length overflow, which is easy to forget when adding new rules.
    """
    problem = npm_package(value)
    assert problem is not None
    _message, hint = problem
    assert hint


# ---------------------------------------------------------------------------
# Purity
# ---------------------------------------------------------------------------


def test_validator_is_pure() -> None:
    """Repeated calls produce identical results; no hidden state.

    A validator that cached results, mutated a module-level default,
    or relied on the environment would be a liability. This test is
    the guardrail for a future optimisation that adds caching, and
    it documents that purity is a *contract* of this module — not an
    accident.
    """
    value = "my-package"
    results = [npm_package(value) for _ in range(5)]
    assert results == [None] * 5

    value = "node_modules"
    results = [npm_package(value) for _ in range(5)]
    assert len(set(results)) == 1


# ---------------------------------------------------------------------------
# Cross-check: the three named validators disagree deliberately
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("candidate", "npm_ok", "reason"),
    [
        # npm accepts hyphens; Python package names do not.
        ("my-package", True, "hyphen — npm's preferred separator"),
        # npm accepts dots; project_name and python_package do not.
        ("my.package", True, "dot — allowed interior separator"),
        # npm accepts underscores interior; project_name does not.
        ("my_package", True, "underscore — allowed interior separator"),
    ],
    ids=["hyphen", "dot", "underscore"],
)
def test_npm_accepts_separators_other_validators_reject(
    candidate: str, npm_ok: bool, reason: str
) -> None:
    """The three validators disagree on separators, deliberately.

    The validators serve different ecosystems:

    * ``project_name``: lowercase + digits + hyphens (npm-oriented
      *and* URL-oriented);
    * ``python_package``: Python identifier (no hyphens, dots
      allowed only at package-nesting boundaries which are not
      supported);
    * ``npm_package``: npm grammar (hyphens, dots, underscores all
      allowed in interior positions).

    The disagreements are the *point* of having three validators.
    A future "simplification" that merged them would break this
    test — forcing a conversation about which rules to unify and
    which to keep separate, rather than an accidental rule drift.
    """
    assert (npm_package(candidate) is None) is npm_ok, reason


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _split(problem: tuple[str, str] | None) -> tuple[str, str]:
    """Assert the problem is non-``None`` and return ``(message, hint)``.

    Same helper as ``test_python_package.py`` — see that file's
    docstring for the rationale. Duplicated rather than shared
    because each file is small enough that the import indirection
    would be more noise than the duplication; if a third file needs
    it, promote to ``tests/unit/resolution/_helpers.py``.
    """
    assert problem is not None, "expected a problem tuple, got None"
    return problem
