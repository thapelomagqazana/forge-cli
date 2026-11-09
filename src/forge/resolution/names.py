"""Named, domain-specific validators for common blueprint variable ids.

Why a dedicated module?
    Some variables — `project_name`, `python_package`, `npm_package` —
    appear across many blueprints and must behave *identically* wherever
    they appear. If each blueprint author wrote their own regex, we would
    have as many variants as blueprints, and "the wizard accepted it but
    the CLI rejected it" would become a routine bug class.

    These validators are keyed by *variable id*, not by a rule field in
    the blueprint. That is deliberate: the identifier `python_package`
    has one meaning across the entire ecosystem, and attaching rules to
    the identifier keeps that meaning stable. Blueprint authors can still
    add stricter rules via `validation.pattern`; that only *narrows* what
    this module accepts, never widens it.

Design contract
---------------
Every validator in this module:

* Accepts a `str` and returns ``None`` on success or a ``(message, hint)``
  tuple on failure.
* Is **pure**: no I/O, no caching, no environment, no time.
* Is **total**: never raises, even on exotic inputs (empty, huge, unicode).
* Is **deterministic**: same input always yields the same result.
* Produces **actionable** messages: the message names *what* is wrong,
  the hint names *how* to fix it, ideally with a concrete example.

Why a `(message, hint)` tuple and not `ValidationError` directly?
    Because this module is used in two very different contexts:

    * the resolver, which wraps the tuple into a full `ValidationError`
      with `path`, `source`, and `code`; and
    * the interactive prompt engine, which needs *only* the hint text to
      show inline feedback in real time.

    Returning a bare tuple keeps the module free of loader-layer imports
    (no import cycle), and gives both consumers exactly what they need.

Why `None` on success and not `True`?
    `None` is the standard "no error" sentinel in Python (`re.match`,
    `dict.get`, etc.), and it makes the call site read naturally:

        if problem := project_name(value): ...
"""

from __future__ import annotations

import keyword
import re

# A validated name plus its fix hint — the return type of every validator
# in this module. Kept as a module alias so call sites and tests can refer
# to it by name rather than repeating the tuple type.
Problem = tuple[str, str]


# ---------------------------------------------------------------------------
# Project name — the top-level directory and display name of a generated
# project. Permissive across ecosystems, but strict enough to be safe on
# every common filesystem and as a URL slug.
# ---------------------------------------------------------------------------

# Constraints, explained (this is the whole spec):
#   * starts with a lowercase letter (no leading digits — those are hard
#     to type and often confuse shell completion);
#   * body is lowercase letters, digits, or hyphens;
#   * does not end with a hyphen (avoids `foo-` which many tools reject);
#   * single-character names are allowed (e.g. "a", "x").
_PROJECT = re.compile(r"^(?:[a-z]|[a-z][a-z0-9-]*[a-z0-9])$")


def project_name(value: str) -> Problem | None:
    """Validate a project name (also usable as a directory name).

    Accepted:   ``my-service``, ``svc``, ``api2``
    Rejected:   ``My Project`` (spaces, uppercase), ``1api`` (leading digit),
                ``api-`` (trailing hyphen), ``my_service`` (underscore —
                discouraged because npm consumers expect hyphens)
    """
    if not value:
        return (
            "project name is required",
            "Pick a name like 'my-service'.",
        )
    if _PROJECT.match(value):
        return None
    if value != value.lower():
        return (
            "project name must be lowercase",
            f"Lowercase the name, e.g. '{value.lower()}'.",
        )
    if value[0].isdigit():
        return (
            "project name must start with a letter",
            "Prepend a letter, e.g. 'app-' + the current name.",
        )
    if value.endswith("-"):
        return (
            "project name must not end with a hyphen",
            "Remove the trailing hyphen.",
        )
    return (
        "invalid project name",
        "Use lowercase letters, digits, and hyphens; start with a letter; "
        "do not end with a hyphen. Example: 'my-service'.",
    )


# ---------------------------------------------------------------------------
# Python package name — a directory name that must also be a valid
# identifier for `import ...`. Stricter than project names: no hyphens,
# no leading digit, not a keyword.
# ---------------------------------------------------------------------------

# The full identifier rule is "letter or underscore followed by letters,
# digits, or underscores". We check `isidentifier()` at call time (which
# handles unicode correctly), and use this regex only as a fast pre-check
# for the ASCII common case and to give a specific error message.
_PY_IDENT_ASCII = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def python_package(value: str) -> Problem | None:
    """Validate a Python package name (a valid identifier, not a keyword).

    Accepted:   ``my_service``, ``_internal``, ``Pkg2``
    Rejected:   ``123abc`` (leading digit), ``with-hyphen`` (not an
                identifier), ``class`` (keyword), ``has space``
    """
    if not value:
        return (
            "Python package name is required",
            "Use a valid identifier such as 'my_service'.",
        )
    if keyword.iskeyword(value):
        return (
            f"'{value}' is a Python keyword",
            "Choose a different name; Python keywords cannot be identifiers.",
        )
    # `str.isidentifier()` handles unicode identifiers correctly and is
    # stricter than the ASCII regex (which is only used to phrase the
    # error). Prefer the stdlib: fewer bugs, better future-proofing.
    if value.isidentifier() and not value[0].isdigit():
        return None
    if value[0].isdigit():
        return (
            "Python package name must not start with a digit",
            "Prepend an underscore or a letter, e.g. '_' + the current name.",
        )
    if "-" in value:
        return (
            "Python package name must not contain hyphens",
            "Replace hyphens with underscores, e.g. 'my-service' -> 'my_service'.",
        )
    return (
        "invalid Python package name",
        "Use letters, digits, and underscores; start with a letter or "
        "underscore. Example: 'my_service'.",
    )


# ---------------------------------------------------------------------------
# npm package name — a lowercase, URL-safe identifier. This is a practical
# subset of the official rules; the full spec has reserved names and
# scoping rules that are irrelevant for scaffolding and would only add
# noise to the hint text.
# ---------------------------------------------------------------------------

# Constraints (subset of npm's official rules, documented for reviewers):
#   * length 1..214 (npm's hard cap);
#   * lowercase only;
#   * may contain digits, hyphens, dots, underscores;
#   * must not start with a dot or underscore (npm reserves those);
#   * must not contain spaces or uppercase.
_NPM = re.compile(r"^(?!\.)(?!_)[a-z0-9][a-z0-9._-]{0,213}$")

# Names that npm forbids because they clash with Node.js internals or
# well-known reserved modules. Not exhaustive — new reserved names are
# rare, and a false positive here is annoying but not dangerous.
_NPM_RESERVED = frozenset(
    {
        "node_modules",
        "favicon.ico",
        "http",
        "https",
        "fs",
        "path",
        "child_process",
    }
)


def npm_package(value: str) -> Problem | None:
    """Validate an npm package name (lowercase, URL-safe, length-bounded).

    Accepted:   ``my-package``, ``@scope/name`` (scoped — see note), ``a``
    Rejected:   ``MyPackage`` (uppercase), ``_private`` (leading
                underscore), ``.hidden`` (leading dot), ``has space``,
                ``node_modules`` (reserved)

    Note on scopes: scoped names (``@scope/name``) are legitimately common
    but require additional rules (each part must independently satisfy the
    name grammar). This validator intentionally rejects them for now —
    scaffolding a scoped name means creating a nested directory structure
    we don't yet support. Better to reject with a clear message than to
    accept and fail later during file creation.
    """
    if not value:
        return (
            "npm package name is required",
            "Use a lowercase name like 'my-package'.",
        )
    if value in _NPM_RESERVED:
        return (
            f"'{value}' is a reserved npm name",
            "Choose a different name.",
        )
    if value.startswith("@") or "/" in value:
        return (
            "scoped npm package names are not supported yet",
            "Use an unscoped name like 'my-package'; scoped names "
            "('@scope/name') will be supported in a future release.",
        )
    if _NPM.match(value):
        return None
    if value != value.lower():
        return (
            "npm package name must be lowercase",
            f"Lowercase the name, e.g. '{value.lower()}'.",
        )
    if value[0] in "._":
        return (
            "npm package name must not start with a dot or underscore",
            "Prepend a letter, e.g. 'pkg-' + the current name.",
        )
    if " " in value:
        return (
            "npm package name must not contain spaces",
            "Replace spaces with hyphens, e.g. 'my package' -> 'my-package'.",
        )
    return (
        "invalid npm package name",
        "Use lowercase letters, digits, hyphens, dots, and underscores; "
        "do not start with a dot or underscore; max 214 chars. "
        "Example: 'my-package'.",
    )


# ---------------------------------------------------------------------------
# Registry — the single source of truth for which validators exist.
# ---------------------------------------------------------------------------

# Keyed by the *variable id* used in blueprints. Callers look up by id;
# adding a new named validator is a one-line addition here plus a test.
NAMED_VALIDATORS: dict[str, Callable[[str], Problem | None]] = {
    "project_name": project_name,
    "python_package": python_package,
    "npm_package": npm_package,
}

# Explicit type import kept at the bottom to avoid shadowing `Problem` above
# in the registry annotation. `from __future__ import annotations` already
# makes the annotation a string, but this makes the intent unambiguous.
from collections.abc import Callable  # noqa: E402  (deliberate, see comment above)
