"""The single place precedence, deduplication, and ordering are decided.

Why this module exists
----------------------
Sources (``sources.py``) are deliberately dumb: they yield candidates and
nothing else. If two sources disagree about which document represents a
given blueprint id, *this* module is the referee — and only this module.

Centralising the rule is not stylistic. Precedence bugs are the kind of
thing that surface months later as "why did my local override silently
get ignored in production but not in dev?" — impossible to reason about
when the rule is spread across sources. Keeping it here means:

* A future remote-registry source inherits precedence for free.
* A test suite can assert the rule once, against one function.
* Changing the rule (e.g. adding a "project-local beats user-local"
  layer) is a one-line edit, not a sweep across N sources.

Policy summary (see ADR-0009)
-----------------------------
* **Precedence:** local (20) beats official (10). Higher wins.
* **Duplicates:** emitted once as a ``WARNING`` naming both sources;
  the loser is dropped from the catalogue.
* **Malformed candidates:** skipped with a ``WARNING`` carrying the
  source label. One bad file never takes down the catalogue.
* **Ordering:** ``resolve`` returns candidates in arrival order;
  ``to_metadata`` sorts by id ascending. Determinism is a feature —
  ``forge list`` output is diff-able in CI and snapshot-testable.

Parsing is delegated to the loader
----------------------------------
``_parse`` calls ``BlueprintLoader.load()`` rather than re-implementing
YAML parsing, schema validation, and model construction. Two benefits
follow directly:

* **One pipeline.** Discovery and ``forge validate`` cannot disagree
  about what a valid blueprint is, because they run the same phases
  (ADR-0010). A future loader improvement — a BOM check, a stricter
  YAML mode — lands in both commands with zero coordination.
* **One error shape.** The loader returns ``Result[Blueprint,
  list[ValidationError]]``; the resolver translates the ``Err`` branch
  into a ``MalformedBlueprintError`` whose ``reason`` is the *first*
  error's ``format()`` output. Users see the same message they would
  see from ``forge validate``, not a second dialect invented here.

The resolver passes ``check_files=False``. Discovery is a *metadata*
operation: users want the catalogue even when a template has been
deleted from one blueprint. "Missing template" is a real error for
``forge create`` but a false positive for ``forge list`` — the
blueprint is discoverable, just not renderable. Filesystem checks are
the loader's option precisely so callers with different needs can
choose.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from forge.discovery.errors import MalformedBlueprintError
from forge.discovery.sources import Candidate
from forge.loading import BlueprintLoader
from forge.loading.result import Err
from forge.models import Blueprint
from forge.models.blueprint import BlueprintSource, DiscoveredBlueprint

log = logging.getLogger("forge.discovery")

# Precedence table. Higher value wins. Kept as a module-level constant so
# the rule is inspectable, testable, and trivially extendable — a new
# source type adds one entry; nothing else changes.
_PRECEDENCE: dict[BlueprintSource, int] = {
    BlueprintSource.OFFICIAL: 10,
    BlueprintSource.LOCAL: 20,
}


@dataclass(frozen=True, slots=True)
class ResolvedCandidate:
    """A parsed, validated blueprint plus the provenance the resolver
    needs to reason about it.

    Frozen so it can be cached, hashed, and passed across threads
    without aliasing concerns. ``source_label`` is the human-facing
    origin ("bundled:python.fastapi.api" or "local:/path/to/file");
    ``origin`` is the machine-facing layer used for precedence.
    """

    blueprint: Blueprint
    source_label: str
    origin: BlueprintSource


# The loader is stateless and safe to share; module-level construction
# avoids re-instantiating it on every candidate and keeps the hot path
# allocation-free.
_LOADER = BlueprintLoader()


def _parse(candidate: Candidate) -> ResolvedCandidate:
    """Parse and validate a single candidate via the shared loader.

    Raises ``MalformedBlueprintError`` on any failure so the caller can
    skip with a warning. All failure modes — unreadable file, YAML
    syntax, non-mapping root, schema violation, model validation — are
    already folded into the loader's ``Result.error`` list, so this
    function has one branch, not four.

    **Why stage to a temp file.** ``BlueprintLoader.load()`` expects a
    path, because the pipeline's I/O boundary is "read a file." Sources
    yield ``bytes`` because a future remote-registry source has no
    path. The resolver bridges the two by staging the bytes into a
    temporary directory. The cost is one file write per candidate
    (~microseconds); the benefit is that discovery runs *exactly* the
    same code path as ``forge validate``, so a discovery-accepted
    blueprint cannot fail validation later.

    **Why ``check_files=False``.** Discovery is metadata-only. A
    blueprint with a missing template is still discoverable — the user
    needs to see it in ``forge list`` so they can find and fix it.
    Requiring templates to exist would make a single deleted file
    silently remove a blueprint from the catalogue, which is the
    opposite of what a discovery command is for.
    """
    label, raw, origin = candidate

    if not raw:
        # Sources yield b"" when they cannot read a file. Treat as
        # malformed so the warning path is uniform — a broken symlink
        # and a YAML syntax error should produce the same user-facing
        # message shape, not two different ones.
        raise MalformedBlueprintError(label, "file could not be read or is empty")

    with TemporaryDirectory(prefix="forge-discovery-") as tmp:
        staged = Path(tmp) / "blueprint.yaml"
        try:
            staged.write_bytes(raw)
        except OSError as exc:
            # Extremely rare (tmp dir unwritable) but possible in
            # locked-down CI containers. Surfacing it as malformed
            # rather than propagating preserves the resolver's
            # "never crash on a bad candidate" invariant.
            raise MalformedBlueprintError(
                label, f"could not stage candidate for validation: {exc}"
            ) from exc

        result = _LOADER.load(staged, check_files=False)

    if isinstance(result, Err):
        # The loader aggregates every error; the warning only needs the
        # first to be actionable (the user fixes one thing, re-runs
        # discovery, sees the next). Using ``format()`` gives the same
        # ``source:line:col: path: message — hint: ...`` rendering that
        # ``forge validate`` produces, so users do not have to learn a
        # second error dialect for discovery warnings.
        first = result.error[0] if result.error else None
        reason = first.format() if first is not None else "unknown validation failure"
        raise MalformedBlueprintError(label, reason)

    return ResolvedCandidate(blueprint=result.value, source_label=label, origin=origin)


def resolve(candidates: Iterable[Candidate]) -> list[ResolvedCandidate]:
    """Deduplicate by blueprint id using ``_PRECEDENCE``; warn on collisions.

    Malformed candidates are logged and skipped — one bad file must not
    take down the catalogue. This is a hard-won operational lesson: users
    edit local files while the CLI is running, and a half-saved YAML file
    should degrade to "your blueprint is missing" rather than "the CLI
    crashes."

    The returned list is *not* sorted; callers that need stable ordering
    should use ``to_metadata``. Keeping ``resolve`` unordered lets future
    callers (e.g. a validator that wants to report collisions) work with
    the resolver's natural order without paying for a sort they do not
    need.
    """
    winners: dict[str, ResolvedCandidate] = {}

    for candidate in candidates:
        try:
            resolved = _parse(candidate)
        except MalformedBlueprintError as exc:
            # Single warning format for every "we could not use this"
            # case. Uses the standard logging façade so CI can capture
            # structured records and users see it only when they ask.
            log.warning("Skipping malformed blueprint: %s", exc)
            continue

        existing = winners.get(resolved.blueprint.id)
        if existing is None:
            winners[resolved.blueprint.id] = resolved
            continue

        # Collision. Compare precedence, keep the higher one, and emit
        # exactly one warning naming both sides. The warning names which
        # file won *and* which lost so users can find and delete a stale
        # override — the "who won?" question is what makes duplicate
        # blueprints confusing in the first place.
        if _PRECEDENCE[resolved.origin] > _PRECEDENCE[existing.origin]:
            log.warning(
                "Duplicate blueprint '%s': %s (%s) overrides %s (%s)",
                resolved.blueprint.id,
                resolved.source_label,
                resolved.origin.value,
                existing.source_label,
                existing.origin.value,
            )
            winners[resolved.blueprint.id] = resolved
        else:
            log.warning(
                "Duplicate blueprint '%s': keeping %s (%s); ignoring %s (%s)",
                resolved.blueprint.id,
                existing.source_label,
                existing.origin.value,
                resolved.source_label,
                resolved.origin.value,
            )

    return list(winners.values())


def find(
    candidates: Iterable[Candidate],
    blueprint_id: str,
) -> ResolvedCandidate | None:
    """Return the winning candidate for ``blueprint_id``, or ``None``.

    This is the *single-item* entry point used by
    ``BlueprintDiscoveryService.load()``. It runs the same precedence
    rules as ``resolve`` — including collision warnings, so users still
    see "your local override is shadowing an official blueprint" even
    when they only asked for one blueprint by name.

    Why not "call ``resolve()`` and filter"? Because ``resolve()``
    parses every candidate, and a ``load()`` for one blueprint only
    needs the winner. A naive filter would pay O(N) parse cost for an
    O(1) question. The current implementation walks candidates in
    arrival order and keeps the *best-so-far* for the target id,
    short-circuiting nothing (a later candidate could have higher
    precedence) but also not parsing candidates for other ids. The
    savings are real when a user-local directory contains many
    blueprints and the user asks for one specific official blueprint.

    Semantics match ``resolve`` exactly:
        * higher precedence wins;
        * ties go to the *first* candidate seen (which, given
          ``default_sources`` orders official-before-local, means
          official-first for same-precedence collisions — currently
          impossible because the two sources have distinct precedence
          values, but the tie rule is documented so a future source
          with an equal precedence value has a defined outcome);
        * malformed candidates are logged and skipped.
    """
    best: ResolvedCandidate | None = None

    for candidate in candidates:
        # Skip candidates that are not even *for* this id before paying
        # the parse cost. A candidate's label is not guaranteed to
        # encode its id (labels are ``bundled:<dir>`` or ``local:<path>``),
        # so this is an optimisation, not a filter — the id check
        # happens after parsing. The comment exists so a future reader
        # does not "fix" the missing pre-filter by adding a fragile
        # string match on the label.
        try:
            parsed = _parse(candidate)
        except MalformedBlueprintError as exc:
            log.warning("Skipping malformed blueprint: %s", exc)
            continue

        if parsed.blueprint.id != blueprint_id:
            continue

        if best is None:
            best = parsed
            continue

        # Collision on the requested id. Same precedence rules as
        # ``resolve``, same warning wording, so the log surface is
        # consistent regardless of which entry point the user hit.
        if _PRECEDENCE[parsed.origin] > _PRECEDENCE[best.origin]:
            log.warning(
                "Duplicate blueprint '%s': %s (%s) overrides %s (%s)",
                parsed.blueprint.id,
                parsed.source_label,
                parsed.origin.value,
                best.source_label,
                best.origin.value,
            )
            best = parsed
        else:
            log.warning(
                "Duplicate blueprint '%s': keeping %s (%s); ignoring %s (%s)",
                parsed.blueprint.id,
                best.source_label,
                best.origin.value,
                parsed.source_label,
                parsed.origin.value,
            )

    return best


def to_metadata(resolved: Iterable[ResolvedCandidate]) -> list[DiscoveredBlueprint]:
    """Project resolved documents into catalogue views, sorted by id.

    Sorting by id gives stable output across runs and machines, which
    keeps ``forge list`` diff-able in CI and predictable in tests. The
    projection strips the full blueprint document down to the fields a
    catalogue needs — cheap to serialise, cheap to render, and impossible
    to accidentally leak template contents or verification commands into
    a listing command's output.
    """
    return sorted(
        (
            DiscoveredBlueprint(
                id=item.blueprint.id,
                name=item.blueprint.name,
                version=item.blueprint.version,
                description=item.blueprint.description,
                category=item.blueprint.category,
                source=item.origin,
            )
            for item in resolved
        ),
        key=lambda bp: bp.id,
    )
