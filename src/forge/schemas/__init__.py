"""Bundled JSON Schemas shipped with the Forge CLI.

This package is intentionally *tiny*. Its only job is to expose the
canonical schema documents to the rest of the application through a
stable, import-safe API. Anything beyond loading — validation,
migration, semantic checks — belongs to the verification layer, not
here.

Public API
----------
    load_schema()      -> dict[str, Any]   parsed schema document (cached)
    schema_version()   -> str              the version the document declares
    SCHEMA_PACKAGE     -> str              package name (for resource lookups)
    SCHEMA_FILENAME    -> str              bundled filename (for diagnostics)

Design notes
------------
* **No side effects on import.** Importing this module must never touch
  the filesystem beyond what ``importlib.resources`` already does
  lazily. CLI startup time is a feature; keep it that way.
* **Re-exports, not re-implementations.** Every public symbol is
  defined exactly once in ``_loader`` and re-exported here so callers
  can ``from forge.schemas import load_schema`` without reaching into a
  private module — but there is only one implementation to maintain.
* **Stable surface for future schema versions.** When schema v2 lands,
  the additions here will be additive (``load_schema(version="2")``)
  rather than a rename, so existing call sites keep working unchanged.
"""

from __future__ import annotations

from forge.schemas._loader import (
    SCHEMA_FILENAME,
    SCHEMA_PACKAGE,
    load_schema,
    schema_version,
)

__all__ = [
    "SCHEMA_FILENAME",
    "SCHEMA_PACKAGE",
    "load_schema",
    "schema_version",
]
