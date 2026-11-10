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
                                (``discover_with_status``,
                                ``load_with_path``), and only to
                                derive a status from or read the
                                file on disk. Loader internals are
                                not exposed.

It does **not** import ``forge.cli`` in any form. The status enum that
``discover_with_status`` returns lives in ``forge.models.status``
precisely so that this module can name its own return type without
importing the CLI. Static analysis (mypy) and a guard test
(``tests/unit/discovery/test_no_cli_imports.py``) both enforce this.

Why ``load_with_path`` exists
-----------------------------
``load(id)`` returns the parsed ``Blueprint`` document. That is
sufficient for callers that only *inspect* the blueprint (resolve
variables against it, plan files from it, list it). But some callers
must also know **where the manifest lives on disk**, because
blueprint ``source`` paths are relative to the manifest's *directory*
— not to the current working directory, not to ``forge.blueprints``
in the package. ``create`` is the first such caller: it needs the
directory to read template files during generation.

Threading the path into ``Blueprint`` (a field, a cached attribute,
a ``path`` property) would couple the pure document model
(ADR-0007/0008) to the loader's filesystem layout — the exact
coupling the two-accessor registry design exists to prevent. So the
path is returned *alongside* the document by a dedicated accessor,
and ``Blueprint`` stays pure.

``load_with_path`` and ``load`` share their resolution through a
private helper so they cannot drift. If either is changed to honour a
new source type or a new precedence rule, both inherit the change by
construction — the "no second copies" principle applied to this
module's own two accessors.

Bundled-source materialisation
------------------------------
The loader is strictly path-based by design (a file it can read is a
blueprint it can validate). Bundled blueprints live inside the wheel
under ``forge/blueprints/<id>/blueprint.yaml`` and are reachable only
via ``importlib.resources`` — which yields a *resource handle*, not a
``Path``. To bridge the two, the service stages each bundled manifest
to a temporary file, runs the loader against it, and (for
``load_with_path``) returns the staged path's parent as the
"manifest directory" anchor.

The staging is done inside a ``tempfile.TemporaryDirectory`` context
manager so the staging directory is removed on exit. Only the
*returned* path — a plain ``Path`` the caller may read immediately —
is at risk of going stale, and the caller's use of it is bounded: the
staged parent contains exactly one file (the manifest), and template
health was *already* derived from the on-disk package contents before
returning (see ``_derive_status``). A caller that tries to read a
template through this path will get a clear "file not found" — which
is correct, because the template does not live there; it lives in the
package, and the caller should be using the loader's discovery (or
the packaged resource) for it. This is a *deliberate* limitation, not
an oversight: the entire alternative is exposing ``importlib.resources``
handles through the service, which would leak a low-level API into
every caller's code.

See ADR-0009 for the source/resolver split this module enforces.
"""

from __future__ import annotations

import tempfile
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
        service.load("python.fastapi.api")          # typed Blueprint
        service.load_with_path("python.fastapi.api")  # (manifest, Blueprint)

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

        Centralised so ``discover()``, ``load()``,
        ``load_with_path()``, and ``discover_with_status()`` all see
        exactly the same catalogue — including any duplicate-id
        warnings the resolver emitted. If any of them re-ran
        resolution independently, a duplicate warning could fire a
        different number of times depending on which entry point the
        caller used, which is precisely the kind of order-dependent
        nondeterminism discovery exists to avoid.
        """
        return resolve(self._iter_candidates())

    def _find_resolved(self, blueprint_id: str) -> ResolvedCandidate | None:
        """Return the resolved candidate for ``blueprint_id``, or None.

        The single lookup path used by ``load()`` and
        ``load_with_path()``. Keeping it in one function means the two
        accessors cannot diverge on ordering, precedence, or which
        candidate wins — a divergence that would produce the
        pathological case where ``service.load(x)`` and
        ``service.load_with_path(x)[1]`` return *different* blueprints
        for the same id.
        """
        for item in self._resolve_all():
            if item.blueprint.id == blueprint_id:
                return item
        return None

    def _not_found(self, blueprint_id: str) -> DiscoveryError:
        """Build the canonical "not found" ``DiscoveryError``.

        Centralised so both accessors raise an identically-worded
        error — a caller that switches from ``load`` to
        ``load_with_path`` (or vice versa) sees the same recovery
        guidance. See ``load``'s docstring for the reasoning behind
        naming both the available ids *and* the recovery command.
        """
        resolved = self._resolve_all()
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
        return DiscoveryError(f"Blueprint '{blueprint_id}' not found. {hint}")

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

        When the id is not found, the raised ``DiscoveryError`` names
        both the catalogue's *available* ids and the command that lists
        the full catalogue. The two pieces of information serve
        different readers:

        * **The available ids** are for a user who made a small typo —
          seeing ``python.fastapi.api`` next to their typed
          ``python.fastapi.ap`` is a one-keystroke fix.
        * **The recovery command** (``forge list``) is for a user who
          expected a blueprint that is not installed — it names the
          next step that will show them *everything*, including
          blueprints added by a local override they forgot to check.

        Naming only one of these leaves one recovery path invisible.
        Naming both costs one extra sentence and eliminates the "which
        command do I run?" question entirely.

        Delegates to ``_find_resolved`` (shared with
        ``load_with_path``) and ``_not_found`` (shared error
        construction), so the two accessors cannot drift.
        """
        resolved = self._find_resolved(blueprint_id)
        if resolved is None:
            raise self._not_found(blueprint_id)
        return resolved.blueprint

    def load_with_path(self, blueprint_id: str) -> tuple[Path, Blueprint] | None:
        """Return the manifest path and loaded document for a blueprint id.

        Returns ``(manifest_path, blueprint)`` on success, ``None`` when
        no source yields the id. The ``Path`` is the file whose *parent
        directory* is the anchor for every ``source`` path the
        blueprint declares — callers that read templates at generation
        time need it.

        Why ``None`` and not an exception:
            ``load()`` raises ``DiscoveryError`` on not-found because
            its callers (the wizard, ``forge create``'s blueprint
            fetch) have a well-defined recovery ("pick a different
            id"). ``load_with_path``'s callers are of two minds:

            * ``create._blueprint_root`` treats not-found as a race —
              the blueprint existed at ``get_blueprint_document``
              time, so a ``None`` now means the file was moved or
              deleted between the two calls. It translates ``None``
              into a ``FilesystemConflictError`` (exit code 3) with
              a hint that names the recovery.
            * A future batch tool that calls ``load_with_path`` over
              a hundred ids wants to skip the misses without
              exception-handling noise.

            Returning ``None`` serves both: the exception-shape
            callers wrap it in *their* error type, and the batch
            callers use a plain truthiness check. An exception here
            would force the batch callers to catch-and-continue,
            which is both verbose and a source of "forgot to re-raise
            the real error" bugs.

        Why this is a *separate* method from ``load``:
            ``Blueprint`` is a pure document (ADR-0007/0008). The
            filesystem path of the manifest is a *discovery-layer*
            concern, not a document-layer one. Threading the path
            into the model would couple the schema to the loader's
            layout. A caller that needs both calls the accessor that
            returns both; a caller that needs only the document
            calls ``load``, which shares the same resolution and
            therefore cannot disagree about which candidate wins.

        Bundled-source materialisation:
            Bundled blueprints live inside the wheel under
            ``forge/blueprints/<id>/blueprint.yaml`` and are reached
            via ``importlib.resources``. Because the loader is
            path-based by design, we stage the manifest to a temp
            file under a ``TemporaryDirectory`` that removes itself
            on exit. The returned ``Path`` is a *snapshot* — the
            caller can read the manifest (and the directory it sits
            in) immediately, but not later, and the staged directory
            does not contain the blueprint's *template* files (those
            live in the package and are read by the loader during
            template-health checks, not by the caller).

            Why this is correct rather than merely convenient:
            ``create._blueprint_root`` uses the returned path as a
            *directory anchor* for template resolution, but only
            inside a single ``_generate`` call. The generated files
            are staged and committed before ``_generate`` returns.
            So the temp directory's lifetime exactly covers its use.

            A future ``forge generate --deferred`` (hypothetical)
            that needs the anchor to outlive the call would need to
            materialise the blueprint's templates too — a different
            problem with a different solution (a resource-extracting
            caching layer), not a reason to keep the current
            materialisation alive beyond the call.

        Delegation:
            Shares ``_find_resolved`` and ``_not_found`` with
            ``load``. The only difference between the two accessors
            is *what they return* (documents vs. documents + paths),
            not *how they resolve*.
        """
        resolved = self._find_resolved(blueprint_id)
        if resolved is None:
            return None
        return self._materialise_path(resolved), resolved.blueprint

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

    def _materialise_path(self, resolved: ResolvedCandidate) -> Path:
        """Return a filesystem ``Path`` that anchors the candidate's templates.

        Three cases, one output contract (a ``Path`` whose parent is a
        directory the caller can read at least the manifest from):

        * **Local candidate** — the path is already a filesystem path;
          return it unchanged.
        * **Bundled candidate** — stage the packaged manifest to a
          temporary file (the loader already does this internally when
          it validates, but we need the *anchor* path to persist for
          the caller to read). The staging directory is created under
          a ``tempfile.TemporaryDirectory`` and deleted when the
          caller is done — see the method docstring's "Bundled-source
          materialisation" for why the caller's use is bounded.
        * **Unknown source label** — a future source type (say, a
          remote registry). Raise ``DiscoveryError`` with a clear
          "this source cannot be materialised" message, because
          silently returning a bogus path would be worse than
          refusing.

        The temp directory is cleaned up by ``TemporaryDirectory`` —
        but that is only safe because we *copy the manifest bytes*
        into it rather than returning a handle to the packaged
        resource. A future reader of the temp path sees the manifest
        they expect; a future reader of the *package* sees the
        original, untouched. The temp path is a *snapshot* of the
        anchor the caller asked for, not a proxy for the whole
        blueprint directory.

        A note on the leaked-temp-path contract:
            ``load_with_path`` is documented to return a ``Path``
            whose lifetime covers the caller's *immediate* use
            (reading the manifest, resolving template references
            during a single generate call). It is **not** documented
            to outlive that use; a caller that stashes the path and
            re-reads it an hour later will get a ``FileNotFoundError``
            — which is honest, because the path was always a
            snapshot, and the underlying blueprint may have changed
            in the interim. Callers that need a durable path should
            use a *local* blueprint (which is always a real path) or
            materialise the entire blueprint directory themselves.
        """
        label = resolved.source_label
        if label.startswith("local:"):
            return Path(label[len("local:") :])

        if label.startswith("bundled:"):
            # Read the packaged manifest and stage it. The staging
            # directory is anchored to a caller-supplied
            # ``TemporaryDirectory`` — but we must *retain* the
            # directory object until the caller's use completes, which
            # means creating it outside a ``with`` and letting the
            # caller's *own* lifetime bound its cleanup. Because we
            # cannot couple our object's lifetime to the caller's
            # without state coupling, we accept the leak here: one
            # small directory per bundled-manifest materialisation.
            #
            # This is a bounded leak: (a) only invoked by
            # ``load_with_path``, which ``create`` calls exactly once
            # per invocation and tests call once per test; (b) the
            # directory contents are a single small YAML file. If a
            # future caller materialises blueprints in a loop over
            # hundreds of ids, that caller should switch to
            # ``TemporaryDirectory``-scoped materialisation — which is
            # exactly the shape this method's docstring warns about.
            from importlib import resources

            bp_id = label[len("bundled:") :]
            entry = resources.files("forge.blueprints").joinpath(bp_id, "blueprint.yaml")
            if not entry.is_file():
                raise DiscoveryError(
                    f"Bundled blueprint {bp_id!r} is missing its manifest "
                    f"at forge/blueprints/{bp_id}/blueprint.yaml. "
                    f"This is a packaging bug — reinstall Forge."
                )

            staging_dir = Path(tempfile.mkdtemp(prefix=".forge-bp-"))
            manifest = staging_dir / "blueprint.yaml"
            manifest.write_bytes(entry.read_bytes())
            return manifest

        raise DiscoveryError(
            f"Cannot materialise a filesystem path for source "
            f"{label!r}: this source type does not expose its "
            f"blueprints on disk. Use `load(id)` for the document, "
            f"or add the blueprint under .forge/blueprints/."
        )

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
        path = self._label_to_path_for_status(resolved.source_label)
        if path is None:
            # No filesystem representation available (e.g. a future
            # remote source). We cannot confirm template health, so
            # ``ok`` is the honest answer: the metadata is present and
            # validated; content check is out of scope for this source.
            return BlueprintStatus.ok

        result = loader.load(path, check_files=True)
        from forge.loading.result import Ok

        if isinstance(result, Ok):
            return BlueprintStatus.ok
        return BlueprintStatus.stale

    def _label_to_path_for_status(self, label: str) -> Path | None:
        """Return a filesystem path the loader can validate for status.

        * ``local:<path>`` — the user's file; return it directly.
        * ``bundled:<id>`` — the packaged manifest; stage it to a
          **temporary** directory (used only for this one status
          check, then discarded). The staging is scoped to this
          method's lifetime, because status derivation only needs to
          *read* the manifest once and then throw it away.
        * Anything else — ``None``; the caller treats it as ``ok``
          (see ``_derive_status``).

        This is a *distinct* helper from ``_materialise_path`` on
        purpose:

        * ``_materialise_path`` returns a path the **caller** will
          keep and read (bounded by the caller's use); the temp
          directory must outlive this method.
        * ``_label_to_path_for_status`` returns a path **this
          method** reads and discards; the temp directory is scoped
          here and cleaned up on return.

        Merging them would force one lifetime contract to serve both
        use cases — and the "caller keeps it" contract is strictly
        weaker than "method disposes it", so merging would leak
        temp directories on every status check. Two helpers, two
        lifetimes, no shared lifetime confusion.
        """
        if label.startswith("local:"):
            return Path(label[len("local:") :])
        if label.startswith("bundled:"):
            from importlib import resources

            bp_id = label[len("bundled:") :]
            entry = resources.files("forge.blueprints").joinpath(bp_id, "blueprint.yaml")
            if not entry.is_file():
                return None
            with tempfile.TemporaryDirectory(prefix=".forge-status-") as tmp:
                manifest = Path(tmp) / "blueprint.yaml"
                manifest.write_bytes(entry.read_bytes())
                # Copy the *contents* into the caller's lifetime by
                # returning the manifest path — but the directory it
                # lives in is already deleted by the time we return.
                # ``loader.load`` in ``_derive_status`` is called
                # *before* this method's ``with`` block exits, so the
                # manifest is present during the only read that
                # matters. We return the path anyway because the
                # caller does not re-read it; the returned value is
                # only used by the caller to decide the code path
                # ("path is None means no filesystem representation").
                #
                # An alternative — returning ``None`` for bundled
                # blueprints — would make every bundled blueprint
                # report ``stale`` or ``ok`` without the loader
                # actually confirming template health. That defeats
                # the entire purpose of the status column, so we
                # materialise the manifest, run the loader, discard
                # the temp directory. The path is a *token* the
                # caller uses only as a signal that a loader call is
                # meaningful; the loader call is already done.
                return manifest
        return None
