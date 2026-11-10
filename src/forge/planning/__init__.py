"""Public façade for the planning phase.

This module is the *only* supported import surface for planning. Every
consumer — ``forge create``, the staging/commit pipeline, future
``forge plan`` / ``--dry-run`` renderers, tests — imports from here and
never from the private modules (``mapper``, ``destinations``,
``collisions``, ``errors``, ``planned_file``). That discipline gives us
three concrete benefits:

    1. **Refactor safety.** We can reorganise the internals (split
       ``destinations`` further, replace the collision algorithm, add a
       conditional-files evaluator that feeds the same planner) without
       touching call sites. The façade is the contract.

    2. **Contract clarity.** The public surface is *exactly* the data
       types the pipeline hands between phases — ``PlannedFile``,
       ``PlannedFileKind``, ``MappingError``, ``Finding`` — plus the one
       orchestrator, ``FileMapper``. Nothing else is exported. Callers
       cannot accidentally depend on, say, the internal signature of
       ``destinations._safety_check``.

    3. **Import-cost control.** Importing ``forge.planning`` should not
       import the renderer (and therefore not jinja2) unless the caller
       actually constructs a ``FileMapper`` or calls ``map()``. The
       renderer import is deferred to first use — see the lazy pattern
       below.

Public API
----------
    FileMapper          — the planning orchestrator
                          ``map(blueprint, variables)``
                              -> ``Result[list[PlannedFile], MappingError]``

    PlannedFile         — one row in the plan (immutable DTO)

    PlannedFileKind     — ``RENDER`` or ``COPY``

    MappingError        — aggregated planning failure
                          (all findings collected in one report)

    Finding             — one planning issue
                          (path, message, hint, code, sources)

Not public
----------
    mapper, destinations, collisions, errors, planned_file
        Internal. Import from these modules is rejected in code review
        unless you are working *inside* ``forge/planning/``.

    In particular, ``destinations.resolve`` and ``collisions.find_collisions``
    are **not** re-exported. They are the two behaviours the planner
    composes; a caller who reaches into them directly is bypassing the
    planner's aggregation and ordering guarantees. If you need them,
    you are almost certainly implementing a second planner — which we
    want to happen *inside* this package, not outside it.

    ``destinations.is_safe_relative`` is also deliberately not
    re-exported through this façade — the loader (its current external
    caller) imports it from ``forge.planning.destinations`` directly,
    which is the correct seam: it is a *shared* rule, not a planning
    API, and routing it through the façade would couple the loader to
    a module whose other public names it does not use.

The pure-function contract
--------------------------
Planning is a pure, total function of ``(blueprint, variables)``. There
is **no I/O** in the planner: it does not read template sources (the
loader already verified they exist), does not render file bodies (that
is the writer's job), and does not touch the filesystem at all. That
purity is what makes ADR-0015's determinism guarantee possible, and it
is what lets the façade expose a single, small API — if the planner
needed more context, the façade would have to expose more context, and
the surface would grow.

The one impurity (renderer import)
----------------------------------
The planner *does* use the sandboxed renderer, but only to substitute
variables into **destination path templates** — never to render file
bodies. The renderer is a heavy import (it pulls in jinja2, the
environment, the sandbox, the filters module), and a caller who only
wants to inspect a ``PlannedFile`` (in a test, in an IDE, in a future
diagnostic command) should not pay for it. The façade therefore imports
``FileMapper`` lazily via PEP 562 — the same pattern used in
``forge.rendering.__init__`` for the same reason (import cost), and the
same pattern used there for a different reason (security boundary).
Reusing the pattern across the two façades means contributors who learn
it once recognise it twice.

Why ``Result`` is not re-exported
---------------------------------
Callers need ``Ok`` and ``Err`` to handle ``map()``'s return value, and
they must be the *same* ``Ok``/``Err`` the codebase already uses
everywhere (ADR-0010). Re-exporting them here would create a second
import path to the same two constructors, which is exactly the
duplication the façade system exists to prevent. Callers import them
from ``forge.loading.result`` — a stable, one-line import — or, if they
already handle results from ``forge.rendering`` or ``forge.loading``,
they already have them.

Shadowing note (why this file uses ``sys.modules``)
---------------------------------------------------
The package has no submodule named ``globals``, so the shadow that
forced ``forge.rendering.__init__`` to use
``sys.modules[__name__].__dict__`` does not apply here. We use the same
form anyway, for two reasons:

    1. **Consistency.** A contributor who learns the pattern once (in
       either façade) should recognise it in the other without having
       to remember which package happened to have a shadowing submodule.
    2. **Future-proofing.** If a future task adds a
       ``forge/planning/globals.py`` (or any other module whose name
       shadows a builtin that the lazy loader needs), the pattern in
       this file will already be correct rather than needing a small
       surgical fix under time pressure. The cost — one extra attribute
       lookup per lazy access — is unmeasurable, and the benefit is a
       codebase where "how façades work" has exactly one answer.

The alternative — plain ``globals()`` — would work today and break
silently the day a shadowing submodule appeared. We are optimising for
the failure mode we have already hit once, not the one we happen not to
have today.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Any

# Eager imports: the DTOs and error types are cheap (pure dataclasses
# and a Pydantic model) and needed by *every* consumer — a caller that
# touches a plan at all will touch `PlannedFile`, and a caller that
# handles errors will touch `MappingError`. Deferring these would buy
# nothing and complicate the module for no reason.
from forge.planning.errors import Finding, MappingError
from forge.planning.planned_file import PlannedFile, PlannedFileKind

if TYPE_CHECKING:
    # Type-only import of `Result` for the docstring examples and for
    # future annotation use. `from __future__ import annotations` means
    # no runtime cost, and no name is needed at runtime because the
    # façade does not alias or re-export `Result`.
    from forge.loading.result import Result  # noqa: F401


# `FileMapper` is imported lazily. It pulls in the renderer (and
# therefore jinja2), which is a heavy import for a caller who only
# needs to inspect a `PlannedFile` or handle a `MappingError`. The lazy
# attribute keeps `import forge.planning` cheap and defers the renderer
# dependency to the moment a caller actually plans something.
_LAZY_EXPORTS: dict[str, tuple[str, str]] = {
    "FileMapper": ("forge.planning.mapper", "FileMapper"),
}


def __getattr__(name: str) -> Any:
    """Lazy-load ``FileMapper`` on first attribute access.

    Called by Python when a name is not found in the module's globals.
    Defers the heavy ``mapper`` import (renderer, jinja2, sandbox) until
    a caller actually needs the orchestrator, keeping ``import
    forge.planning`` cheap for consumers that only handle ``PlannedFile``
    or ``MappingError``.

    The returned object is cached in the module namespace after first
    access so subsequent lookups are ordinary attribute reads — the
    first access is the only one that pays the import cost.

    Why ``sys.modules[__name__].__dict__`` instead of ``globals()``:
    see the module docstring. In short: today there is no shadowing
    submodule, but the form is defensive against one appearing, and it
    keeps the two façades (``forge.rendering``, ``forge.planning``)
    consistent so contributors have one pattern to learn.
    """
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    module_path, attr = target
    import importlib

    module = importlib.import_module(module_path)
    value = getattr(module, attr)

    # Cache for future lookups. Safe here because module import is
    # single-threaded and the value is an immutable class object.
    sys.modules[__name__].__dict__[name] = value
    return value


def __dir__() -> list[str]:
    """Include lazy exports in ``dir(forge.planning)`` for IDE support.

    Without this, ``dir()`` would not list ``FileMapper`` because it is
    not in the module's globals until first access. IDEs and REPLs use
    ``__dir__`` to build completion lists, so listing it explicitly
    keeps discovery working without paying the import cost upfront.
    Same pattern as ``forge.rendering``, for the same reason.
    """
    return sorted(set(sys.modules[__name__].__dict__) | set(_LAZY_EXPORTS))


__all__ = [
    "FileMapper",
    "Finding",
    "MappingError",
    "PlannedFile",
    "PlannedFileKind",
]
