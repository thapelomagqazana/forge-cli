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
under ``forge/blueprints/<id>/blueprint.yaml`` and are reached via
``importlib.resources.files("forge.blueprints")``.

**On every supported installation mode** — source checkout,
``pip install -e .``, ``pip install <wheel>`` — ``importlib.resources``
yields a traversable that is backed by a **real filesystem path**.
That path's *parent* is the blueprint directory, which contains both
``blueprint.yaml`` and the ``templates/`` subdirectory the blueprint
references. Returning it as the materialised path makes every
``files[].source`` resolvable against it — the same way it already
works for local blueprints.

An earlier design staged the manifest into a ``tempfile.mkdtemp``
directory and returned the staged path's parent. That was correct for
callers that only wanted to **read the manifest** (a one-shot read is
fine from a temp copy) but wrong for callers that want to **resolve
template paths against the manifest's directory** — the temp directory
contains no ``templates/`` and every source lookup fails. The
``create`` command's ``_blueprint_root`` is exactly such a caller: it
passes the returned path's parent to the staging engine as
``blueprint_root``, and every ``files[].source`` is resolved against
it. Staging the manifest reproduced the "template file not found"
failure for every bundled blueprint, even though the templates were
present in the package the whole time.

The current design fixes that by returning the real package path.
Zip-import installation (a ``.zip`` on ``sys.path``) is the one mode
where the packaged files are *not* on a real filesystem; the engine
already does not support it (templates are read from disk by the
loader, the renderer, and the safety layer), so this module fails
loudly at the conversion site rather than silently returning a wrong
path. See ``_materialise_path`` for the specific error message and
recovery hint.

Why the ``# type: ignore[arg-type]`` on ``Path(entry)``
-------------------------------------------------------
``importlib.resources.files("forge.blueprints")`` returns a
``Traversable``, and after ``.joinpath(bp_id, "blueprint.yaml")``, the
result is still a ``Traversable``. On every supported installation
mode that traversable is *backed by* a real filesystem path (a
``pathlib.Path`` subclass instance), but the *static type* the stdlib
stubs declare is the abstract ``Traversable`` — which is not
``os.PathLike`` in mypy's view, so ``Path(entry)`` fails
``[arg-type]``.

At runtime, ``Path(entry)`` succeeds whenever the traversable is a
real ``Path`` (which it is on filesystem-backed installs). On the one
unsupported mode — a zip on ``sys.path`` — ``entry`` is a
``zipfile.Path``, and ``Path(zipfile.Path(...))`` raises
``TypeError``. The code below catches ``TypeError`` and turns it into
a clear ``DiscoveryError`` naming the unsupported installation mode,
so the runtime failure is *handled*, not silent.

The ``# type: ignore[arg-type]`` records the intentional gap between
"mypy sees ``Traversable``" and "the value is really
``os.PathLike`` on every mode we support." The alternative — casting
to ``Path`` first, or calling ``str(entry)`` and constructing a
``Path`` from the string — would either defer the same mypy complaint
to a different line (a cast is ``Any`` to the checker) or introduce a
subtle bug (``str(zipfile.Path)`` produces a plausible-looking path
that does not exist on disk; the ``TypeError`` from ``Path(...)`` is
exactly the signal we want on that mode).

The same suppression appears in ``_label_to_path_for_status`` for the
identical reason: both methods convert a ``Traversable`` to a
``Path`` and both need the runtime ``TypeError`` catch to keep
zip-mode from silently producing a wrong status.

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
            via ``importlib.resources``. On every supported
            installation mode that path is backed by a **real
            filesystem path**, and it is the manifest of a
            directory that also contains the blueprint's
            ``templates/``. The returned path is therefore the
            correct anchor for template resolution: the caller can
            read the manifest *and* every template the blueprint
            references from the manifest's parent directory.

            An earlier version of this method staged the manifest
            into a temp directory and returned that. That broke
            template resolution for `create` — see the module
            docstring's "Bundled-source materialisation" section
            for the full history and the reason the current design
            returns the real path instead.

            Zip imports are unsupported (the engine reads templates
            from disk); ``_materialise_path`` fails loudly on that
            installation mode rather than silently returning a
            path that cannot resolve sources.

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
        """Return the real filesystem manifest path for a resolved candidate.

        Three cases, one output contract: a ``Path`` whose *parent
        directory* is the blueprint's directory — the directory that
        contains both ``blueprint.yaml`` and (for blueprints that
        ship templates) a ``templates/`` subdirectory the caller can
        read.

        * **Local candidate** — the label already carries the manifest
          path. Return it unchanged. Local blueprints are the anchor's
          ideal case: the manifest and its templates both live on disk
          at a stable path the caller controls.

        * **Bundled candidate** — the manifest and its templates both
          live inside the installed ``forge.blueprints`` package. On
          every supported installation mode — source checkout,
          ``pip install -e .``, ``pip install <wheel>`` —
          ``importlib.resources.files`` yields a traversable that is
          backed by a **real filesystem path**, and that path's
          parent is the direct anchor the caller needs. Return it
          without staging.

          This is a change from an earlier design that staged the
          manifest into a ``tempfile.mkdtemp`` directory and returned
          the staged path. That design worked for callers that only
          wanted to **read the manifest** (a one-shot read is fine
          from a temp copy) but broke for callers that wanted to
          **resolve template paths against the manifest's directory**:
          the temp directory contains only a copy of the manifest and
          no ``templates/``, so every source lookup failed. See the
          module docstring's "Bundled-source materialisation" section.

        * **Unknown source label** — a future source type (say, a
          remote registry). Raise ``DiscoveryError`` with a clear
          "this source cannot be materialised" message. Silently
          returning a bogus path would be worse than refusing. A
          future remote source that needs to expose a filesystem
          anchor should materialise its *whole* blueprint directory
          (manifest + templates) to a stable cache location and
          return that; the fix shape is the same, but the cache
          location is the source's responsibility.

        Zip-import failure mode
        -----------------------
        If the installed package is a ``.zip`` on ``sys.path`` (an
        installation mode the blueprint engine does not support,
        because templates are read from disk by the loader, the
        renderer, and the safety layer),
        ``importlib.resources.files`` yields a traversable that is
        not a real ``Path``. We detect that at the ``Path(entry)``
        conversion and raise a clear ``DiscoveryError`` naming the
        unsupported mode — rather than silently staging the manifest
        (which reproduces the bug we are fixing) or attempting to
        materialise the whole package (which is out of scope for
        this method and would require a caching layer).
        """
        label = resolved.source_label

        # Local blueprints: the label *is* the manifest path. Return
        # it directly. The caller's directory anchor is the
        # manifest's parent, which is exactly where the blueprint's
        # templates live.
        if label.startswith("local:"):
            return Path(label[len("local:") :])

        # Bundled blueprints: return the real package path. On every
        # supported install mode this is a filesystem path whose
        # parent contains the blueprint's templates.
        if label.startswith("bundled:"):
            from importlib import resources

            bp_id = label[len("bundled:") :]
            entry = resources.files("forge.blueprints").joinpath(
                bp_id,
                "blueprint.yaml",
            )
            if not entry.is_file():
                raise DiscoveryError(
                    f"Bundled blueprint {bp_id!r} is missing its manifest "
                    f"at forge/blueprints/{bp_id}/blueprint.yaml. "
                    f"This is a packaging bug — reinstall Forge."
                )

            # ``entry`` is a traversable. On every supported install
            # mode it is backed by a real filesystem path; on an
            # unsupported mode (a zip on ``sys.path``) it is not, and
            # ``Path(entry)`` raises ``TypeError`` because the
            # traversable is not ``os.PathLike``.
            #
            # ``# type: ignore[arg-type]`` because the stdlib stubs
            # declare ``Traversable``, not ``os.PathLike``. See the
            # module docstring's "Why the ``# type: ignore[arg-type]``
            # on ``Path(entry)``" section for the reasoning: the value
            # *is* ``os.PathLike`` on every supported install mode, and
            # the unsupported mode is caught at runtime by the
            # ``TypeError`` handler below.
            try:
                real_path = Path(entry)  # type: ignore[arg-type]
            except TypeError as exc:
                raise DiscoveryError(
                    f"Bundled blueprint {bp_id!r} is inside a zip "
                    f"archive (its manifest is not on the filesystem). "
                    f"The Forge blueprint engine requires templates "
                    f"to be readable from disk; install Forge from a "
                    f"wheel or a source checkout instead of a zip."
                ) from exc

            # Defensive: if a future packaging change ever returns a
            # traversable that stringifies to something that is not a
            # real file, fail loudly. This is a check on the *result*
            # of the conversion, not on the conversion itself — it
            # catches any path where ``Path(entry)`` succeeds but the
            # resulting path is not readable.
            if not real_path.is_file():
                raise DiscoveryError(
                    f"Bundled blueprint {bp_id!r} manifest is not "
                    f"readable at {real_path}. This is a packaging or "
                    f"permissions bug — reinstall Forge or check file "
                    f"permissions."
                )

            return real_path

        # Unknown source type — refuse rather than silently return a
        # wrong anchor. A future source that needs to materialise
        # should implement the anchor contract itself: manifest and
        # templates both present in the returned directory.
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
        * ``bundled:<id>`` — return the real packaged manifest path.
          Same reasoning as ``_materialise_path``: the loader's
          ``check_files=True`` resolves template references against
          the manifest's directory, and the packaged directory is
          the only one that actually contains the templates. A temp
          copy would report every bundled blueprint as ``stale``
          regardless of whether the templates exist — the exact
          false-negative this method must not produce.
        * Anything else — ``None``; the caller treats it as ``ok``
          (see ``_derive_status``).

        This is a *distinct* helper from ``_materialise_path`` on
        purpose:

        * ``_materialise_path`` returns the caller's anchor — its
          lifetime is bounded by the caller's use, and its correctness
          is checked by ``create``'s template resolution.
        * ``_label_to_path_for_status`` returns the status-check
          anchor — its lifetime is this method's, and its correctness
          is checked by the CLI's ``forge list`` status column.

        Keeping them separate means a future change to one (say, a
        new source type that needs different staging) does not
        silently change the other. They happen to share the same
        implementation today because both need the *real* directory;
        if they diverge in the future, they can diverge without
        coordination.
        """
        if label.startswith("local:"):
            return Path(label[len("local:") :])
        if label.startswith("bundled:"):
            from importlib import resources

            bp_id = label[len("bundled:") :]
            entry = resources.files("forge.blueprints").joinpath(
                bp_id,
                "blueprint.yaml",
            )
            if not entry.is_file():
                return None
            try:
                # ``# type: ignore[arg-type]`` on the same reasoning as
                # ``_materialise_path``: the stdlib stubs declare the
                # traversable as ``Traversable`` (not ``os.PathLike``),
                # but on every supported install mode it *is* a real
                # ``Path``. The unsupported mode (zip on ``sys.path``)
                # raises ``TypeError`` below and is treated as "no path"
                # — the status is then ``ok`` because we cannot check
                # it, which is the same honest default the unknown-source
                # branch uses.
                real_path = Path(entry)  # type: ignore[arg-type]
            except TypeError:
                # Zip import — status derivation cannot confirm
                # template health; treat as ``ok`` for the same reason
                # an unknown source is ``ok``: we cannot check, so
                # reporting ``stale`` would be a false negative.
                return None
            return real_path if real_path.is_file() else None
        return None
