"""Collision detection for planned destination paths.

Why this file exists
--------------------
Two sources mapping to the same destination is the single most dangerous
class of planning bug:

    * it silently overwrites — a generated file from source A is replaced
      by source B, and the user has no way to know which one won;
    * it defeats reproducibility — swapping file order in the blueprint
      changes the output without changing the blueprint's meaning;
    * it hides behind path normalisation — `src/a.py`, `./src/a.py`, and
      `src//a.py` look different to a naive string comparison but write to
      the same file.

The fix is a set problem, not a loop problem: build a bucket per
normalised destination in one pass, then report any bucket with more than
one entry. The algorithm is trivially correct, and — crucially — *every*
colliding source is reported at once. Users fix the whole plan in one
pass, not one collision per iteration.

Design
------
* **Normalise before grouping.** Two paths that resolve to the same file
  must bucket together. Normalisation rules live in `destinations.py`
  (single source of truth); this module calls `normalise()` and never
  re-implements path-shape logic.
* **Aggregate, don't short-circuit.** Every collision is collected before
  returning. The planner merges these findings with any structural ones
  and returns them in a single `MappingError`.
* **Deterministic order.** Findings are sorted by normalised destination
  so the report is stable across runs and machines, and snapshot tests
  don't need to sort.
* **No I/O, no exceptions.** Pure function of the input list. Nothing to
  mock, nothing to clean up.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable

from forge.planning.destinations import normalise
from forge.planning.errors import Finding
from forge.planning.planned_file import PlannedFile


def find_collisions(files: Iterable[PlannedFile]) -> list[Finding]:
    """Return one finding per destination claimed by two or more sources.

    Parameters
    ----------
    files
        The planned writes to check. Order of input does not matter: the
        output is sorted by destination, and every member of a colliding
        bucket is included in that finding.

    Returns
    -------
    A list of `Finding` objects (empty on success). Each finding has:

        * `code` == "destination.collision"
        * `destination` == the *normalised* path shared by the colliders
        * `sources` == every source that mapped to that destination, in
          the order the planner emitted them (which is blueprint order,
          preserved by `map()` before sorting).

    Callers that expect at most one collision (e.g. `forge validate`) can
    iterate the list to render each; callers that only need yes/no can
    use `bool(find_collisions(...))`.

    Design note on `sources` order
    ------------------------------
    We preserve planner order rather than sorting sources. Why?
        * Blueprint order is meaningful to the author (files[] order),
          so showing the sources "in the order you wrote them" is the
          least surprising choice.
        * If we sorted sources, two blueprints with identical files[]
          in different order would produce different reports — the
          opposite of what "deterministic" should mean here.
    The *findings* list is sorted by destination; within a finding, the
    source tuple stays in blueprint order.
    """
    # defaultdict(list) keeps the hot path allocation-light: no branch on
    # "is this bucket missing?" for each iteration. Every bucket is a
    # candidate for a collision finding, but most will have len == 1.
    buckets: dict[str, list[PlannedFile]] = defaultdict(list)
    for planned in files:
        buckets[normalise(planned.destination)].append(planned)

    findings: list[Finding] = []
    for destination, members in buckets.items():
        if len(members) > 1:
            findings.append(
                Finding(
                    destination=destination,
                    message=(f"{len(members)} sources map to the same destination"),
                    hint=(
                        "Each destination must be produced by exactly one "
                        "source. Rename one of the sources, adjust a "
                        "destination template, or move one of the files "
                        "to a different directory."
                    ),
                    code="destination.collision",
                    sources=tuple(m.source for m in members),
                )
            )

    # Stable order: sort findings by destination so the report is
    # diffable across runs. Sorting here (rather than at the caller)
    # keeps every consumer of collisions.py deterministic for free.
    findings.sort(key=lambda f: f.destination)
    return findings
