"""Per-variable validation against a `VariableDefinition`.

This module answers exactly one question:

    Given a variable's declared rules and one candidate value,
    is the value legal — and if not, why?

It does **not** decide precedence (that is `precedence.choose`), does not
mutate its inputs, and does not touch I/O. It is a pure function from
`(definition, value)` to `value | ValidationError`. That makes it trivially
unit-testable and safe to call from the resolver's tight loop, from the
interactive prompt engine (real-time validation), and from any future
batch tooling.

Layering
--------
Validation happens in three ordered phases. Order matters because each
phase assumes a stronger precondition than the last, and because the
first error a user sees should be the most fundamental one:

    1. **Structural**  — coerce the raw value into the declared runtime
                         type (string / bool / enum). Fails with
                         ``type.mismatch`` if impossible.

    2. **Declarative** — apply rules declared *by the blueprint author*:
                         ``validation.pattern`` (full-match regex) and
                         ``validation.enum`` (allowed values). Enum
                         membership is checked inside the coercion step
                         because "enum of strings" *is* the type, but
                         pattern is a separate concern and lives here.

    3. **Named**       — apply the small set of *domain-specific*
                         validators (`project_name`, `python_package`,
                         `npm_package`) that ship with Forge. These are
                         keyed by variable id, not by rule field, because
                         the same identifier may appear across many
                         blueprints and must behave identically.

If all three phases pass, the *coerced* value is returned (not the raw
input) so downstream code receives the type it will actually render
against — never a string that happens to look like a boolean.

Design guarantees
-----------------
* **Deterministic.** No time, randomness, environment, or filesystem.
* **Non-throwing (mostly).** Invalid input yields a `ValidationError`,
  never an exception, *except* when the blueprint itself declares a
  broken regex. That is a blueprint authoring error, not a user error,
  so we surface it as a distinct ``blueprint.pattern.invalid`` code and
  the loader's hint catalogue points at the blueprint file — not at the
  user's input.
* **Fully reported.** A single call returns at most one error per
  variable (the most fundamental one). Aggregation across variables is
  the resolver's job; the invariant "one variable, one error" keeps
  messages precise and avoids drowning the user in cascades.
* **Hint on every error.** `ValidationError.hint` is required by the
  shape; this module never leaves it empty.
"""

from __future__ import annotations

import re

from forge.loading.errors import ValidationError
from forge.models import VariableDefinition, VariableType
from forge.resolution.coercion import to_boolean, to_enum, to_string
from forge.resolution.names import NAMED_VALIDATORS

# Distinct error codes let callers branch on failure category without
# parsing prose. Keep this list aligned with docs/reference/VARIABLE_RESOLUTION.md.
_CODE_TYPE_MISMATCH = "type.mismatch"
_CODE_ENUM_NOT_ALLOWED = "enum.not_allowed"
_CODE_PATTERN_INVALID = "pattern.invalid"
_CODE_PATTERN_BROKEN = "blueprint.pattern.invalid"
_CODE_NAME_INVALID = "name.invalid"


def _error(
    *,
    path: str,
    message: str,
    hint: str,
    source: str,
    code: str,
    extra: dict[str, object] | None = None,
) -> ValidationError:
    """Build a ValidationError with the module's invariant (hint non-empty).

    Centralised so we cannot accidentally ship an error without a hint.
    """
    if not hint:
        # The docstring says "invariant", so enforce it with a real
        # raise -- ``python -O`` strips ``assert``, and a blank-hint
        # ValidationError slipping past under ``-O`` is exactly the
        # bug this helper exists to prevent. ``RuntimeError`` (not
        # ``UserInputError``) because this is a Forge bug, not a
        # user-blueprint bug; include ``path`` and ``code`` so the
        # traceback names the offending coercion site.
        raise RuntimeError(
            "coercion error helper called without a hint " f"(path={path!r}, code={code!r})"
        )
    return ValidationError(
        path=path,
        message=message,
        hint=hint,
        source=source,
        code=code,
        extra=extra or {},
    )


# ---------------------------------------------------------------------------
# Phase 1 — structural coercion
# ---------------------------------------------------------------------------


def _coerce_to_declared_type(
    var: VariableDefinition,
    raw: object,
    *,
    path: str,
    source: str,
) -> object | ValidationError:
    """Coerce `raw` into the variable's declared runtime type.

    Each branch delegates to `coercion`, which owns the exact spelling
    rules (e.g. which strings count as `true`). Centralising that means
    the CLI, the wizard, and future API paths never disagree on what
    "yes" means.
    """
    if var.type is VariableType.boolean:
        return to_boolean(raw, path=path, source=source)

    if var.type is VariableType.enum:
        # An enum variable without allowed values is a blueprint authoring
        # error, not a user error. Report it as such so the fix lands in
        # the blueprint, not in the user's command line.
        if not var.validation or not var.validation.enum:
            return _error(
                path=path,
                message="enum variable has no allowed values in the blueprint",
                hint="Add at least one value to `validation.enum` in the blueprint definition.",
                source=source,
                code=_CODE_PATTERN_BROKEN,
            )
        allowed = tuple(var.validation.enum)
        return to_enum(raw, allowed, path=path, source=source)

    # string (the fallback — every other type is handled explicitly above)
    return to_string(raw, path=path, source=source)


# ---------------------------------------------------------------------------
# Phase 2 — declarative pattern
# ---------------------------------------------------------------------------


def _apply_pattern(
    var: VariableDefinition,
    value: str,
    *,
    path: str,
    source: str,
) -> ValidationError | None:
    """Return a validation error if `value` fails the declared pattern, else None.

    Rules
    -----
    * Full match, not partial. A blueprint author writing ``[a-z]+``
      almost certainly means "every character is a lowercase letter",
      not "contains at least one". `re.fullmatch` is the safer default
      and is documented in the reference doc.
    * Broken patterns in the blueprint are surfaced with a distinct code
      and a hint that points at the blueprint, not at the user's value.
    """
    if not var.validation or not var.validation.pattern:
        return None

    pattern = var.validation.pattern
    try:
        compiled = re.compile(pattern)
    except re.error as exc:
        return _error(
            path=path,
            message=f"blueprint pattern is invalid: {exc}",
            hint="Fix `validation.pattern` in the blueprint's variable definition.",
            source=source,
            code=_CODE_PATTERN_BROKEN,
            extra={"pattern": pattern},
        )

    if compiled.fullmatch(value) is None:
        return _error(
            path=path,
            message=f"value {value!r} does not match the required pattern",
            hint=f"Value must match: {pattern}",
            source=source,
            code=_CODE_PATTERN_INVALID,
            extra={"pattern": pattern},
        )
    return None


# ---------------------------------------------------------------------------
# Phase 3 — named validators
# ---------------------------------------------------------------------------


def _apply_named_validator(
    var: VariableDefinition,
    value: object,
    *,
    path: str,
    source: str,
) -> ValidationError | None:
    """Apply the domain-specific validator registered under `var.id`, if any.

    Only string-typed variables participate; the named validators are
    about *identifier shape*, which is a string concept. A boolean
    variable accidentally named `project_name` is a blueprint bug, and
    silently skipping the check would hide it — so we surface that too.
    """
    validator = NAMED_VALIDATORS.get(var.id)
    if validator is None:
        return None

    if not isinstance(value, str):
        # The user (or blueprint) mis-wired the variable. Better to fail
        # loudly than to skip a rule that was clearly intended to run.
        return _error(
            path=path,
            message=f"named validator '{var.id}' requires a string value",
            hint="Change the variable's type to `string` in the blueprint, or rename it.",
            source=source,
            code=_CODE_TYPE_MISMATCH,
        )

    problem = validator(value)
    if problem is None:
        return None

    message, hint = problem
    return _error(
        path=path,
        message=message,
        hint=hint,
        source=source,
        code=_CODE_NAME_INVALID,
        extra={"variable": var.id},
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def validate_variable(
    var: VariableDefinition,
    raw: object,
    *,
    path: str,
    source: str,
) -> object | ValidationError:
    """Coerce and validate a single variable, returning the typed value.

    Parameters
    ----------
    var
        The variable's definition — the sole source of its rules.
    raw
        The winning raw value selected by `precedence.choose`. May be a
        string (from CLI), a native type (from a default in the
        blueprint), or a boolean (from a prompt).
    path
        Dotted path used in errors, e.g. ``variables.author``. Kept as a
        parameter (rather than derived from `var.id`) so callers in nested
        contexts (e.g. blueprint-defined "environments") can supply their
        own prefix without this function knowing about nesting.
    source
        Provenance label from `Chosen.source` (``cli:author`` etc.).
        Flows straight through to `ValidationError.source` so the user
        sees exactly which input to fix.

    Returns
    -------
    The *coerced* value (a `str`, `bool`, or enum-member `str`) on
    success, or a single `ValidationError` describing the first
    fundamental problem found. The resolver aggregates across variables.
    """
    # Phase 1 — structural.
    coerced = _coerce_to_declared_type(var, raw, path=path, source=source)
    if isinstance(coerced, ValidationError):
        return coerced

    # Phase 2 — declarative pattern. Runs only on strings, since a regex
    # over booleans is meaningless. Enum values are strings, so they are
    # covered — but the enum membership check already ran in phase 1.
    if isinstance(coerced, str):
        pattern_error = _apply_pattern(var, coerced, path=path, source=source)
        if pattern_error is not None:
            return pattern_error

    # Phase 3 — named validators (project name, python package, etc.).
    named_error = _apply_named_validator(var, coerced, path=path, source=source)
    if named_error is not None:
        return named_error

    return coerced
