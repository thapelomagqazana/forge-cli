"""Orchestration entry point for blueprint discovery.

What this module is
-------------------
The composition layer. It builds the source list, streams candidates
from every source into the resolver, and projects the result into the
public catalogue shape. There is no parsing, no precedence, no ordering,
and no filesystem traversal in this file — each of those lives exactly
once in its own module, and the service exists to wire them together.

Why "no policy" is a design goal
--------------------------------
The temptation in an orchestrator is to add "just one" convenience
check — ``if not local_root.exists(): skip local source``, or
``if only one candidate: bypass the resolver``. Each such shortcut
creates a second copy of the rule the resolver already owns, and second
copies are exactly what drift. The service's contract is: **whatever
the sources yield, the resolver adjudicates, the service presents.**
Anything that would break that sentence belongs upstream (in a source)
or downstream (in a caller).

Cache policy
------------
Discovery does **not** cache. Two reasons:

1. Users edit blueprints mid-session (``vi .forge/blueprints/x/blueprint.yaml``
   in one pane, ``forge list`` in another). A cache means they see stale
   data with no obvious way to invalidate it.
2. Discovery is cheap on realistic blueprint counts (tens of
   candidates, each a small YAML file). A cache would be a
   correctness risk paying for a performance problem the users do not
   have.

Callers that need caching — say, a long-running daemon — layer it on
top of the service with their own invalidation story. That way cache
correctness is their concern, and the service stays provably correct
regardless of who calls it.

See ADR-0009 for the source/resolver split this module enforces.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from forge.core.exceptions import DiscoveryError
from forge.discovery.resolver import resolve, to_metadata
from forge.discovery.sources import (
    Candidate,
    SourceLike,
    default_sources,
)
from forge.models import Blueprint
from forge.models.blueprint import DiscoveredBlueprint

# Default location of user-local blueprints, resolved relative to the
# *current working directory* rather than the home directory.
#
# Why CWD and not ``~/.forge/blueprints``:
#   * Blueprints are project-shaped artefacts (they reference templates
#     relative to themselves and generate into a project directory), so
#     per-project overrides are the natural unit of customisation.
#   * A CWD default means two terminals in two projects get different
#     catalogues without any environment variable fiddling — the
#     behaviour users expect from tools like ``pre-commit``,
#     ``pyproject.toml``, and ``.env`` files.
#   * The ``~`` location can be added later as a *third* source without
#     changing this default; freezing it now would make that addendum a
#     breaking change.
DEFAULT_LOCAL_ROOT: Path = Path.cwd() / ".forge" / "blueprints"


@dataclass
class BlueprintDiscoveryService:
    """Discover blueprints from layered sources, in precedence order.

    Constructing with defaults gives the standard behaviour:

        service = BlueprintDiscoveryService()
        service.discover()          # catalogue, sorted by id
        service.load("python.fastapi.api")  # typed Blueprint

    Constructing with an explicit ``local_root`` re-roots the local
    layer — useful for tests, for tools that ship their own blueprint
    directory, and for CLI flags like ``--blueprint-dir``.

    Injecting ``sources`` replaces the default composition entirely.
    This is the seam for future extensions (a remote registry, an XDG
    cache, a project-vendored layer). The list is stored and used as-is;
    the service does not reorder, filter, or validate it — precedence is
    the resolver's concern, not the service's.

    Why a ``dataclass`` and not a ``NamedTuple`` or plain class?
    ------------------------------------------------------------
    * ``dataclass`` gives equality, ``repr``, and constructor wiring for
      free — three things every test that asserts on service behaviour
      relies on.
    * Mutable (not frozen) because the constructor may be cheap but
      tests routinely want to set ``service.local_root = tmp_path`` on a
      shared fixture. Freezing would force a rebuild per test for no
      behavioural benefit; the service has no internal state that could
      be corrupted by attribute assignment.
    * Not a ``NamedTuple`` because ``sources`` defaults to ``None``
      (meaning "use the default composition"), and ``NamedTuple`` would
      make the "was it explicitly provided?" distinction awkward.
    """

    local_root: Path = field(default_factory=lambda: DEFAULT_LOCAL_ROOT)
    sources: list[SourceLike] | None = None

    def _resolved_sources(self) -> list[SourceLike]:
        """Return the source list the service will actually use.

        Kept private so callers cannot read the "effective sources" and
        then act on them — that would reopen the door to precedence
        logic leaking out of the resolver. Tests that need to inspect
        the composition can pass an explicit ``sources`` list and read
        it back from the attribute; that keeps the inspection capability
        but makes it visible.
        """
        if self.sources is not None:
            return self.sources
        return default_sources(self.local_root)

    def _iter_candidates(self) -> Iterator[Candidate]:
        """Stream every candidate from every source.

        Lazy on purpose: with a remote source in the future, streaming
        means a slow source does not delay processing of already-yielded
        candidates from faster sources. The resolver consumes the
        iterator one candidate at a time, so memory stays O(catalogue
        size), not O(total-bytes-on-disk).
        """
        for source in self._resolved_sources():
            yield from source.candidates()

    def discover(self) -> list[DiscoveredBlueprint]:
        """Return the catalogue: deduplicated, local-wins, sorted by id.

        Returns immutable ``DiscoveredBlueprint`` views — never full
        ``Blueprint`` documents. Callers that need the full document
        call ``load(id)``. Separating the two keeps ``forge list``
        cheap (no template parsing, no verification-command compilation)
        and keeps the catalogue shape stable even if the underlying
        document model evolves.
        """
        return to_metadata(resolve(self._iter_candidates()))

    def load(self, blueprint_id: str) -> Blueprint:
        """Return the full typed ``Blueprint`` for the given id.

        Re-resolves from the same sources rather than caching, for the
        reasons documented in the module docstring. Re-resolution is
        cheap; correctness that survives user edits is not.

        When the id is not found, the raised ``DiscoveryError`` names both
        the catalogue's *available* ids and the command that lists the
        full catalogue. The two pieces of information serve different
        readers:

        * **The available ids** are for a user who made a small typo —
        seeing ``python.fastapi.api`` next to their typed
        ``python.fastapi.ap`` is a one-keystroke fix.
        * **The recovery command** (``forge list``) is for a user who
        expected a blueprint that is not installed — it names the next
        step that will show them *everything*, including blueprints
        added by a local override they forgot to check.

        Naming only one of these leaves one recovery path invisible.
        Naming both costs one extra sentence and eliminates the "which
        command do I run?" question entirely.

        The available list is sorted and rendered inline so it reads as
        data, not prose — the same format the registry uses for the same
        reason.

        When the catalogue is empty (a distinct failure from "not
        found"), the message names the install-level recovery instead,
        because a user with nothing installed is not helped by a
        ``forge list`` command that would print nothing.
        """
        # Resolve the whole catalogue once, then filter. Filtering is O(n)
        # either way, and doing it this way means a duplicate-id collision
        # that produces a warning will fire even when the caller only asked
        # for a single blueprint. That warning is valuable: users should
        # learn about the duplicate the first time they touch discovery,
        # not the first time they list.
        resolved = resolve(self._iter_candidates())
        for item in resolved:
            if item.blueprint.id == blueprint_id:
                return item.blueprint

        # Build the "available" hint from the *resolved* catalogue — this
        # is the same set ``discover()`` would return, sorted the same way,
        # so a user who runs ``forge list`` after reading the error sees
        # exactly the ids listed here. That equivalence is the point of
        # naming them: the error becomes a preview of the recovery command.
        available = sorted(item.blueprint.id for item in resolved)
        if available:
            listing = ", ".join(available)
            # Both pieces of information, one sentence each. The first is
            # for the typo case (copy-paste); the second is for the missing
            # case (blueprint you expected is not installed). Collapsing
            # them into one clause would lose one of the two recoveries.
            hint = (
                f"Available blueprints: {listing}. " f"Run `forge list` to see the full catalogue."
            )
        else:
            # An empty catalogue is a distinct failure ("nothing installed")
            # from "not found" ("installed, but not the one you asked for").
            # Collapsing them would send a user with a broken install looking
            # for a typo, and a user with a typo looking at their install.
            hint = (
                "No blueprints are installed. Add a blueprint under "
                ".forge/blueprints/, or check your installation with "
                "`forge doctor`."
            )

        raise DiscoveryError(f"Blueprint '{blueprint_id}' not found. {hint}")
