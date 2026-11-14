"""Public façade for the staging engine.

Purpose
-------
`forge.staging` owns the *produce rendered bytes in a hidden tree* phase
of the generation pipeline. It sits between the planner (which decides
what files will be produced) and the commit phase (which moves the
produced files into the destination). Downstream phases — verification,
commit, dry-run reporting — consume the results of this module without
ever reaching into its internals.

Why a façade at all?
--------------------
Every phase in this codebase exposes a small, curated surface:

    * `forge.loading` — `BlueprintLoader`, `Result`, `ValidationError`
    * `forge.resolution` — `VariableResolver`, `ResolvedValues`
    * `forge.conditions` — `ConditionalEvaluator`, `ConditionError`
    * `forge.planning` — `FileMapper`, `PlannedFile`, `MappingError`
    * `forge.safety` — `safe_resolve`, `StagingArea`, `SafetyError`
    * `forge.staging` — this module

The pattern pays for itself in three ways:

1. **Refactor freedom.** The engine's internals (`_renderer_loop`,
   `_validate`, `engine.py`) can be reorganised — split, merged,
   renamed, replaced — without a single change in downstream code, as
   long as the façade's names and signatures are preserved. A caller
   that imports `from forge.staging.engine import StagingEngine`
   forfeits that freedom for no gain; a caller that imports
   `from forge.staging import StagingEngine` gets it for free.

2. **Reviewability.** A reviewer who wants to know "what does staging
   promise to the rest of the codebase?" reads one file. If the public
   names were scattered across submodules, that question would require
   grepping. The façade is the contract, in the concrete sense that the
   *set of names exported here* is the entire public API.

3. **Coupling hygiene.** Nothing exported here should reference types
   from sibling phases in a way that leaks their internals. When a
   public name in this façade does need to mention a type from another
   phase (e.g. `StagingResult.manifest` refers to `PlannedFileKind`
   values as strings), that type is documented in the façade so the
   dependency is visible at the surface, not hidden three modules deep.

What is *not* re-exported, and why
----------------------------------
Deliberate omissions:

    * `render_or_copy` — this is the per-file transition function; it is
      a staging engine implementation detail. External callers should
      not invoke it directly because it takes a *bound writer callback*
      as a parameter, meaning its contract only makes sense in the
      context of a `StagingArea`. Exporting it would invite callers to
      pass their own writers, which is exactly the class of bug the
      safety layer exists to prevent. Tests import it from
      `forge.staging._renderer_loop` by explicit private path, which is
      the right signal: "this is a private seam."

    * `validate_staged_tree` — same reasoning. It is a structural
      self-check of the engine's own output; it is not a public
      verification API. The *blueprint verifier* (a later phase) is a
      different concern with a different contract, and it will not
      reuse this function. Exposing this name would blur the two, and
      a future maintainer might reasonably call it to "check the
      output" and be surprised when it does not check anything about
      the blueprint's promises.

    * `_READY_SUFFIX` and other module-private constants — internal
      naming choices; not part of any contract.

Stability policy
----------------
The names listed in `__all__` are the public API of the staging phase.
Within a minor release:

    * existing names may gain optional keyword-only parameters (additive),
    * `ProgressEvent` may gain fields (additive; downstream consumers
      switch on `stage` and ignore unknown fields),
    * `Finding.code` values may be added (documented in
      `STAGING.md`; downstream consumers that switch on known codes
      should have a default branch).

Within a minor release, the following are *breaking changes* and
require a major version bump per `semver`:

    * removing or renaming a public name,
    * removing or renaming a field on `StagingResult` or
      `ProgressEvent`,
    * removing or renaming a `Finding.code` value that downstream code
      is documented as switching on,
    * changing the shape of `StagingEngine.stage()`'s parameters or
      return type.

There is no deprecation grace period yet (the library is pre-1.0 in its
internal contracts); the policy is "one release, one break, and the ADR
is amended to say why." When the API stabilises, a deprecation window
will be introduced by amending this docstring.

Why `null_sink` is exported
---------------------------
`ProgressSink` is a protocol; the *type* is exported for annotations in
downstream code (e.g. a CLI helper that wraps a sink in a progress bar).
`null_sink` is exported because it is the correct default for any
caller that wants to construct a `StagingEngine` without worrying about
progress — it communicates "I have a sink and it does nothing," which is
more honest than passing `None` and forcing the engine to branch. Every
consumer that omits `progress=` gets this default for free.

Why `StagingFinding` is aliased
-------------------------------
Each phase defines its own `Finding` dataclass (see
`staging/errors.py`'s docstring for why they are not shared). But a
downstream module that imports findings from multiple phases needs to
distinguish them at the call site:

    from forge.staging import StagingFinding
    from forge.planning import Finding as PlanningFinding

Without the alias, downstream code would be forced to write
`from forge.staging.errors import Finding as StagingFinding` — reaching
into a private module to disambiguate a public type. The alias in the
façade makes the type *publicly* addressable under a name that
communicates which phase it belongs to.

The unaliased `Finding` name is deliberately *not* exported, so that a
careless `from forge.staging import Finding` fails loudly with an
`ImportError` rather than silently picking up a phase-specific type
that a reader might assume is universal. This is the codebase's pattern
for phase-local findings: name the phase in the export.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Engine — the sole public entry point for the staging phase.
# ---------------------------------------------------------------------------
from forge.staging.engine import StagingEngine

# ---------------------------------------------------------------------------
# Errors — phase-local shapes, aliased to avoid ambiguity at call sites.
# ---------------------------------------------------------------------------
from forge.staging.errors import Finding as StagingFinding
from forge.staging.errors import RenderingError

# ---------------------------------------------------------------------------
# Progress protocol and events — for downstream observers.
# ---------------------------------------------------------------------------
from forge.staging.progress import (
    ProgressEvent,
    ProgressSink,
    null_sink,
)

# ---------------------------------------------------------------------------
# Result type — the ownership handoff between staging and commit.
# ---------------------------------------------------------------------------
from forge.staging.result import StagingResult

# The explicit `__all__` documents the public surface for tooling (IDEs,
# `from forge.staging import *`, `pydoc`) and for readers of this file.
# It must be kept in sync with the imports above; a missing name here is
# a documented-as-public type that will mysteriously not appear in
# autocomplete, and an extra name here is an import that exists but is
# not promised to remain stable. Both fail the "auditable public API"
# goal this façade exists to serve.
__all__ = [
    # Engine
    "StagingEngine",
    # Result
    "StagingResult",
    # Errors
    "RenderingError",
    "StagingFinding",
    # Progress
    "ProgressEvent",
    "ProgressSink",
    "null_sink",
]
