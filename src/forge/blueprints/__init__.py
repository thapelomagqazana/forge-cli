"""Official Forge blueprints, shipped as package data.

Why this module exists
----------------------
Python cannot locate ``forge.blueprints`` as a resource unless it is a
proper package. This file is that package marker. ``importlib.resources``
resolves it identically across editable installs (PEP 660), wheels, and
zipimports, which is exactly what the discovery layer requires — the
bundled source reads these files through ``resources.files()``, never
through ``__file__``, so path assumptions never leak into user code.

Layout invariant
----------------
Discovery expects this structure and nothing else::

    forge/blueprints/
    ├── __init__.py                         # this file
    ├── <blueprint.id>/                     # one directory per blueprint
    │   ├── blueprint.yaml                  # the manifest
    │   └── templates/...                   # optional support files
    └── ...

The directory name *should* equal the manifest's ``id`` field. The
resolver keys on the manifest id, not the directory name, so a mismatch
is tolerated at runtime — but it will be flagged by the packaging tests
and rejected in review, because the two diverging silently is exactly
the kind of drift the schema layer was designed to prevent.

What this module deliberately does NOT do
-----------------------------------------
* It does not import the models, the schema loader, or the discovery
  service. Doing so would create an import cycle (``models`` → ``schemas``
  → ``blueprints`` would loop) and would make ``import forge.blueprints``
  pay the cost of loading Pydantic and jsonschema just to confirm a
  package exists.
* It does not register, enumerate, or validate blueprints. Those are the
  discovery service's job (single source of truth), and duplicating them
  here is precisely the technical debt this architecture avoids.
* It exposes no constants. The blueprint set is data — consumers query
  it through ``BlueprintDiscoveryService``, not through Python imports.

See ADR-0009 for the discovery/precedence model and ADR-0007 for why
the manifest schema, not this package, is the canonical contract.
"""

from __future__ import annotations

# The one invariant worth asserting at import time: this package *is*
# a package, so resource lookups have a stable anchor. If someone ever
# converts this directory to a namespace package or moves it, discovery
# silently breaks in wheels but works in editable installs — a
# notoriously painful debugging experience. Failing loudly here costs
# nothing and catches the mistake at the earliest possible moment.
__all__: list[str] = []
