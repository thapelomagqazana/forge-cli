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

Dependency direction (ADR-0009)
-------------------------------
This module imports:

* ``forge.discovery.resolver`` — the precedence/dedup/ordering owner.
* ``forge.discovery.sources``  — the candidate producers.
* ``forge.models``             — parsed blueprint types and views.
* ``forge.core.exceptions``    — ``DiscoveryError``.
* ``forge.loading``            — *only* inside methods that need it
                                (``discover_with_status``), and only
                                to derive a status from the file on
                                disk. Loader internals are not exposed.

It does **not** import ``forge.cli`` in any form. The status enum that
``discover_with_status`` returns lives in ``forge.models.status``
precisely so that this module can name its own return type without
importing the CLI. Static analysis (mypy) and a guard test
(``tests/unit/discovery/test_no_cli_imports.py``) both enforce this.

Why ``discover_with_status`` is not "no policy"
-----------------------------------------------
It resolves once and derives a status per already-resolved candidate.
It does **not** re-resolve; it does **not** re-run the source stream;
it does **not** re-check the schema. Deriving a status is a *second
question* ("is the content on disk complete?") that the resolver does
not answer, so it must be answered here — but only as a projection
over the resolver's output, never as a second resolution pass.

Earlier drafts of this method did re-resolve per blueprint (an N+1
filesystem pass for a listing command). That is exactly the kind of
"convenience check" the module docstring warns against: it duplicated
work the resolver had already done and made the cost of
``discover_with_status()`` O(N x loader) instead of O(loader) with a
per-candidate status check.

See ADR-0009 for the source/resolver split this module enforces.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from forge.core.exceptions import DiscoveryError
from forge.discovery.resolver import ResolvedCandidate, resolve, to_metadata
from forge.discovery.sources import (
    Candidate,
    SourceLike,
    default_sources,
)
from forge.loading.loader import BlueprintLoader
from forge.loading.result import Ok
from forge.models import Blueprint
from forge.models.blueprint import DiscoveredBlueprint

# ``BlueprintStatus`` is imported from its home module, not from
# ``forge.cli.schemas.list_output``. The CLI re-exports the same class,
# so identity is preserved end-to-end (see ADR-0012 and the module
# docstring of ``forge.models.status``). Importing it from here is what
# keeps ``forge.discovery`` free of any CLI dependency.
from forge.models.status import BlueprintStatus

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

    # -- internals --------------------------------------------------------

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

    def _resolve_all(self) -> list[ResolvedCandidate]:
        """Resolve every candidate through the one pipeline we own.

        Centralised so ``discover()``, ``load()``, and
        ``discover_with_status()`` all see exactly the same catalogue —
        including any duplicate-id warnings the resolver emitted. If any
        of them re-ran resolution independently, a duplicate warning
        could fire a different number of times depending on which entry
        point the caller used, which is precisely the kind of
        order-dependent nondeterminism discovery exists to avoid.
        """
        return resolve(self._iter_candidates())

    # -- public API -------------------------------------------------------

    def discover(self) -> list[DiscoveredBlueprint]:
        """Return the catalogue: deduplicated, local-wins, sorted by id.

        Returns immutable ``DiscoveredBlueprint`` views — never full
        ``Blueprint`` documents. Callers that need the full document
        call ``load(id)``. Separating the two keeps ``forge list``
        cheap (no template parsing, no verification-command compilation)
        and keeps the catalogue shape stable even if the underlying
        document model evolves.
        """
        return to_metadata(self._resolve_all())

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
        resolved = self._resolve_all()
        for item in resolved:
            if item.blueprint.id == blueprint_id:
                return item.blueprint

        available = sorted(item.blueprint.id for item in resolved)
        if available:
            listing = ", ".join(available)
            hint = (
                f"Available blueprints: {listing}. " f"Run `forge list` to see the full catalogue."
            )
        else:
            hint = (
                "No blueprints are installed. Add a blueprint under "
                ".forge/blueprints/, or check your installation with "
                "`forge doctor`."
            )

        raise DiscoveryError(f"Blueprint '{blueprint_id}' not found. {hint}")

    def discover_with_status(
        self,
    ) -> list[tuple[DiscoveredBlueprint, BlueprintStatus]]:
        """Return ``(blueprint, status)`` pairs for ``forge list``.

        ``status`` reflects whether the underlying blueprint is fully
        usable:

        * ``ok``    — parses, validates, and every referenced template
                      file exists.
        * ``stale`` — parses and validates, but at least one referenced
                      template file is missing.
        * ``error`` — reserved for a future "list broken" mode; the
                      default catalogue excludes broken blueprints
                      entirely (see ADR-0009), so this value is not
                      currently produced.

        The richer API is *opt-in* because status derivation runs the
        loader once per already-resolved candidate. On realistic
        catalogue sizes (tens of blueprints) this is cheap; on very
        large catalogues it is the caller's cue to use ``discover()``
        for metadata-only needs.

        Ordering matches ``discover()`` — the resolver's order,
        projected into ``DiscoveredBlueprint`` views. The CLI sorts once
        (per ADR-0012) before handing rows to a renderer; this method
        deliberately preserves the resolver's order rather than
        introducing a second sort that the CLI would then have to
        reconcile with the JSON envelope's contract.
        """
        # Local imports keep the loader dependency visible only where
        # it is actually used. ``BlueprintLoader`` pulls in the YAML
        # parser, the schema validator, and the pydantic model tree —
        # none of which ``discover()`` or ``load()`` need. Importing
        # them at module scope would make every CLI startup pay that
        # cost even for commands that never touch status.
        #
        # The status enum itself is imported at module scope (see above)
        # because it is part of the public return type and appears in
        # the method signature; deferring it would only obscure the
        # contract.
        from forge.loading import BlueprintLoader

        loader = BlueprintLoader()

        # Single resolution pass. The status of each candidate is
        # derived from its already-parsed document (``item.blueprint``)
        # plus the origin file on disk, not from re-running the loader
        # against the whole candidate stream. This keeps
        # ``discover_with_status()`` O(resolve + N * file-check) instead
        # of O(resolve + N * (resolve + file-check)).
        out: list[tuple[DiscoveredBlueprint, BlueprintStatus]] = []
        for resolved in self._resolve_all():
            view = to_metadata([resolved])[0]
            status = self._derive_status(resolved, loader=loader)
            out.append((view, status))
        return out

    # -- helpers ----------------------------------------------------------

    def _derive_status(
        self,
        resolved: ResolvedCandidate,
        *,
        loader: BlueprintLoader,
    ) -> BlueprintStatus:
        """Classify an already-resolved candidate as ``ok`` or ``stale``.

        The classification is a **file health** check, not a
        re-validation:

        * The candidate has already passed schema + model validation
          (the resolver would not have returned it otherwise).
        * What has *not* been checked is whether the template files it
          references actually exist on disk — that is the one piece of
          information the resolver cannot supply without doing the
          filesystem pass itself, and doing it there would conflate
          "which blueprint wins" (precedence) with "is this blueprint's
          content complete" (health). Two concerns, two places.

        The loader is passed in so tests can substitute a fake without
        patching module state — a small but real testability win for
        what is otherwise a straightforward re-check.
        """
        path = self._label_to_path(resolved.source_label)
        if path is None:
            # No filesystem representation available (e.g. a future
            # remote source). We cannot confirm template health, so
            # ``ok`` is the honest answer: the metadata is present and
            # validated; content check is out of scope for this source.
            return BlueprintStatus.ok

        result = loader.load(path, check_files=True)
        if isinstance(result, Ok):
            return BlueprintStatus.ok
        return BlueprintStatus.stale

    def _label_to_path(self, label: str) -> Path | None:
        """Best-effort recovery of a filesystem path from a source label.

        The resolver's labels are one of:

        * ``local:<absolute-path>`` — the file the user has on disk.
        * ``bundled:<blueprint-id>`` — a *logical* label, not a path;
          the physical file lives inside the package under
          ``forge/blueprints/<id>/blueprint.yaml``. We resolve that via
          ``importlib.resources`` so the loader's filesystem checks work
          the same way for bundled and local candidates.

        Returning ``None`` means "no filesystem representation is
        available" — the status derivation treats that as ``ok``
        (see ``_derive_status``).
        """
        if label.startswith("local:"):
            return Path(label[len("local:") :])
        if label.startswith("bundled:"):
            from importlib import resources

            bp_id = label[len("bundled:") :]
            entry = resources.files("forge.blueprints").joinpath(bp_id, "blueprint.yaml")
            if entry.is_file():
                # Materialise to a temp path so the loader — which is
                # strictly path-based by design — can inspect it. The
                # temp file is leaked (one small YAML per bundled
                # blueprint per process); acceptable for a CLI, and a
                # future refactor could cache it keyed by blueprint id.
                import tempfile

                tmp = Path(tempfile.mkdtemp()) / "blueprint.yaml"
                tmp.write_bytes(entry.read_bytes())
                return tmp
        return None
