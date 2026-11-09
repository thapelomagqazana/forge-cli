"""Explicit, per-type coercion of raw user input into runtime values.

Why this module exists
----------------------
CLI flags, environment variables, and YAML defaults all arrive as
*strings* (or as whatever a `.yaml` loader produced). Before any rule
can be applied, each value must be turned into the type the blueprint
declares. That sounds trivial until you write it twice and get two
different answers.

Two failure modes are endemic to "just cast it":

    >>> bool("false")
    True
    >>> int("1_000")  # works, but was it intended?
    1000

The first is the classic "`--flag false` turned the flag on" bug. The
second is a subtle case where two people on the same team disagree about
whether it's allowed. Both are *policy* decisions, and policy belongs in
exactly one place.

Design contract
---------------
* **One spelling per type.** For booleans we accept a small, explicit
  set of words (`true/false/yes/no/on/off/1/0`) and nothing else. For
  strings, only strings pass — we never silently stringify an int, because
  doing so hides real bugs (e.g. YAML `1.10` parsing as `1.1`). For enums,
  membership is checked here because "the enum of allowed string values"
  *is* the type.
* **Explicit failure.** Every function returns either the coerced value
  or a `ValidationError`. Never raises for user input. The caller
  aggregates; coercion only reports the *first* structural problem for
  a given value, because cascading "and also it wasn't a boolean" messages
  are noise.
* **Hint on every error.** A coercion failure without a hint is a bug;
  we assert that invariant in a shared helper.
* **Consistent error codes.** ``type.mismatch`` and ``enum.not_allowed``
  are the same codes the rest of the resolution layer emits, so callers
  that route on `.code` see one vocabulary across the whole pipeline.

Relationship to the rest of the module
-------------------------------------
`validators.validate_variable` calls into this module for the *structural*
phase, then applies pattern and named-validator rules on the coerced
value. The interactive prompt engine also imports the boolean/string
helpers for its own input parsing, so what a user types in the wizard is
interpreted identically to what they'd pass on the command line.

Keeping coercion isolated means "what does 'yes' mean" has exactly one
answer across the entire application.
"""

from __future__ import annotations

from forge.loading.errors import ValidationError

# ---------------------------------------------------------------------------
# Boolean spellings
# ---------------------------------------------------------------------------

# The full, closed set of spellings we accept. Adding to this set is a
# deliberate policy decision — e.g. we intentionally do *not* accept "1.0"
# or "yep", because a permissive boolean is exactly the kind of thing that
# ends up turning `--enable-analytics false` into "enabled". If a user
# needs a wider input surface, that belongs in their shell wrapper, not
# in every Forge invocation.
_TRUE_SPELLINGS = frozenset({"true", "1", "yes", "y", "on"})
_FALSE_SPELLINGS = frozenset({"false", "0", "no", "n", "off"})


def _error(
    *,
    path: str,
    message: str,
    hint: str,
    source: str,
    code: str,
    extra: dict[str, object] | None = None,
) -> ValidationError:
    """Build a ValidationError with the invariant (hint non-empty) enforced."""
    if not hint:
        # A programmer error, not a user error -- a coercion path
        # forgot to supply a hint. ``RuntimeError`` (not
        # ``UserInputError``) because this is a bug in Forge, not in
        # the user's blueprint. Failing loudly here beats emitting a
        # ValidationError with a blank hint that the user sees as an
        # empty line under their error message.
        raise RuntimeError(
            f"coercion error helper called without a hint " f"(path={path!r}, code={code!r})"
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
# Boolean
# ---------------------------------------------------------------------------


def to_boolean(
    raw: object,
    *,
    path: str,
    source: str,
) -> bool | ValidationError:
    """Coerce `raw` to a bool using the explicit spellings above.

    Accepts
    -------
    * Real ``bool`` values (from YAML defaults and prompt answers).
    * Case-insensitive strings from the closed set above (with surrounding
      whitespace stripped, since shells frequently pass ``" true "``).

    Rejects
    -------
    * Integers (`0`/`1` — the int/bool confusion bug class).
    * Anything else, including the empty string, ``"maybe"``, ``"None"``,
      and numeric strings like ``"0.0"``.
    """
    if isinstance(raw, bool):
        # Note: check this *before* int, since `isinstance(True, int)`
        # is True. If we ever accept int for booleans, bool must be
        # matched first.
        return raw
    if isinstance(raw, str):
        lowered = raw.strip().lower()
        if lowered in _TRUE_SPELLINGS:
            return True
        if lowered in _FALSE_SPELLINGS:
            return False
        return _error(
            path=path,
            message=f"expected a boolean, got {raw!r}",
            hint="Use one of: true, false, yes, no, on, off.",
            source=source,
            code="type.mismatch",
            extra={"accepted": sorted(_TRUE_SPELLINGS | _FALSE_SPELLINGS)},
        )
    return _error(
        path=path,
        message=f"expected a boolean, got {type(raw).__name__}",
        hint="Provide a boolean value (true/false) on the command line.",
        source=source,
        code="type.mismatch",
    )


# ---------------------------------------------------------------------------
# String
# ---------------------------------------------------------------------------


def to_string(
    raw: object,
    *,
    path: str,
    source: str,
) -> str | ValidationError:
    """Require that `raw` is already a string.

    We deliberately do **not** call ``str(raw)``. Silent stringification
    is how a YAML author writes ``version: 1.10`` meaning "1.10" and gets
    ``"1.1"`` — the number, then re-stringified. It is also how a stray
    ``None`` becomes ``"None"`` in a rendered template.

    If a caller has a non-string value it wants to treat as a string,
    they should convert explicitly at the call site — not implicitly
    here. Explicitness is the whole point.
    """
    if isinstance(raw, str):
        return raw
    return _error(
        path=path,
        message=f"expected a string, got {type(raw).__name__}",
        hint="Wrap the value in quotes on the command line.",
        source=source,
        code="type.mismatch",
    )


# ---------------------------------------------------------------------------
# Enum
# ---------------------------------------------------------------------------


def to_enum(
    raw: object,
    allowed: tuple[str, ...],
    *,
    path: str,
    source: str,
) -> str | ValidationError:
    """Coerce `raw` to a string, then assert it is one of `allowed`.

    Enum membership is checked *here* (rather than in `validators`) because
    the set of allowed values *is* the type for an enum variable. Splitting
    "is it a string" from "is it a legal string" would just create two
    places where the allowed list must be threaded through.

    The `allowed` tuple is provided by the variable definition. If it's
    empty, this is a blueprint authoring error and the caller (validators)
    reports it before calling here — that invariant keeps this function's
    contract clean ("the caller already confirmed there are choices").
    """
    # Reuse `to_string` so the "not a string at all" path emits the exact
    # same message as a plain string variable — one wording, one place.
    coerced = to_string(raw, path=path, source=source)
    if isinstance(coerced, ValidationError):
        return coerced

    if coerced not in allowed:
        # Sorted `allowed` in the hint is a small touch that dramatically
        # improves readability when the list has more than three items.
        hint = f"Allowed: {', '.join(allowed)}."
        return _error(
            path=path,
            message=f"'{coerced}' is not one of the allowed values",
            hint=hint,
            source=source,
            code="enum.not_allowed",
            extra={"allowed": list(allowed), "got": coerced},
        )
    return coerced
