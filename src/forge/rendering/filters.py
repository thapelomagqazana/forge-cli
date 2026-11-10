"""The filter allowlist — the *only* place templates can gain capability.

Design principles
-----------------
Templates are untrusted input. A filter is therefore a *capability grant*,
not a convenience. Every filter in this file has been chosen because it
is:

    * **Pure** — same input, same output, no clocks, no randomness, no
      environment or filesystem access. This is what makes rendering
      byte-deterministic and golden-testable.
    * **Total** — defined for every plausible input (or raising a clear
      `TypeError`/`ValueError` that the renderer converts into a
      structured `TemplateRenderError`).
    * **Boring** — string casing, whitespace, deterministic JSON. If a
      filter is exciting, it is probably a security risk.

Allowlist, never denylist
-------------------------
Jinja ships a large default filter set, some of which are non-deterministic
(`random`, `now`), some parse untrusted content (`urlize`, `xmlattr`), and
some expose attribute access (`attr`, `map(attribute=...)`, `selectattr`)
that contradicts the sandbox's attribute policy. We clear Jinja's defaults
and install *only* the map below. A denylist would inevitably miss a filter
added in a future Jinja release; an allowlist cannot.

Doc-sync invariant
------------------
`docs/reference/TEMPLATE_FILTERS.md` is the human-readable twin of this
file. `tests/unit/rendering/test_filter_docs.py` asserts that the set of
names documented equals the set of names in `ALLOWED_FILTERS`, so the two
cannot drift. Adding a filter requires updating both — that is the point.

`Undefined` awareness
---------------------
Exactly one filter in this file — ``default`` — must know about Jinja's
``Undefined`` sentinel. Every other filter operates on concrete values
that have already resolved; if the value were undefined, Jinja's
``StrictUndefined`` would have raised before the filter was called.

``default`` is the exception because its *entire purpose* is to
intercept the undefined case. Under ``StrictUndefined``, a missing
variable is an ``Undefined`` instance that raises on every operation
(``__str__``, ``__iter__``, ``__eq__``, ``__bool__``). The filter must
therefore check ``isinstance(value, Undefined)`` *before* performing any
comparison or conversion, or it will raise instead of substituting. The
ordering of the checks inside ``_default`` is load-bearing and is
documented at the call site; do not reorder them.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # Type-only import: ``jinja2.Environment`` appears only in the
    # ``install()`` signature, and importing it here means merely
    # importing this module (for ``ALLOWED_FILTERS`` — e.g. by the
    # doc-sync test, an LSP, or the rendering façade) does not pull
    # jinja2 into ``sys.modules``.
    from jinja2 import Environment

# ---------------------------------------------------------------------------
# Case / whitespace helpers
#
# Implemented as module-level functions (not lambdas) so they are picklable,
# introspectable, and named in tracebacks — a small thing that pays off
# whenever a template author reports "the `snake` filter is wrong".
# ---------------------------------------------------------------------------


def _upper(value: Any) -> str:
    return str(value).upper()


def _lower(value: Any) -> str:
    return str(value).lower()


def _title(value: Any) -> str:
    return str(value).title()


def _capitalize(value: Any) -> str:
    return str(value).capitalize()


def _trim(value: Any, chars: str | None = None) -> str:
    """Strip whitespace (or the given characters) from both ends."""
    return str(value).strip(chars)


def _replace(value: Any, old: str, new: str, count: int = -1) -> str:
    """`str.replace` with an explicit signature (no **kwargs surprises)."""
    return str(value).replace(old, new, count)


# ---------------------------------------------------------------------------
# Identifier casing
#
# These are the three casing idioms that appear in virtually every scaffold.
# They share one normalisation so `snake`, `kebab`, and `pascal` cannot
# disagree about where the word boundaries are. Implementation note: the
# two-regex trick below handles `HTTPServer` → `http_server` and
# `myVariable` → `my_variable` in one pass, which naive splitting misses.
# ---------------------------------------------------------------------------

_BOUNDARY_1 = re.compile(r"(.)([A-Z][a-z]+)")
_BOUNDARY_2 = re.compile(r"([a-z0-9])([A-Z])")


def _normalise_identifier(value: Any) -> str:
    text = str(value)
    text = _BOUNDARY_1.sub(r"\1_\2", text)
    text = _BOUNDARY_2.sub(r"\1_\2", text)
    return text.lower()


def _snake(value: Any) -> str:
    """`MyThing` → `my_thing`. The canonical form; others derive from it."""
    return _normalise_identifier(value)


def _kebab(value: Any) -> str:
    """`my_thing` → `my-thing`. Common for npm names and CLI slugs."""
    return _snake(value).replace("_", "-")


def _pascal(value: Any) -> str:
    """`my_thing` → `MyThing`. Python/TS class-name idiom."""
    return "".join(part.capitalize() for part in _snake(value).split("_") if part)


# ---------------------------------------------------------------------------
# Defaulting and whitespace control
#
# `default` deliberately only fires for undefined names, `None`, and `""`.
# Jinja's stock `default` also has a "boolean" mode that replaces every
# falsy value — which silently turns `0`, `False`, and `[]` into the
# default. That is a determinism-adjacent footgun (a variable set to `0`
# becomes the default), so we ship the *strict* variant only.
# ---------------------------------------------------------------------------


def _default(
    value: Any,
    fallback: Any = "",
    *_args: Any,
    **_kwargs: Any,
) -> Any:
    """Return `fallback` if `value` is undefined, `None`, or `""`.

    The three checks, in this exact order, and why the order matters:

    1. **``isinstance(value, Undefined)``** — a name that is not in the
       render context, or a failed attribute access that produced an
       ``Undefined`` sentinel. Under ``StrictUndefined``, every operation
       on such a value raises: ``__str__``, ``__iter__``, ``__eq__``,
       ``__bool__``, arithmetic, attribute access. ``isinstance`` is the
       *only* operation that is safe on an ``Undefined`` instance without
       triggering that machinery. If this check were moved after the
       equality checks below, the ``value == ""`` comparison would call
       ``StrictUndefined.__eq__``, which raises ``UndefinedError`` — and
       the whole point of ``default`` is to intercept before that
       happens. This check must come first.

    2. **``value is None``** — an explicit ``None`` is a legitimate value
       in most contexts, but for a ``default`` filter it usually means
       "not provided." Treating it as absent is the least-surprising
       behaviour for templates that pipe every optional variable through
       ``|default``.

    3. **``value == ""``** — an empty string. This is the "explicitly
       blank" case, which most templates want to fall back for.

    Extra positional/keyword args are accepted and ignored so that
    ``{{ x | default(y) }}`` and ``{{ x | default(y, true) }}`` do not
    blow up. The second argument's "boolean mode" is intentionally
    ignored — see module docstring.

    Why the ``*_args`` / ``**_kwargs`` and not bare ``*`` / ``**``
    ------------------------------------------------------------
    The original signature was ``def _default(value, fallback="", *_, **__)``.
    That *works* at runtime — Python accepts anonymous star-args — but
    neither ``*_`` nor ``**__`` gives the type checker a name it can
    annotate, which is why mypy's ``no-untyped-def`` rejected the
    function even after ``value`` and ``fallback`` were typed. Naming
    the catch-alls ``_args`` and ``_kwargs`` (both ``Any``) lets mypy
    verify the full signature without changing behaviour: they are
    still ignored, still accept any number of extra arguments, and the
    leading underscore still signals "deliberately unused."

    Why this fix exists
    -------------------
    Before this change, the filter was::

        return fallback if value is None or value == "" else value

    That worked for ``None`` and empty strings, but under
    ``StrictUndefined`` it *raised* for a missing name — because
    ``value == ""`` calls ``StrictUndefined.__eq__``, which raises.
    The result was that ``{{ who | default("world") }}`` with an absent
    ``who`` produced an ``UndefinedError`` instead of rendering
    ``"world"``. That contradicted the hint the renderer attaches to
    undefined-variable errors ("...or use |default(...) explicitly"),
    making the hint actively misleading.

    The ``isinstance`` check restores the documented behaviour. It is
    also the *only* place in this module where ``Undefined`` needs to
    be known about — every other filter only runs after the value has
    successfully resolved, so ``StrictUndefined``'s machinery has
    already done its job.
    """
    # Jinja is imported lazily, not at module top, so that pure-data
    # consumers of `ALLOWED_FILTERS` (a doc-sync test, an LSP tool that
    # wants the list of names) do not pay for importing jinja2 just to
    # read the dict. The cost of the import is paid the first time a
    # template actually renders, which is when jinja2 is imported
    # anyway by `environment.py`.
    from jinja2 import Undefined

    # Order is load-bearing — see the docstring above.
    if isinstance(value, Undefined):
        return fallback
    if value is None or value == "":
        return fallback
    return value


def _indent(value: Any, width: int = 4, first: bool = False) -> str:
    """Indent every line after the first by `width` spaces.

    `first=True` also indents the first line (needed when embedding a
    block inside an already-indented context). Implemented with
    `splitlines(keepends=True)` so mixed line endings survive intact.
    """
    pad = " " * width
    lines = str(value).splitlines(keepends=True)
    if not lines:
        return ""
    if not first:
        return lines[0] + "".join(pad + line for line in lines[1:])
    return "".join(pad + line for line in lines)


# ---------------------------------------------------------------------------
# Serialisation and structural helpers
# ---------------------------------------------------------------------------


def _tojson(value: Any) -> str:
    """Deterministic JSON.

    * `sort_keys=True` — dict iteration order is not guaranteed by
      arbitrary user input, so serialising without sorting is a
      determinism hazard. Sorting makes byte-equality meaningful.
    * `ensure_ascii=False` — non-ASCII text should survive round-trips.
    * `separators=(",", ":")` — no incidental whitespace, so goldens
      match across environments.
    """
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _length(value: Any) -> int:
    """`len()` — exposed explicitly so tests can assert the allowlist."""
    return len(value)


def _join(value: Iterable[Any], sep: str = "") -> str:
    """Join iterable items with a separator, converting each to `str`.

    Unlike Jinja's default `join`, we coerce every element rather than
    relying on Jinja's `soft_str`, so behaviour is identical whether the
    item is a `str`, an int, or an enum.
    """
    return sep.join(str(item) for item in value)


def _int(value: Any) -> int:
    """Integer conversion. Raises `ValueError`/`TypeError` on failure,
    which the renderer turns into a structured error with a hint."""
    return int(value)


def _string(value: Any) -> str:
    """`str()` — the explicit "give me the textual form" filter."""
    return str(value)


# ---------------------------------------------------------------------------
# The allowlist
#
# This dict IS the contract. Names on the left appear in templates and in
# `TEMPLATE_FILTERS.md`. Values are the pure callables above (or builtins
# whose behaviour is trivially pure and total, like `len`, `int`, `str`).
#
# Ordering groups related filters so a reviewer can scan by category.
# ---------------------------------------------------------------------------

ALLOWED_FILTERS: dict[str, Callable[..., Any]] = {
    # Case
    "upper": _upper,
    "lower": _lower,
    "title": _title,
    "capitalize": _capitalize,
    # Whitespace / simple string ops
    "trim": _trim,
    "replace": _replace,
    # Identifier casing (derived from a single normaliser so they agree)
    "snake": _snake,
    "kebab": _kebab,
    "pascal": _pascal,
    # Defaulting and indentation
    "default": _default,
    "indent": _indent,
    # Serialisation / structural
    "tojson": _tojson,
    "length": _length,
    "join": _join,
    "int": _int,
    "string": _string,
}


def install(environment: Environment) -> None:
    """Install the allowlist on an environment, replacing Jinja's defaults.

    `clear()` before `update()` is essential: Jinja copies its default
    filter map into every new environment at construction. Updating without
    clearing would leave `random`, `now`, `urlize`, `attr`, `map`, etc.
    silently available — exactly the surface this module exists to
    eliminate.

    Idempotent by design (used by `get_environment()` and by
    `ForgeSandboxedEnvironment.overlay()` to reinstall on clones).
    """
    environment.filters.clear()
    environment.filters.update(ALLOWED_FILTERS)
