"""Tests for required-variable enforcement in ``VariableResolver.resolve``.

The required/optional distinction is the boundary between two very
different user experiences:

- **Required + absent** → a ``ValidationError`` with code
  ``required.missing``, aggregated with every other missing variable
  so the user can fix them all in one edit.
- **Optional + absent** → *omitted* from ``ResolvedValues`` (not
  ``None``, not a default), so templates can use
  ``{% if author %}`` / ``value is defined`` cleanly.

That distinction is easy to break by accident. The natural failure
modes are subtle:

* Returning ``None`` for an optional variable instead of omitting it
  (breaks template conditionals and misleads callers that check
  ``if value is not None``).
* Raising on the *first* missing required variable instead of
  aggregating (breaks the "fix everything in one edit" UX the
  resolver's ``Result`` shape exists to deliver).
* Treating ``default: null`` as "default is None" instead of "no
  default" (breaks the required-vs-optional decision at the schema
  layer — ``null`` means absence, not a value).

The tests here pin each of those behaviours at the layer where they
live: ``VariableResolver.resolve``.

Scope
-----
These tests are **only** about required/optional semantics. They do
not test:

* Precedence (which source wins) — that is ``test_precedence.py``.
* Type coercion (string → boolean etc.) — that is
  ``test_type_coercion.py``.
* Name rules (project name, Python package, npm) — that is
  ``test_project_name.py`` et al.

The separation is deliberate. Each concern gets its own file so a
failure points at exactly one behaviour, and a change to one
behaviour has a visible blast radius in one file rather than in
"every resolver test".

Fixture strategy
----------------
Every test builds its variable definitions and its ``Blueprint``
inline via the small factories below. There are two reasons:

1. **Locality.** The rule under test is visible in the test body:
   the variable's ``required`` flag, its default, its type. A shared
   fixture that grows a field would break every test at once, which
   is worse than a few extra lines per test.

2. **Truth.** The factories construct *real* ``VariableDefinition``
   and ``Blueprint`` model instances, not duck-typed fakes. The
   resolver validates against those types, so the tests must supply
   them — a fake that happens to satisfy the attributes the resolver
   reads today would silently break the moment the resolver gains a
   new dependency on the model.

The factories are deliberately minimal: they only fill the fields
the resolver consults. If a future change makes the resolver read a
new field, the corresponding test will fail with a clear
``ValidationError`` (from Pydantic) or ``AttributeError`` (from the
resolver) — at the call site that is testing that behaviour, not
somewhere unrelated.
"""

from __future__ import annotations

import pytest

from forge.loading.result import Err, Ok
from forge.models import (
    Blueprint,
    Category,
    Compatibility,
    VariableDefinition,
    VariableType,
    VariableValidation,
)
from forge.resolution import ResolvedValues, VariableResolver

# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------


def _blueprint(*variables: VariableDefinition) -> Blueprint:
    """Build a minimal valid ``Blueprint`` with the given variables.

    The verification contract is a single ``required_file`` check —
    the smallest legal value (``minItems: 1`` in the schema). It is
    present because the model requires it, not because these tests
    care what it says.

    Everything else (id, name, version, category, description) is a
    valid placeholder. Blueprint identity is not what this file
    tests; the variables are.
    """
    return Blueprint(
        schema_version="1.0.0",
        id="sample.demo",
        name="Sample Demo",
        version="1.0.0",
        description="Test blueprint.",
        category=Category.api,
        variables=list(variables),
        verification=[{"id": "ok", "type": "required_file", "path": "README.md"}],
        compatibility=Compatibility(min_forge_version="1.0.0"),
    )


def _required(var_id: str, **overrides: object) -> VariableDefinition:
    """A required string variable, optionally overridden."""
    fields: dict[str, object] = {
        "id": var_id,
        "type": VariableType.string,
        "description": f"Test variable {var_id}.",
        "required": True,
    }
    fields.update(overrides)
    return VariableDefinition(**fields)  # type: ignore[arg-type]


def _optional(var_id: str, **overrides: object) -> VariableDefinition:
    """An optional string variable, optionally overridden."""
    fields: dict[str, object] = {
        "id": var_id,
        "type": VariableType.string,
        "description": f"Test variable {var_id}.",
        "required": False,
    }
    fields.update(overrides)
    return VariableDefinition(**fields)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Required + absent → error
# ---------------------------------------------------------------------------


def test_required_variable_with_no_value_returns_error() -> None:
    """A required variable with no source and no default is an error.

    The error code is ``required.missing`` (not a generic
    ``UserInputError`` message), so callers that route on category
    can treat it specially — e.g. the wizard suppresses it while the
    user is mid-typing.
    """
    bp = _blueprint(_required("author"))
    result = VariableResolver().resolve(bp, cli_args={})

    assert isinstance(result, Err)
    assert len(result.error) == 1
    err = result.error[0]
    assert err.code == "required.missing"
    assert err.path == "variables.author"


def test_required_variable_error_carries_actionable_hint() -> None:
    """The missing-variable error explains *how to supply the value*.

    "Required variable 'author' has no value" tells the user what is
    wrong but not what to do. The hint must name the mechanism —
    ``--var author=...`` — and, for typed variables, the shape of
    the value (boolean spellings, enum choices).
    """
    bp = _blueprint(_required("author"))
    result = VariableResolver().resolve(bp, cli_args={})

    assert isinstance(result, Err)
    hint = result.error[0].hint
    assert "--var" in hint
    assert "author" in hint


def test_required_boolean_variable_hint_names_true_false() -> None:
    """A boolean variable's hint names the accepted spellings.

    A user who has never read the docs does not know whether
    ``--var debug=1`` or ``--var debug=yes`` or ``--var debug=true``
    is the accepted form. The hint must say. Otherwise the "missing"
    error becomes a scavenger hunt.
    """
    bp = _blueprint(_required("debug", type=VariableType.boolean))
    result = VariableResolver().resolve(bp, cli_args={})

    assert isinstance(result, Err)
    assert "true" in result.error[0].hint.lower()
    assert "false" in result.error[0].hint.lower()


def test_required_enum_variable_hint_lists_choices() -> None:
    """An enum variable's hint lists the allowed values.

    Same reasoning as the boolean case: the hint must be
    self-contained. Sending the user to a doc to find the allowed
    values is a worse experience than naming them inline.
    """
    bp = _blueprint(
        _required(
            "mode",
            type=VariableType.enum,
            validation=VariableValidation(enum=["dev", "staging", "prod"]),
        )
    )
    result = VariableResolver().resolve(bp, cli_args={})

    assert isinstance(result, Err)
    hint = result.error[0].hint
    for choice in ("dev", "staging", "prod"):
        assert choice in hint


# ---------------------------------------------------------------------------
# Required + supplied → Ok
# ---------------------------------------------------------------------------


def test_required_variable_with_supplied_value_succeeds() -> None:
    """Required means *enforced*, not *broken by default*."""
    bp = _blueprint(_required("author"))
    result = VariableResolver().resolve(bp, cli_args={"author": "alice"})

    assert isinstance(result, Ok)
    assert result.value["author"] == "alice"


def test_required_variable_with_default_succeeds_without_input() -> None:
    """A required variable with a default is satisfied by the default.

    "Required" means "must have *a* value", not "must be supplied by
    the user". A default is a value. Failing this case would break
    every blueprint with sensible defaults, which is most of them.
    """
    bp = _blueprint(_required("author", default="forge"))
    result = VariableResolver().resolve(bp, cli_args={})

    assert isinstance(result, Ok)
    assert result.value["author"] == "forge"


# ---------------------------------------------------------------------------
# Optional + absent → omitted (not None)
# ---------------------------------------------------------------------------


def test_optional_variable_absent_is_omitted_from_resolved_values() -> None:
    """Optional + absent is *omitted*, not set to ``None``.

    This is the load-bearing distinction. If the resolver returned
    ``{"author": None}`` instead of ``{}``, every downstream
    consumer that does ``if "author" in values`` would be wrong,
    and templates using ``{% if author %}`` would take the "falsy"
    branch — which is correct for a value of ``None`` but wrong for
    the *semantic* question "did the user supply this variable?".

    Omission is the honest answer: the variable was not resolved
    because there was nothing to resolve.
    """
    bp = _blueprint(_optional("author"))
    result = VariableResolver().resolve(bp, cli_args={})

    assert isinstance(result, Ok)
    assert "author" not in result.value.as_dict()


def test_optional_variable_supplied_is_present() -> None:
    """Optional + supplied is a real value in ``ResolvedValues``."""
    bp = _blueprint(_optional("author"))
    result = VariableResolver().resolve(bp, cli_args={"author": "bob"})

    assert isinstance(result, Ok)
    assert result.value["author"] == "bob"


def test_optional_variable_with_default_uses_default() -> None:
    """Optional + no input + a default → the default is resolved.

    A "default" is a *real* value the blueprint author chose to
    provide. Treating it as "no value" (because the variable is
    optional) would discard the author's intent and break every
    blueprint whose defaults are *meant* to be applied.
    """
    bp = _blueprint(_optional("author", default="forge"))
    result = VariableResolver().resolve(bp, cli_args={})

    assert isinstance(result, Ok)
    assert result.value["author"] == "forge"


# ---------------------------------------------------------------------------
# ``None`` as default → no default
# ---------------------------------------------------------------------------


def test_default_of_none_means_no_default_for_required() -> None:
    """``default: null`` on a required variable is still required.

    ``None`` is the resolver's "no default" sentinel — an explicit
    ``null`` in YAML is the same as omitting the field. A required
    variable with ``default: null`` therefore has *no* source to
    draw from and *must* error when no CLI or prompt value is
    supplied.

    The alternative interpretation ("the default is ``None``, so the
    variable is satisfied") would silently defeat the required flag
    for any blueprint author who wrote ``default: null`` meaning
    "no default". That is a footgun, not a feature.
    """
    bp = _blueprint(_required("author", default=None))
    result = VariableResolver().resolve(bp, cli_args={})

    assert isinstance(result, Err)
    assert result.error[0].code == "required.missing"


def test_default_of_none_means_no_default_for_optional() -> None:
    """``default: null`` on an optional variable is treated as absent.

    Same sentinel reasoning as the required case, and the same
    consequence for the resolved mapping: the key is *omitted*, not
    set to ``None``.
    """
    bp = _blueprint(_optional("author", default=None))
    result = VariableResolver().resolve(bp, cli_args={})

    assert isinstance(result, Ok)
    assert "author" not in result.value.as_dict()


# ---------------------------------------------------------------------------
# Aggregation — every missing variable in one error list
# ---------------------------------------------------------------------------


def test_multiple_missing_required_variables_are_aggregated() -> None:
    """Every missing required variable appears in the error list.

    The resolver's ``Result`` shape exists so the caller can render
    *all* problems at once. A resolver that raised on the first
    missing variable would still be "correct" in a narrow sense but
    would produce a miserable UX: fix one, rerun, fix the next, and
    so on. Aggregation is the feature.
    """
    bp = _blueprint(
        _required("author"),
        _required("license"),
        _required("version_control"),
    )
    result = VariableResolver().resolve(bp, cli_args={})

    assert isinstance(result, Err)
    assert len(result.error) == 3
    paths = {err.path for err in result.error}
    assert paths == {
        "variables.author",
        "variables.license",
        "variables.version_control",
    }
    # Every error is the *right* kind of error — not a cascade of
    # downstream failures from the first missing value.
    assert all(err.code == "required.missing" for err in result.error)


def test_missing_and_present_mix_is_reported_correctly() -> None:
    """Only the *missing* required variables are in the error list.

    A required variable that *was* supplied must not appear in
    ``Err``, even when its siblings are missing. Otherwise a user
    fixing one field at a time would see errors that were not
    actually about the field they just changed.
    """
    bp = _blueprint(
        _required("author"),
        _required("license"),
        _required("version_control"),
    )
    result = VariableResolver().resolve(bp, cli_args={"author": "alice"})

    assert isinstance(result, Err)
    missing_paths = {err.path for err in result.error}
    assert missing_paths == {"variables.license", "variables.version_control"}


def test_aggregation_includes_optional_validation_failures() -> None:
    """Errors from *optional* variables aggregate alongside missing required ones.

    An optional variable that has an invalid value is still an
    error — the user typed something, and it did not pass. The
    fact that the variable is optional means the user is allowed to
    *omit* it, not that the user is allowed to supply garbage.

    This test pins that the resolver reports both kinds in one
    ``Err`` list, so the caller's rendering logic does not need to
    special-case "which errors came from which required-ness".
    """
    bp = _blueprint(
        _required("author"),  # will be missing
        _optional(
            "mode",
            type=VariableType.enum,
            validation=VariableValidation(enum=["dev", "prod"]),
        ),
    )
    result = VariableResolver().resolve(bp, cli_args={"mode": "bogus"})

    assert isinstance(result, Err)
    codes = {err.code for err in result.error}
    assert "required.missing" in codes
    assert "enum.not_allowed" in codes


# ---------------------------------------------------------------------------
# Ordering — deterministic error order
# ---------------------------------------------------------------------------


def test_errors_are_sorted_by_path() -> None:
    """Errors come back sorted by path.

    Deterministic ordering makes CLI output diffable, snapshot tests
    stable, and bug reports reproducible. The resolver sorts before
    returning (see ``VariableResolver.resolve``), so the caller does
    not have to.
    """
    bp = _blueprint(
        _required("zeta"),
        _required("alpha"),
        _required("mike"),
    )
    result = VariableResolver().resolve(bp, cli_args={})

    assert isinstance(result, Err)
    paths = [err.path for err in result.error]
    assert paths == sorted(paths)


# ---------------------------------------------------------------------------
# Interplay with prompts (documented, not re-tested here)
# ---------------------------------------------------------------------------


def test_prompt_supplied_value_satisfies_required() -> None:
    """A value from the prompt channel counts as "supplied".

    Interactive mode is not a special case for requiredness — the
    resolver does not care *which* source supplied the value, only
    that a value exists. This test proves that invariant at the
    resolver layer, where it lives.

    The wizard's own decision to *prompt* for required variables is
    a command-layer concern (``_collect_interactive_answers``),
    tested in ``tests/unit/cli/test_create_dual_mode.py``.
    """
    bp = _blueprint(_required("author"))
    result = VariableResolver().resolve(
        bp,
        cli_args={},
        prompt_answers={"author": "carol"},
    )

    assert isinstance(result, Ok)
    assert result.value["author"] == "carol"


# ---------------------------------------------------------------------------
# Result shape
# ---------------------------------------------------------------------------


def test_resolver_returns_ok_on_success() -> None:
    """The success case is ``Ok(ResolvedValues)``, not a bare dict.

    ``ResolvedValues`` is the resolver-owned type that downstream
    code annotates parameters with to get the "no unvalidated input
    reached the renderer" guarantee (ADR-0011). Returning a plain
    dict would silently undo that guarantee.
    """
    bp = _blueprint(_required("author"))
    result = VariableResolver().resolve(bp, cli_args={"author": "alice"})

    assert isinstance(result, Ok)
    assert isinstance(result.value, ResolvedValues)


def test_resolver_returns_err_on_failure() -> None:
    """The failure case is ``Err(list[ValidationError])``, not an exception.

    A raised exception is easy to miss at a call site, and the CLI
    handler must catch it anyway to render the message. ``Err``
    makes the failure part of the function's *signature* — mypy
    requires callers to check it — while still carrying the same
    structured information an exception would.
    """
    bp = _blueprint(_required("author"))
    result = VariableResolver().resolve(bp, cli_args={})

    assert isinstance(result, Err)
    assert isinstance(result.error, list)
    assert result.error  # never empty when Err is returned


# ---------------------------------------------------------------------------
# Empty blueprint
# ---------------------------------------------------------------------------


def test_blueprint_with_no_variables_succeeds_with_empty_result() -> None:
    """A variable-less blueprint resolves to an empty ``ResolvedValues``.

    This is the legitimate case (a blueprint that only writes static
    templates), and the resolver must handle it without special
    casing. ``Ok`` with no keys — not ``Err``, not ``None``.
    """
    bp = _blueprint()
    result = VariableResolver().resolve(bp, cli_args={})

    assert isinstance(result, Ok)
    assert result.value.as_dict() == {}


# ---------------------------------------------------------------------------
# Purity — resolve() must not mutate its inputs
# ---------------------------------------------------------------------------


def test_resolve_does_not_mutate_input_mappings() -> None:
    """CLI and prompt mappings are not modified by the call.

    The command layer reuses the same ``--var`` dict across multiple
    invocations (e.g. in a shell loop that calls ``forge create``
    repeatedly). A resolver that popped keys "to consume them" would
    silently break every call after the first.
    """
    bp = _blueprint(_required("author"), _optional("note"))
    cli = {"author": "alice"}
    prompt = {"note": "hello"}
    cli_before = dict(cli)
    prompt_before = dict(prompt)

    VariableResolver().resolve(bp, cli_args=cli, prompt_answers=prompt)

    assert cli == cli_before
    assert prompt == prompt_before


# ---------------------------------------------------------------------------
# Idempotency — resolve() is a pure function of its inputs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cli_args",
    [
        {},
        {"author": "alice"},
        {"author": ""},
        {"author": "  "},  # whitespace-only — still invalid per the pattern
    ],
)
def test_repeated_calls_produce_identical_results(
    cli_args: dict[str, str],
) -> None:
    """Two calls with the same inputs produce the same output.

    Purity is a design property of the resolver — no I/O, no cache,
    no clock. This test is cheap and catches the class of bug where
    a well-meaning optimisation introduces hidden state (a
    module-level cache that goes stale, a shared mutable default
    that accumulates entries, etc.).
    """
    bp = _blueprint(_required("author"))
    first = VariableResolver().resolve(bp, cli_args=cli_args)
    second = VariableResolver().resolve(bp, cli_args=cli_args)

    # ``Ok`` and ``Err`` compare by value (dataclasses), so equality
    # of the outer container plus equality of the payload is enough.
    if isinstance(first, Ok):
        assert isinstance(second, Ok)
        assert first.value.as_dict() == second.value.as_dict()
    else:
        assert isinstance(second, Err)
        assert [(e.path, e.code) for e in first.error] == [(e.path, e.code) for e in second.error]
