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
* **Ordering:** the final list is sorted by blueprint id ascending.
  Determinism is a feature — ``forge list`` output is diff-able in CI
  and snapshot-testable.

Parsing is delegated to the verification funnel
-----------------------------------------------
``_parse`` calls ``validate_blueprint_document`` (the schema-then-model
funnel from ADR-0007 and ADR-0008) rather than re-implementing
validation. Discovery cannot accept a document the rest of the system
would reject, and schema changes propagate here with zero coordination.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass

import yaml
from pydantic import ValidationError

from forge.core.exceptions import ForgeError
from forge.discovery.errors import MalformedBlueprintError
from forge.discovery.sources import Candidate
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


def _parse(candidate: Candidate) -> ResolvedCandidate:
    """Parse and validate a single candidate.

    Raises ``MalformedBlueprintError`` on any failure so the caller can
    skip with a warning. All parsing errors — YAML syntax, non-mapping
    root, schema violation, model validation failure — are funnelled
    into a single exception type so the resolver has one branch, not
    four.

    Parsing flows through ``validate_blueprint_document`` deliberately:
    discovery must not accept documents that the rest of the system
    rejects. If we parsed here and validated later, a malformed local
    blueprint would land in the catalogue and explode on first use —
    which is exactly the failure mode "skip malformed" is meant to
    prevent.
    """
    # Local import keeps the discovery module importable without
    # pulling in jsonschema/pydantic at module load. The service is
    # cheap to construct; this import only costs anything when resolve()
    # is actually called.
    from forge.verification import validate_blueprint_document

    label, raw, origin = candidate

    if not raw:
        # Sources yield b"" when they cannot read a file. Treat as
        # malformed so the warning path is uniform — a broken symlink
        # and a YAML syntax error should produce the same user-facing
        # message shape, not two different ones.
        raise MalformedBlueprintError(label, "file could not be read or is empty")

    try:
        document = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise MalformedBlueprintError(label, f"invalid YAML: {exc}") from exc

    if not isinstance(document, dict):
        # ``safe_load`` returns ``None`` for empty documents and non-dict
        # scalars for pathological input. Both are blueprint-invalid;
        # catching them here gives a clearer message than the schema's
        # "not an object" error would.
        raise MalformedBlueprintError(
            label, f"expected a mapping at document root, got {type(document).__name__}"
        )

    try:
        blueprint = validate_blueprint_document(document, source=label)
    except ValidationError as exc:
        # Pydantic's native exception escapes only when the verification
        # façade is bypassed. Catching it here means discovery is robust
        # even if a future caller changes the façade's contract.
        raise MalformedBlueprintError(label, f"model validation failed: {exc}") from exc
    except ForgeError as exc:
        # SchemaValidationError and ModelValidationError both derive from
        # ForgeError. Their __str__ already includes structured pointers;
        # we wrap rather than re-format so the reason string carries the
        # full detail into the warning log.
        raise MalformedBlueprintError(label, str(exc)) from exc

    return ResolvedCandidate(blueprint=blueprint, source_label=label, origin=origin)


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
