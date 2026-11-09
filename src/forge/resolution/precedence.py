"""Select a raw value per variable from ordered sources.

This module has exactly one responsibility: **who wins**. It does not
coerce, validate, or fail — those belong to `validators` and `resolver`.
Keeping selection separate is what makes failure messages precise: the
resolver can say "the CLI value you passed for 'author' is invalid"
because `precedence` told it *which* value won, and where it came from.

Precedence (highest first)
---------------------------
    1. CLI flags / --var k=v
    2. Interactive prompt answers
    3. Blueprint default
    4. (absent) — required → error; optional → omitted

Design contract
---------------
* **Pure.** No I/O, no logging, no side effects. Callers can invoke
  `choose()` inside a tight loop or a test without worrying about state.
* **Total.** Every input combination yields either a `Chosen` or `None`.
  "Not provided" is a legitimate answer, not an exception.
* **Sparse-friendly.** `None` in a source mapping is treated as "not
  provided", so callers can use plain dicts populated by argparse without
  needing a `MISSING` sentinel type. `False`, `0`, and `""` are *real*
  values and win as such — this is the critical distinction that prevents
  the classic "`--flag false` didn't take effect" bug.
* **Self-describing.** `Chosen.source` is a human-readable label that
  flows straight into `ValidationError.source`, so any subsequent error
  can point the user at the exact source they need to fix.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from forge.models import VariableDefinition

# Sentinel used internally to distinguish "key present with value None"
# from "key absent". Exposed only as a module-private constant so callers
# never depend on it.
_UNSET: Final = object()


@dataclass(frozen=True, slots=True)
class Chosen:
    """A winning raw value and a label describing where it came from.

    Attributes
    ----------
    value
        The raw, *unvalidated* value. Downstream (`validators`) is
        responsible for coercion and rule checks. Keeping this raw is
        deliberate: it lets errors quote exactly what the user supplied.
    source
        Human-readable provenance label, e.g. ``cli:author``,
        ``prompt:author``, ``default:author``. Written into
        ``ValidationError.source`` when validation fails, so the user's
        error report points at the right place.
    origin
        Machine-readable provenance, useful for callers that want to
        branch on it (e.g. "was this value user-supplied?"). One of
        ``"cli"``, ``"prompt"``, ``"default"``.
    """

    value: Any
    source: str
    origin: str  # "cli" | "prompt" | "default"


def _lookup(source: Mapping[str, Any], key: str) -> Any:
    """Return the value at ``key`` or ``_UNSET``.

    Distinguishes three cases cleanly:

    * key absent              → `_UNSET`
    * key present, value None → `_UNSET` (treated as "not provided")
    * key present, value real → the value (even if falsy: 0, False, "")
    """
    if key not in source:
        return _UNSET
    value = source[key]
    return _UNSET if value is None else value


def choose(
    var: VariableDefinition,
    *,
    cli_values: Mapping[str, Any],
    prompt_values: Mapping[str, Any],
) -> Chosen | None:
    """Return the highest-precedence value present for ``var``, or ``None``.

    Parameters
    ----------
    var
        The variable definition. Only ``var.id`` and ``var.default`` are
        consulted here; type and rule checks are the validator's job.
    cli_values
        Values collected from CLI flags. Highest precedence.
    prompt_values
        Values collected from interactive prompts. Middle precedence.

    Returns
    -------
    `Chosen` for the winning source, or `None` if no source supplied a
    value and the blueprint declares no default. Whether that absence is
    an error (required) or fine (optional) is decided by the resolver —
    this function never fails.
    """
    # 1. CLI flags — highest precedence.
    value = _lookup(cli_values, var.id)
    if value is not _UNSET:
        return Chosen(value=value, source=f"cli:{var.id}", origin="cli")

    # 2. Interactive prompt answers — middle precedence.
    value = _lookup(prompt_values, var.id)
    if value is not _UNSET:
        return Chosen(value=value, source=f"prompt:{var.id}", origin="prompt")

    # 3. Blueprint default — lowest precedence.
    # A declared default of `None` is treated as "no default" so optional
    # variables without a default are cleanly omitted from resolved values.
    if var.default is not None:
        return Chosen(value=var.default, source=f"default:{var.id}", origin="default")

    # 4. Nothing supplied and no default → caller decides.
    return None
