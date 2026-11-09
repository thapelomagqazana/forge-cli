"""Tests for the ``precedence.choose`` selection function.

The function under test answers exactly one question: *which source wins?*
It does not coerce, validate, or fail — that is `validators`' job. These
tests therefore assert the *selection contract* in isolation:

* The precedence order (CLI > prompt > default > absent) is exact.
* The distinction between "absent" and "falsy-but-present" is honoured
  (`False`, `0`, `""` are real values; only `None` and missing keys are
  treated as absent).
* Provenance labels (`source`, `origin`) are correct, because they flow
  into `ValidationError.source` downstream.
* The function is total: no input combination raises.

A parallel set of integration-style tests in
``tests/unit/resolution/test_resolver.py`` exercises the *combined*
behaviour (selection + validation). Those tests live with the resolver
because that is where the observable behaviour of a user command lives;
the tests here stay narrow so a failure points at exactly one piece of
logic.

Fixture strategy
----------------
Each test builds its variable definitions via small factories
(``_var``, ``_enum_var``, ``_bool_var``) rather than loading a YAML
fixture. Three reasons:

1. **Locality.** The rule being tested is *visible in the test body*,
   not hidden in a fixture file three directories away.
2. **Speed.** No file I/O, no schema validation, no Pydantic construction
   beyond what is directly needed. The suite stays fast enough to run on
   every save.
3. **Precision.** When a test fails, the diff shows exactly which rule
   broke. A shared fixture that grows a new field breaks every test at
   once, which is worse than a few extra lines per test.

The factories are deliberately tiny and defined once at the top of the
file — a full ``Blueprint`` is not needed because ``choose`` only reads
``var.id`` and ``var.default``.
"""

from __future__ import annotations

import pytest

from forge.models import (
    VariableDefinition,
    VariableType,
    VariableValidation,
)
from forge.resolution.precedence import Chosen, choose

# ---------------------------------------------------------------------------
# Factories
#
# These build only the fields ``choose`` actually consults. If a future
# change to ``choose`` starts reading another field, the corresponding
# test will fail with a clear message ("attribute not found on fake"),
# which is exactly the moment we want to be told.
# ---------------------------------------------------------------------------


def _var(
    id: str = "author",
    *,
    default: object | None = None,
    required: bool = True,
) -> VariableDefinition:
    return VariableDefinition(
        id=id,
        type=VariableType.string,
        description=f"test variable {id}",
        required=required,
        default=default,
    )


def _enum_var(
    id: str = "mode",
    *,
    choices: tuple[str, ...] = ("dev", "prod"),
    default: object | None = None,
) -> VariableDefinition:
    return VariableDefinition(
        id=id,
        type=VariableType.enum,
        description="test enum variable",
        required=True,
        default=default,
        validation=VariableValidation(enum=list(choices)),
    )


def _bool_var(
    id: str = "flag",
    *,
    default: object | None = None,
) -> VariableDefinition:
    return VariableDefinition(
        id=id,
        type=VariableType.boolean,
        description="test boolean variable",
        required=True,
        default=default,
    )


# ---------------------------------------------------------------------------
# Precedence order — the four canonical cases
# ---------------------------------------------------------------------------


def test_cli_wins_over_prompt_and_default() -> None:
    """Every source supplies a value; CLI must be the one chosen."""
    var = _var(default="from-default")
    result = choose(
        var,
        cli_values={"author": "from-cli"},
        prompt_values={"author": "from-prompt"},
    )
    assert result == Chosen(
        value="from-cli",
        source="cli:author",
        origin="cli",
    )


def test_prompt_wins_over_default_when_cli_absent() -> None:
    """CLI absent → prompt beats the blueprint default."""
    var = _var(default="from-default")
    result = choose(
        var,
        cli_values={},
        prompt_values={"author": "from-prompt"},
    )
    assert result == Chosen(
        value="from-prompt",
        source="prompt:author",
        origin="prompt",
    )


def test_default_used_when_neither_cli_nor_prompt_supplied() -> None:
    """Both user-supplied sources absent → blueprint default wins."""
    var = _var(default="from-default")
    result = choose(var, cli_values={}, prompt_values={})
    assert result == Chosen(
        value="from-default",
        source="default:author",
        origin="default",
    )


def test_no_sources_returns_none() -> None:
    """No CLI, no prompt, no default → ``None`` (not a failure, just absence).

    The resolver decides whether absence is an error (required variable)
    or fine (optional variable). ``choose`` does not know or care.
    """
    var = _var()  # no default
    result = choose(var, cli_values={}, prompt_values={})
    assert result is None


# ---------------------------------------------------------------------------
# The "absent vs falsy" distinction
#
# This is the single most important behavioural rule of the module. It is
# why `--flag false` actually disables the flag, and why `--count 0` sets
# the count to zero. Only `None` and missing keys are folded into
# "absent"; every other value wins.
# ---------------------------------------------------------------------------


def test_boolean_false_from_cli_is_a_real_value_not_absent() -> None:
    """``--flag false`` must reach the validator as ``False``, not be dropped.

    The classic bug this test prevents: `argparse` leaves a flag at
    ``None`` when unset, and a naive precedence check like
    ``if cli_values.get(k):`` would silently drop ``False`` and let the
    default win. Users would see the flag not working.
    """
    var = _bool_var(default=True)
    result = choose(
        var,
        cli_values={"flag": False},
        prompt_values={},
    )
    assert result == Chosen(value=False, source="cli:flag", origin="cli")


def test_empty_string_from_cli_is_a_real_value_not_absent() -> None:
    """An explicitly-passed empty string wins over the default.

    The empty value will almost certainly be rejected by the validator
    downstream — but that rejection is the correct behaviour: it produces
    "project name cannot be empty", not a silent fall-through to the
    default. Letting the default win here would hide the user's mistake.
    """
    var = _var(default="from-default")
    result = choose(
        var,
        cli_values={"author": ""},
        prompt_values={},
    )
    assert result == Chosen(value="", source="cli:author", origin="cli")


def test_zero_from_cli_is_a_real_value_not_absent() -> None:
    """Numeric zero must survive selection.

    There is no integer variable type in the schema today, but the rule
    is general and future-proofing it now costs one line. If the schema
    ever grows an integer type, this test documents the semantics the
    resolver already provides.
    """
    # ``_var`` uses type=string, but ``choose`` does not consult ``type``.
    # The test is about *selection*, not validation — a string variable
    # with a numeric raw value is a contrived but valid scenario for the
    # function under test.
    var = _var()
    result = choose(
        var,
        cli_values={"author": 0},
        prompt_values={},
    )
    assert result == Chosen(value=0, source="cli:author", origin="cli")


def test_none_in_cli_mapping_is_treated_as_absent() -> None:
    """``None`` in a source mapping means "the caller did not supply this".

    argparse populates an unused option with ``None``; treating that as
    "absent" lets callers pass the raw parsed namespace without
    pre-filtering. This is a *convention*, and it is the one place where
    a value in the mapping does not count.
    """
    var = _var(default="from-default")
    result = choose(
        var,
        cli_values={"author": None},
        prompt_values={"author": "from-prompt"},
    )
    # CLI was ``None`` (absent) → prompt wins.
    assert result == Chosen(
        value="from-prompt",
        source="prompt:author",
        origin="prompt",
    )


def test_none_in_prompt_mapping_is_treated_as_absent() -> None:
    """Same convention on the prompt side, for symmetry."""
    var = _var(default="from-default")
    result = choose(
        var,
        cli_values={},
        prompt_values={"author": None},
    )
    # Prompt was ``None`` (absent) → default wins.
    assert result == Chosen(
        value="from-default",
        source="default:author",
        origin="default",
    )


# ---------------------------------------------------------------------------
# Default semantics
# ---------------------------------------------------------------------------


def test_default_of_none_means_no_default() -> None:
    """A ``default: null`` in the blueprint is "no default", not "default is null".

    This is the same convention the schema's ``"default": []`` fields
    use for lists: absence is expressed by *absence*, not by a
    distinguished value. ``None`` is never a meaningful default for a
    scalar variable in v1, and treating it as absence keeps the resolved
    map clean for templates that use ``{% if var %}``.
    """
    var = _var(default=None)
    result = choose(var, cli_values={}, prompt_values={})
    assert result is None


def test_default_of_false_is_a_real_default() -> None:
    """``default: false`` on a boolean must win when nothing else is supplied.

    Symmetric with the CLI-side test above. ``None`` is the only
    distinguished "no default" value; every other value — including
    ``False`` and ``0`` — is a real default.
    """
    var = _bool_var(default=False)
    result = choose(var, cli_values={}, prompt_values={})
    assert result == Chosen(value=False, source="default:flag", origin="default")


def test_default_of_empty_string_is_a_real_default() -> None:
    """``default: ""`` on a string is a real default (though rarely useful).

    Documented deliberately: if a blueprint author writes an empty
    default, we honour it. The alternative — silently treating ``""``
    as "no default" — would create a subtle bug where a variable that
    *looks* optional suddenly fails as required.
    """
    var = _var(default="")
    result = choose(var, cli_values={}, prompt_values={})
    assert result == Chosen(value="", source="default:author", origin="default")


# ---------------------------------------------------------------------------
# Provenance labels
#
# ``source`` and ``origin`` are load-bearing for downstream error
# reporting: ``ValidationError.source`` shows the user *which input* to
# fix, and callers that branch on ``origin`` (e.g. "was this
# user-supplied?") rely on the enumerated values being stable.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("cli", "prompt", "expected_source", "expected_origin"),
    [
        ({"author": "c"}, {"author": "p"}, "cli:author", "cli"),
        ({"author": "c"}, {}, "cli:author", "cli"),
        ({}, {"author": "p"}, "prompt:author", "prompt"),
    ],
)
def test_source_label_identifies_the_winning_channel(
    cli: dict[str, object],
    prompt: dict[str, object],
    expected_source: str,
    expected_origin: str,
) -> None:
    """The label must say *exactly* which channel supplied the value.

    The format ``"<origin>:<variable_id>"`` is what appears in the
    error's ``source`` field, and users read that field to locate the
    fix. A generic label ("input") would be useless; a wrong label
    (prompt when actually CLI) would send them to the wrong place.
    """
    var = _var()
    result = choose(var, cli_values=cli, prompt_values=prompt)
    assert result is not None
    assert result.source == expected_source
    assert result.origin == expected_origin


def test_default_source_includes_variable_id() -> None:
    """The default's ``source`` names the variable, not just "default"."""
    var = _var(id="author", default="from-default")
    result = choose(var, cli_values={}, prompt_values={})
    assert result is not None
    assert result.source == "default:author"


# ---------------------------------------------------------------------------
# Totality — ``choose`` must not raise on any well-typed input
#
# The resolver's tight loop calls ``choose`` once per variable and
# aggregates failures from the *validator*. If ``choose`` ever raised,
# the aggregation would be lost: the first bad variable would abort the
# whole resolution, and the user would fix one field at a time. These
# tests pin that totality so a future refactor cannot reintroduce it.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cli_values",
    [
        {},
        {"author": None},
        {"author": ""},
        {"author": 0},
        {"author": False},
        {"unrelated": "value"},
        {"author": "ok", "extra": "ignored"},
    ],
)
@pytest.mark.parametrize(
    "prompt_values",
    [
        {},
        {"author": None},
        {"author": "from-prompt"},
        {"unrelated": "ignored"},
    ],
)
def test_choose_is_total_across_sparse_input_combinations(
    cli_values: dict[str, object],
    prompt_values: dict[str, object],
) -> None:
    """``choose`` never raises; it returns a ``Chosen`` or ``None``.

    The parameter matrix covers the interesting shapes callers actually
    produce: sparse dicts with unrelated keys (argparse namespaces after
    ``vars()``), ``None`` for "unset", falsy-but-real values. If any of
    these raised, the resolver would lose its aggregation guarantee.
    """
    var = _var(default="from-default")
    result = choose(var, cli_values=cli_values, prompt_values=prompt_values)
    # Either a winner or a clean absence — never an exception, never a
    # partially-populated result.
    assert result is None or isinstance(result, Chosen)


# ---------------------------------------------------------------------------
# Immutability of the inputs
#
# ``choose`` must not mutate its input mappings. This is a property that
# is easy to break by accident (e.g. a future maintainer writes
# ``cli_values.pop(k, None)`` to "check presence") and easy to test.
# ---------------------------------------------------------------------------


def test_choose_does_not_mutate_input_mappings() -> None:
    """Both source mappings must be byte-identical before and after the call.

    A caller that reuses the same mapping across multiple variables
    (which the resolver does — it passes the whole mapping to each call)
    must not observe an ever-shrinking dict as variables are resolved.
    """
    var = _var(default="from-default")
    cli = {"author": "from-cli", "other": "untouched"}
    prompt = {"unrelated": "still here"}
    cli_before = dict(cli)
    prompt_before = dict(prompt)

    choose(var, cli_values=cli, prompt_values=prompt)

    assert cli == cli_before
    assert prompt == prompt_before


# ---------------------------------------------------------------------------
# Ordering under multiple variables (the resolver's context)
#
# ``choose`` is per-variable; the resolver iterates. This test documents
# that the per-variable contract composes without state leaking between
# calls — a class of bug that would appear as "the second variable always
# gets the first variable's default".
# ---------------------------------------------------------------------------


def test_successive_calls_do_not_leak_state() -> None:
    """Two calls with different variables must return independent results."""
    a = _var(id="alpha", default="a-default")
    b = _var(id="beta", default="b-default")

    r1 = choose(a, cli_values={"alpha": "from-cli"}, prompt_values={})
    r2 = choose(b, cli_values={}, prompt_values={})

    assert r1 == Chosen(value="from-cli", source="cli:alpha", origin="cli")
    assert r2 == Chosen(value="b-default", source="default:beta", origin="default")
