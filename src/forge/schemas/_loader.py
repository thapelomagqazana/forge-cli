"""Load the Blueprint schema from package resources.

Why a dedicated loader module?
------------------------------
* ``importlib.resources`` handles wheels, zips, editable installs, and
  frozen binaries uniformly. Naive ``open(__file__ + "...")`` breaks
  under ``zipimport`` and is unreliable when the package is vendored or
  bundled by tools like PyInstaller. This loader works everywhere.
* The schema is parsed exactly once and cached. Validation runs on the
  CLI's hot path, so re-reading and re-parsing the JSON per call would
  be wasted work that grows with every blueprint validated.
* A typed ``schema_version()`` gives callers (and future migrators) a
  stable API to branch on without reaching into raw JSON structure,
  which keeps the schema's internal shape private.

Design invariants
-----------------
* **Read-only.** This module never writes to the filesystem.
* **Deterministic.** Same installed package -> same parsed document.
* **Thread-safe by construction.** ``functools.lru_cache`` is atomic in
  CPython; the only shared state is the immutable parsed dict, which
  callers must treat as frozen (see ``load_schema`` docstring).
* **No third-party imports.** Only the standard library is used here so
  the schema remains loadable even if optional dependencies are absent.
"""

from __future__ import annotations

import json
from functools import lru_cache
from importlib import resources
from typing import Any

# Package-relative identifiers. Exported so diagnostics, tests, and
# future tooling (e.g. schema-packagers) can reference the exact same
# location without duplicating literals.
SCHEMA_PACKAGE: str = "forge.schemas"
SCHEMA_FILENAME: str = "blueprint.schema.json"


@lru_cache(maxsize=1)
def load_schema() -> dict[str, Any]:
    """Return the parsed Blueprint schema document.

    The result is cached for the lifetime of the process: the schema is
    treated as an immutable, installed artifact, not user-editable
    runtime state. Callers must not mutate the returned dict — to
    enforce that contract at the type level would require a deep
    immutable wrapper, which is unnecessary overhead for a value that
    is only ever read by the JSON Schema validator.

    The runtime type check below exists because ``json.loads`` is
    typed by ``typeshed`` as returning ``Any``. Returning ``Any``
    from a function declared to return ``dict[str, Any]`` is a
    silent hole in mypy's coverage: a JSON file whose top level is
    an array would satisfy the type checker at the ``return``
    statement and then crash at the caller with an ``AttributeError``
    when the caller tries ``schema["properties"]``. The
    ``isinstance`` check narrows ``parsed`` to a dict, so the return
    is guaranteed to match the signature — and a malformed schema
    fails here, on the load, with a message that names the problem
    rather than deep in the validator with a stack trace.

    Raises
    ------
    FileNotFoundError
        If the schema is missing from the installed package. This
        indicates a packaging bug (see ``pyproject.toml``'s
        ``package-data`` entry) and should never occur in a correctly
        built wheel.
    json.JSONDecodeError
        If the bundled schema is not valid JSON. Also a packaging bug.
    ValueError
        If the bundled schema is valid JSON but not a JSON object
        (e.g. an array or a scalar). The schema must be an object
        per Draft 2020-12; a non-object top level is a packaging bug
        in the same class as the two above.
    """
    text = resources.files(SCHEMA_PACKAGE).joinpath(SCHEMA_FILENAME).read_text(encoding="utf-8")
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError(
            f"Bundled schema {SCHEMA_FILENAME!r} must be a JSON "
            f"object, got {type(parsed).__name__}. "
            f"This is a packaging bug in the Forge wheel."
        )
    return parsed


def schema_version() -> str:
    """Return the ``schema_version`` declared by the bundled document.

    This is read from the schema itself (the ``const`` of the
    ``schema_version`` property), so the code that enforces the version
    and the version the document declares can never disagree. If the
    bundled document is replaced, this function reflects the change
    without any code edit — the single-source-of-truth principle
    applied to *metadata* as well as shape.

    Raises
    ------
    KeyError
        If the bundled schema does not expose ``schema_version`` at the
        expected path. Treated as a packaging/schema bug — fail loudly
        rather than silently returning a wrong answer.
    """
    return str(load_schema()["properties"]["schema_version"]["const"])


def reset_cache() -> None:
    """Clear the parsed-schema cache.

    Exposed for tests that install alternative fixtures and for
    long-lived processes (e.g. a future language server) that need to
    pick up a schema change without a restart. Not part of the public
    API surface and deliberately not re-exported from
    ``forge.schemas``; production code should never need it.
    """
    load_schema.cache_clear()
