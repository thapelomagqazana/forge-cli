"""Scenario definitions for `forge list` fixtures.

Each named scenario describes a discovery result that the tests feed
into the command, along with the fixture file that captures the
expected output. Keeping scenarios next to the fixtures means a
reviewer can see, at a glance, what each fixture represents without
reading the test bodies.

The scenarios are *data* (blueprint metadata + status pairs), not
service instances — tests compose them with mocked
``BlueprintDiscoveryService`` to exercise the command deterministically.
"""

from __future__ import annotations

from dataclasses import dataclass

from forge.cli.schemas.list_output import BlueprintStatus
from forge.models.blueprint import (
    BlueprintSource,
    DiscoveredBlueprint,
)


@dataclass(frozen=True, slots=True)
class Scenario:
    """A named `(discovery result, expected fixture)` tuple."""

    name: str
    blueprints: tuple[tuple[DiscoveredBlueprint, BlueprintStatus], ...]
    fixture: str


def _bp(
    id_: str,
    name: str,
    version: str = "1.0.0",
    category: str = "api",
    source: BlueprintSource = BlueprintSource.OFFICIAL,
) -> DiscoveredBlueprint:
    return DiscoveredBlueprint(
        id=id_,
        name=name,
        version=version,
        description="—",
        category=category,
        source=source,
    )


_OFFICIAL = (
    (_bp("python.automation.cli", "Python Automation CLI"), BlueprintStatus.ok),
    (_bp("python.fastapi.api", "Python FastAPI API"), BlueprintStatus.ok),
    (
        _bp("typescript.frontend.react", "TypeScript React Frontend", category="frontend"),
        BlueprintStatus.ok,
    ),
)


SCENARIOS: dict[str, Scenario] = {
    "official": Scenario(
        name="official",
        blueprints=_OFFICIAL,
        fixture="golden_official.json",
    ),
    "local_override": Scenario(
        name="local_override",
        blueprints=(
            (_bp("python.automation.cli", "Python Automation CLI"), BlueprintStatus.ok),
            (
                _bp("python.fastapi.api", "Local FastAPI Override", source=BlueprintSource.LOCAL),
                BlueprintStatus.ok,
            ),
            (
                _bp("typescript.frontend.react", "TypeScript React Frontend", category="frontend"),
                BlueprintStatus.ok,
            ),
        ),
        fixture="golden_local_override.json",
    ),
    "stale": Scenario(
        name="stale",
        blueprints=(
            (_bp("python.automation.cli", "Python Automation CLI"), BlueprintStatus.ok),
            (_bp("python.fastapi.api", "Python FastAPI API"), BlueprintStatus.stale),
            (
                _bp("typescript.frontend.react", "TypeScript React Frontend", category="frontend"),
                BlueprintStatus.ok,
            ),
        ),
        fixture="golden_stale.json",
    ),
    "empty": Scenario(
        name="empty",
        blueprints=(),
        fixture="envelope_empty.json",
    ),
}
