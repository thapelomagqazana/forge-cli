"""Public façade for the dry-run plan builder.

Why a façade?
-------------
Downstream code (``forge.cli.commands.create``, future ``forge plan``
subcommand, future API endpoints, integration tests) imports from
``forge.dryrun``, never from ``forge.dryrun.service`` or
``forge.dryrun.plan`` directly. That gives us three properties:

    1. **Refactor freedom.** We can rename ``service.py`` to
       ``builder.py``, split ``plan.py`` into ``plan/`` with per-DTO
       modules, or swap the internal composition — as long as
       ``__init__`` re-exports the same names, no caller changes.

    2. **One obvious import.** A reviewer scanning a new command sees
       ``from forge.dryrun import DryRunService, GenerationPlan`` and
       immediately understands the dependency is bounded. If they see
       ``from forge.dryrun.service import _build_skips``, they know
       something is wrong.

    3. **Contract clarity.** The names in ``__all__`` are the public
       API. Everything else is internal by definition, so adding,
       removing, or reshaping internals is not a breaking change.

What's exported, and why
------------------------
The façade deliberately *re-exports the errors alongside the happy
path*. A consumer that can build a ``GenerationPlan`` but cannot import
``DryRunError`` would have to import from ``forge.dryrun.errors``
directly, defeating the purpose of the façade. Type annotations like
``-> GenerationPlan | DryRunError`` should work with a single import.

The renderers are also re-exported. They are stateless pure functions
and part of the module's public surface — CLI code should not need to
know that ``render_human`` lives in a sub-module. If we ever consolidate
renderers into a shared ``cli.renderers`` package (following ADR-0012),
we can re-point these re-exports without touching commands.

The snapshot helpers (``fingerprint``, ``diff``, ``assert_unchanged``)
are **not** re-exported. They exist for tests and one-off audits, not
for the CLI. Keeping them out of the façade sends the right signal:
production code depends on ``DryRunService`` and the DTOs; test code
depends on the fingerprint. The boundary between the two is visible in
a single file.
"""

from __future__ import annotations

from forge.dryrun.errors import DryRunError, Finding
from forge.dryrun.plan import (
    CollisionDTO,
    GenerationPlan,
    PlannedFileDTO,
    PlannedFileKind,
    SkippedFileDTO,
)
from forge.dryrun.renderers import render_human, render_json
from forge.dryrun.service import DryRunService

__all__ = [
    # Service — the entry point
    "DryRunService",
    # The DTO — the contract
    "GenerationPlan",
    "PlannedFileDTO",
    "PlannedFileKind",
    "SkippedFileDTO",
    "CollisionDTO",
    # Errors — part of the return type union
    "DryRunError",
    "Finding",
    # Renderers — pure functions, part of the CLI surface
    "render_human",
    "render_json",
]
