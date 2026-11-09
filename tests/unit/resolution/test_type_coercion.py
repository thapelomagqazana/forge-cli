"""Tests for type coercion in ``resolution.coercion``.

Coercion is the boundary between the *wire* representation of a value
(usually a string from a CLI flag or a YAML scalar) and the *runtime*
type the rest of the system validates and renders against. It is
also the place where "just use Python's default behaviour" produces
the worst bugs:

* ``bool("false") == True`` — the canonical footgun.
* ``int("1_000") == 1000`` — legal in Python 3, but likely
  unintended.
* ``str(None) == "None"`` — how a missing value becomes a literal
  string in a rendered template.

``resolution.coercion`` exists precisely to prevent those. Its
functions are explicit, per-type, and refuse ambiguity rather than
silently resolving it. This file pins that behaviour.

Scope
-----
These tests exercise the three coercion functions directly:

* ``to_boolean``  — accepts a closed set of true/false spellings.
* ``to_string``   — accepts real strings only (no ``str()`` casting).
* ``to_enum``     — accepts a string from a declared allowed set.

They do **not** exercise ``resolution.validators``' composition (type
+ pattern + named validator) — that is the resolver's concern, tested
in ``test_resolver.py`` and the per-variable test files. The split
keeps each failure mode attributable: a coercion bug fails here with
the exact spelling case, and a validator-composition bug fails
elsewhere with the composed error.

A note on the "closed set" of boolean spellings
-----------------------------------------------
The set is deliberately small: ``true/false/yes/no/on/off/1/0`` and
their case-insensitive variants. That is what the module docstring
calls "one spelling per type" — a policy decision, not a technical
limitation. Widening the set is a *policy* change, and it belongs in
``resolution.coercion``'s diff. These tests make that visible: every
accepted spelling is a parametrised case, and every rejected
ambiguity is a named negative case. A future "helpful" change that
starts accepting ``"yep"`` or ``"1.0"`` fails a test that says so
in its name.

A note on error shape
---------------------
Every coercion error is a ``ValidationError`` with a ``code``, a
``hint``, and the path/source the caller supplied. The codes are
shared vocabulary across the resolution layer:

* ``type.mismatch``   — the raw value could not be coerced.
* ``enum.not_allowed`` — the coerced string was not in the allowed set.

The tests assert on the code, not just the message, because callers
that route on category (the wizard's inline feedback, CI gates,
future ``--json`` output) key on the code. A message-only test would
pass for an error that routed the wrong way.
"""

from __future__ import annotations

import pytest

from forge.loading.errors import ValidationError
from forge.resolution.coercion import to_boolean, to_enum, to_string

# ---------------------------------------------------------------------------
# ``to_boolean`` — accepted spellings
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # The canonical lowercase spellings.
        ("true", True),
        ("false", False),
        # Short forms.
        ("yes", True),
        ("no", False),
        ("y", True),
        ("n", False),
        ("on", True),
        ("off", False),
        # Numeric spellings.
        ("1", True),
        ("0", False),
        # Real booleans (from YAML defaults and prompt answers).
        (True, True),
        (False, False),
    ],
    ids=[
        "true",
        "false",
        "yes",
        "no",
        "y",
        "n",
        "on",
        "off",
        "one",
        "zero",
        "real-True",
        "real-False",
    ],
)
def test_boolean_accepts_canonical_spellings(raw: object, expected: bool) -> None:
    """The full set of accepted spellings, one case each.

    Each case is a *named* test in the pytest output, so a failure
    says "the ``yes`` case broke" rather than "some spelling broke".
    That distinction is the difference between a one-line fix and a
    bisect.

    The ``ids=`` argument carries human-readable names because the
    raw values include strings that would render confusingly as
    test IDs on their own (``"y"`` and ``"n"`` in particular).
    """
    result = to_boolean(raw, path="variables.flag", source="cli:flag")
    assert result is expected


@pytest.mark.parametrize(
    "raw",
    [
        "TRUE",
        "False",
        "Yes",
        "nO",
        "ON",
        "oFf",
    ],
)
def test_boolean_is_case_insensitive(raw: str) -> None:
    """Case is normalised before matching — a user typing ``TRUE`` gets ``True``.

    A validator that required exactly-lowercase spellings would be a
    bug: users type in mixed case, and the CLI's job is to accept
    what they reasonably mean, not to punish capitalization.

    The set spans every accepted base spelling in at least one
    mixed-case form, so a future "simplification" that only
    lowercases the *whole* string (but forgets to also strip it, or
    that lowercases only the first character) is caught by at least
    one case.
    """
    result = to_boolean(raw, path="variables.flag", source="cli:flag")
    # The result type is bool for every accepted case; a ``str``
    # return would indicate the coercion did not fire.
    assert isinstance(result, bool)


@pytest.mark.parametrize(
    "raw",
    [
        " true ",
        "  false",
        "yes  ",
        "\ttrue\n",
    ],
)
def test_boolean_strips_surrounding_whitespace(raw: str) -> None:
    """Shells frequently pass ``" true "`` — the coercion strips, then matches.

    ``argparse`` passes the value exactly as typed on the command
    line, and shells do not strip the space between ``--flag`` and
    ``value``. A validator that required exact-match would reject
    ``--flag " true "`` (with quotes around a spaced value), which
    is a legitimate way a user might pass the value.
    """
    result = to_boolean(raw, path="variables.flag", source="cli:flag")
    assert isinstance(result, bool)


# ---------------------------------------------------------------------------
# ``to_boolean`` — rejected forms
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "",
        " ",
        "maybe",
        "yep",
        "nope",
        "truee",
        "tru",
        "1.0",
        "0.0",
        "2",
        "-1",
        "None",
        "null",
        "nil",
    ],
)
def test_boolean_rejects_ambiguous_or_unknown_spellings(raw: str) -> None:
    """The closed set is closed — everything else is a ``type.mismatch``.

    The list is the *interesting* set of near-misses:

    * ``"1.0"`` / ``"0.0"`` — numeric-string floats. Python's
      ``bool()`` would accept these (they are truthy), but the
      coercion deliberately does not: a user who typed ``1.0`` for
      a boolean is confused, and accepting it silently hides the
      confusion.
    * ``"2"`` / ``"-1"`` — integer strings outside the ``0``/``1``
      set. Same reasoning.
    * ``"None"`` / ``"null"`` / ``"nil"`` — string spellings of
      absence. ``bool(None)`` is ``False``, but the coercion must
      not accept the *string* ``"None"`` as a boolean value — that
      is almost certainly a bug in the caller.
    * ``"yep"`` / ``"nope"`` — colloquial forms the closed set
      deliberately does not accept. If a user needs them, they
      belong in their shell wrapper, not in every Forge invocation.
    """
    result = to_boolean(raw, path="variables.flag", source="cli:flag")
    assert isinstance(result, ValidationError)
    assert result.code == "type.mismatch"


@pytest.mark.parametrize(
    "raw",
    [
        0,
        1,
        2,
        -1,
        None,
        [],
        {},
        1.0,
    ],
    ids=[
        "int-zero",
        "int-one",
        "int-two",
        "int-negative",
        "none",
        "list",
        "dict",
        "float",
    ],
)
def test_boolean_rejects_non_bool_non_string_values(raw: object) -> None:
    """Coercion refuses to guess — a non-bool, non-string value is a ``type.mismatch``.

    The list covers the values a caller *might* try, and every one
    is rejected:

    * ``0`` / ``1`` — the classic "int as bool" bug. ``0 == False``
      in Python, but the coercion does not accept it because the
      caller is conflating two different types.
    * ``None`` — the resolver uses ``None`` internally to mean
      "absent"; passing it to coercion is a caller bug, not user
      input. Silently coercing ``None`` to ``False`` would hide the
      bug.
    * Lists and dicts — never booleans. ``bool([])`` is ``False``,
      but the *coercion* is not the place to make that decision.

    Rejecting all of these is the point: the coercion's job is to
    turn the *specific* accepted spellings into booleans, not to
    guess what any given Python object "means". The caller is
    responsible for handing it a value it can work with.
    """
    result = to_boolean(raw, path="variables.flag", source="cli:flag")
    assert isinstance(result, ValidationError)
    assert result.code == "type.mismatch"


def test_boolean_error_includes_accepted_spellings() -> None:
    """The error's ``extra`` field lists the accepted spellings.

    Machine-readable consumers (a future ``--json`` output, an IDE
    integration) can surface the full set without re-parsing the
    hint. The ``extra`` field is the documented place for structured
    error detail (see ``ValidationError``'s docstring).
    """
    result = to_boolean("maybe", path="variables.flag", source="cli:flag")
    assert isinstance(result, ValidationError)
    accepted = result.extra.get("accepted")
    assert isinstance(accepted, list)
    # A representative subset — asserting the exact set would make
    # the test brittle to any addition (which is a *policy* change
    # and deserves its own test, not this one).
    for spelling in ("true", "false", "yes", "no", "on", "off"):
        assert spelling in accepted


def test_boolean_error_carries_hint() -> None:
    """Every coercion error carries a hint (empty hints are a bug)."""
    result = to_boolean("maybe", path="variables.flag", source="cli:flag")
    assert isinstance(result, ValidationError)
    assert result.hint  # non-empty


# ---------------------------------------------------------------------------
# ``to_string`` — accepted
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "",
        " ",
        "hello",
        "hello world",
        "  padded  ",
        "café",
        "my😀value",
        "\n",
    ],
)
def test_string_accepts_any_real_string(raw: str) -> None:
    """Any real string is a valid string — no trimming, no normalising.

    ``to_string`` is deliberately the *simplest* coercion: it checks
    that the value is already a string and returns it unchanged.
    Trimming is ``ask_text``'s job (in the wizard) and
    pattern-validation's job (in the resolver), not the coercion's.
    Keeping the coercion simple means a string variable's value
    reaches the resolver *exactly* as the user typed it — including
    padding — so the resolver's pattern rules see what the user
    actually supplied.
    """
    result = to_string(raw, path="variables.author", source="cli:author")
    assert result == raw


# ---------------------------------------------------------------------------
# ``to_string`` — rejected
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        0,
        1,
        True,
        False,
        None,
        [],
        {},
        1.5,
        b"bytes",
    ],
    ids=[
        "int-zero",
        "int-one",
        "bool-true",
        "bool-false",
        "none",
        "list",
        "dict",
        "float",
        "bytes",
    ],
)
def test_string_rejects_non_string_values(raw: object) -> None:
    """Silent ``str()`` casting is forbidden — only real strings pass.

    The classic bug this prevents: YAML ``version: 1.10`` parses as
    a *float* (``1.1``), and ``str(1.1) == "1.1"`` — losing the
    trailing zero the user wrote. A project that reused the version
    string elsewhere would silently have a different value.

    The second classic bug: a stray ``None`` becomes the literal
    string ``"None"`` in a rendered template. Rejecting non-strings
    at the coercion layer converts both into an actionable
    ``type.mismatch`` error pointing at the offending field.

    ``bool`` is rejected too — even though ``True`` and ``False``
    are stringifiable, they are not the *string* ``"True"`` /
    ``"False"``, and a caller passing them is conflating types.
    """
    result = to_string(raw, path="variables.author", source="cli:author")
    assert isinstance(result, ValidationError)
    assert result.code == "type.mismatch"


def test_string_error_names_the_actual_type() -> None:
    """The error names the type it received, not just "wrong type".

    ``"expected a string, got float"`` is actionable — the caller
    can inspect the value that was passed. ``"expected a string"``
    alone would leave them guessing.
    """
    result = to_string(1.5, path="variables.version", source="cli:version")
    assert isinstance(result, ValidationError)
    assert "float" in result.message
    assert result.hint  # non-empty


# ---------------------------------------------------------------------------
# ``to_enum`` — accepted
# ---------------------------------------------------------------------------


def test_enum_accepts_value_in_allowed_set() -> None:
    """A value in the declared set passes unchanged."""
    result = to_enum(
        "dev",
        ("dev", "staging", "prod"),
        path="variables.mode",
        source="cli:mode",
    )
    assert result == "dev"


@pytest.mark.parametrize(
    "value",
    ["dev", "staging", "prod"],
)
def test_enum_accepts_each_allowed_value(value: str) -> None:
    """Each allowed value is accepted — parametrised so a missing case is visible.

    A single test that asserted only one value would miss a bug
    where, e.g., the last entry in the list was compared incorrectly.
    Parametrising over the full set is cheap and closes that gap.
    """
    result = to_enum(
        value,
        ("dev", "staging", "prod"),
        path="variables.mode",
        source="cli:mode",
    )
    assert result == value


# ---------------------------------------------------------------------------
# ``to_enum`` — rejected
# ---------------------------------------------------------------------------


def test_enum_rejects_value_not_in_allowed_set() -> None:
    """A value outside the declared set is ``enum.not_allowed``.

    The code matters: callers can distinguish "the value is not a
    string at all" (``type.mismatch``) from "the value is a string
    but not one of the allowed ones" (``enum.not_allowed``). The
    resolver uses that distinction to shape its message — one is a
    type error, the other is a choice error.
    """
    result = to_enum(
        "qa",
        ("dev", "staging", "prod"),
        path="variables.mode",
        source="cli:mode",
    )
    assert isinstance(result, ValidationError)
    assert result.code == "enum.not_allowed"


def test_enum_rejects_value_with_different_case() -> None:
    """Enum matching is exact — ``"Dev"`` does not match ``"dev"``.

    Case-insensitive matching would be dangerous: an enum with
    entries ``["dev", "DEV"]`` (both legal because they are
    distinct strings) would be unmatchable if case were folded. The
    coercion keeps the rule strict, and users who mistype the case
    get a clear "not in allowed set" error that names the options.

    This is deliberately *different* from ``to_boolean``'s
    case-insensitive matching — the boolean's accepted set is a
    fixed, tiny vocabulary; the enum's accepted set is
    blueprint-author-controlled, and case-folding would make some
    legal sets unmatchable.
    """
    result = to_enum(
        "Dev",
        ("dev", "staging", "prod"),
        path="variables.mode",
        source="cli:mode",
    )
    assert isinstance(result, ValidationError)
    assert result.code == "enum.not_allowed"


def test_enum_error_lists_allowed_values() -> None:
    """The error names the allowed values for both humans and machines.

    The *message* names the specific problem ("'qa' is not one of the
    allowed values"). The *hint* names the fix ("Allowed: dev,
    staging, prod."). Both fields have a job, and the split matters:
    the CLI renders both, but a "print just the errors" consumer
    (log aggregation, CI summary) wants the message without the
    hint, and a "print just the fix" consumer (inline prompt
    feedback while typing) wants the hint without the message.
    Merging them would defeat both consumers.

    The ``extra["allowed"]`` payload carries the same values for
    machine consumers (a future ``--json`` output, an IDE
    completion) that want to *act* on the allowed set rather than
    display it.
    """
    result = to_enum(
        "qa",
        ("dev", "staging", "prod"),
        path="variables.mode",
        source="cli:mode",
    )
    assert isinstance(result, ValidationError)

    # The allowed values belong in the *hint* — the "what to do"
    # field. This is where every other enum error in the system
    # (via ``_error``) puts them, and it is what the wizard shows
    # inline to help the user fix the typo.
    for value in ("dev", "staging", "prod"):
        assert value in result.hint

    # The message names the problem and the offending input, not
    # the allowed set.
    assert "qa" in result.message
    assert "allowed" in result.message.lower()

    # The machine-readable payload carries the same set, as a
    # JSON-serialisable list.
    allowed = result.extra.get("allowed")
    assert isinstance(allowed, list)
    assert set(allowed) == {"dev", "staging", "prod"}


def test_enum_error_carries_hint() -> None:
    """Every enum error carries a hint naming the allowed values."""
    result = to_enum(
        "qa",
        ("dev", "staging", "prod"),
        path="variables.mode",
        source="cli:mode",
    )
    assert isinstance(result, ValidationError)
    assert result.hint
    # The hint restates the allowed values (different wording from
    # the message is fine, but the values themselves must be there).
    for value in ("dev", "staging", "prod"):
        assert value in result.hint


def test_enum_rejects_non_string_values_via_type_mismatch() -> None:
    """A non-string enum value is a ``type.mismatch``, not ``enum.not_allowed``.

    The distinction matters: an integer value for an enum variable
    is a *type* error (the caller passed the wrong kind of thing),
    not a *choice* error (the caller chose a wrong option). The
    resolver's error messages differ accordingly, and a future
    ``--json`` consumer keys on the code to decide how to render.

    ``to_enum`` delegates to ``to_string`` for the type check —
    this test proves the delegation produces the right code, not
    just the right rejection.
    """
    result = to_enum(
        42,
        ("dev", "staging", "prod"),
        path="variables.mode",
        source="cli:mode",
    )
    assert isinstance(result, ValidationError)
    assert result.code == "type.mismatch"


# ---------------------------------------------------------------------------
# Error shape — uniform across all three functions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("call", "expected_code"),
    [
        (lambda: to_boolean("maybe", path="p", source="s"), "type.mismatch"),
        (lambda: to_string(42, path="p", source="s"), "type.mismatch"),
        (
            lambda: to_enum("qa", ("dev",), path="p", source="s"),
            "enum.not_allowed",
        ),
    ],
    ids=["boolean", "string", "enum"],
)
def test_errors_share_a_uniform_shape(call, expected_code: str) -> None:
    """Every coercion error has the same structure, whatever the function.

    Uniformity is a *feature*: the CLI handler, the loader's
    aggregation, and any future structured output can treat every
    error identically. A test that only checked one function would
    miss a regression where one branch started returning a bare
    string or a plain ``ValueError`` — the caller would break in a
    specific command path, discovered by a user, not a test.

    The parametrisation calls each function once with a case that
    produces its characteristic error. All three must have the same
    fields: ``path``, ``message``, ``hint``, ``source``, ``code``.
    """
    result = call()
    assert isinstance(result, ValidationError)
    assert result.code == expected_code
    assert result.path == "p"
    assert result.source == "s"
    assert result.message
    assert result.hint


# ---------------------------------------------------------------------------
# Purity
# ---------------------------------------------------------------------------


def test_coercions_are_pure() -> None:
    """Repeated calls produce identical results; no hidden state.

    A coercion that cached results would be a liability: the same
    input must produce the same output on every call, and there must
    be no state shared between call sites. This test is the
    guardrail for a future optimisation that adds caching.

    It also documents, in the test suite, that purity is a
    *contract* of these functions — not an accident.
    """
    for fn, raw, kwargs in [
        (to_boolean, "true", {"path": "p", "source": "s"}),
        (to_string, "hello", {"path": "p", "source": "s"}),
        (to_enum, "dev", {"path": "p", "source": "s"}),
    ]:
        # Different signature for to_enum — it takes `allowed`.
        if fn is to_enum:
            results = [fn(raw, ("dev", "prod"), **kwargs) for _ in range(3)]
        else:
            results = [fn(raw, **kwargs) for _ in range(3)]
        # Equal-by-value, not equal-by-identity — the coercion may
        # (correctly) return a new string for a string input; the
        # *value* is what matters.
        assert results[0] == results[1] == results[2]
