"""Blueprint discovery — find, deduplicate, and order available blueprints.

What this package does
----------------------
Answers exactly one question: *"Which blueprints are available on this
machine, in what order, and where did each one come from?"*

    BlueprintDiscoveryService(local_root=...).discover()
        -> [DiscoveredBlueprint(id, name, version, description,
                                category, source), ...]  # sorted by id

    BlueprintDiscoveryService(local_root=...).load(blueprint_id)
        -> Blueprint  # full typed document for one id

Everything else — reading files, parsing YAML, validating against the
canonical schema, deciding precedence between layers, emitting warnings,
sorting the catalogue — is implementation detail owned by the modules
inside this package. External code (the CLI, tests, future entry points)
interacts only through this façade.

Why the façade matters
----------------------
Two guarantees we want to preserve for as long as possible:

1. **Precedence lives in one place.** Local blueprints override official
   ones by id. That rule is centralised in ``resolver._PRECEDENCE``.
   Freezing the public surface here means a future contributor cannot
   accidentally "fix" precedence by adding a second rule in a source.
   They would have to import an internal module — a visible, reviewable
   act — rather than tweak a public API and hope no one notices.

2. **The CLI never reads the filesystem.** ``forge list``, ``forge
   create``, and any future command that needs blueprints go through
   the service. This keeps the "how do we find blueprints?" question
   answerable in one file and makes replacing the underlying mechanism
   (a remote registry, an XDG cache, a database) a change to this
   package only.

Layering
--------
    discovery/__init__.py    <- this file; public façade
    discovery/service.py     <- orchestration (composes sources + resolver)
    discovery/sources.py     <- candidate producers (bundled, local)
    discovery/resolver.py    <- precedence, dedup, warnings, ordering
    discovery/errors.py      <- MalformedBlueprintError

The dependency arrow always points inward: the service imports sources
and the resolver, the resolver imports sources and models, sources
import only models. Nothing in this package imports the CLI. That is
what makes the whole subsystem extractable into its own distribution
later without any refactoring inside this directory.

What is deliberately NOT exported
---------------------------------
* ``BundledSource``, ``LocalDirectorySource`` — sources are an
  implementation detail. Tests that need to assert on candidate streams
  import from ``forge.discovery.sources`` directly, which makes the
  intent ("I am testing internals") visible in the test file.
* ``resolve``, ``to_metadata``, ``ResolvedCandidate`` — resolver
  internals. Exposing them would tempt callers to bypass the service,
  which in turn would scatter cache-invalidation and I/O decisions
  across the codebase.
* ``default_sources`` — the composition is a service concern; callers
  that want a custom source list inject it via the service constructor.
* ``DEFAULT_LOCAL_ROOT`` constant *value* is exported, but its
  *definition site* stays in ``service.py`` so that changing the default
  location is a one-file change with an obvious diff.

If a future feature genuinely needs one of these, that is a signal to
add a purpose-built method to the service — not to widen this façade.
See ADR-0009 for the rationale behind the source/resolver split.
"""

from __future__ import annotations

from forge.discovery.errors import MalformedBlueprintError
from forge.discovery.service import (
    DEFAULT_LOCAL_ROOT,
    BlueprintDiscoveryService,
)

__all__ = [
    "BlueprintDiscoveryService",
    "DEFAULT_LOCAL_ROOT",
    "MalformedBlueprintError",
]
