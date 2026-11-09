"""Versioned output DTOs for `forge list` — the contract between command
and renderer.

Why this module exists (see ADR-0012)
-------------------------------------
Every command that has a machine-readable output mode needs a *contract*:
a stable set of field names, types, and constraints that downstream
automation can rely on. When that contract is implicit (a plain `dict`,
a `TypedDict`, or worse, whatever the renderer happens to produce), it
drifts — a field gets renamed in a refactor, an enum grows a new member,
and every CI pipeline that parses `forge list --json` breaks silently.

The DTO fixes that by being:

* **Explicit.** Fields, types, and constraints are declared once and
  validated on construction. No shape can appear in the output that
  isn't part of the model.
* **Versioned.** The envelope carries `schema_version` so consumers can
  pin behaviour and so we can introduce breaking changes additively
  (bump to `2.0.0`, emit both for a release cycle, then drop v1).
* **Shared.** Table and JSON renderers both consume the *same* DTO, so
  the two formats cannot disagree about field names or ordering.
* **Strict.** `extra="forbid"` and `frozen=True` mean an accidental
  extra field raises immediately at construction (a bug the author sees
  in tests) instead of leaking into JSON and surprising consumers.

Field naming
------------
Fields are `snake_case` in Python and serialise to the same names in
JSON. We *could* camelCase on the wire, but the project's existing JSON
surfaces (blueprints, schemas, config files) are snake_case throughout;
consistency beats conventional prettiness.

Where ``BlueprintStatus`` lives
-------------------------------
`BlueprintStatus` is *defined* in `forge.models.status` — a small,
hand-written module — not here, and not in the generated
`forge.models.blueprint`.

The reason is layering, and it is load-bearing:

* `forge.discovery` needs to return `BlueprintStatus` values from
  `discover_with_status()`. `forge.discovery` is a domain package and
  must not import from `forge.cli` (ADR-0009). If this module were the
  definition site, the only way for discovery to name its return type
  would be a forbidden import.
* `forge.models.blueprint` is *generated* from the canonical JSON
  Schema (ADR-0008). Hand-adding an enum to a generated file is erased
  on the next `make models` run, and hand-tuned regions in generated
  files are reserved for semantic gates the generator cannot express
  (validators, aliases) — not for unrelated vocabulary.

So `BlueprintStatus` lives in its own module, and *this* module re-exports
it. Callers keep the import they always used:
`from forge.cli.schemas.list_output import BlueprintStatus` still works,
and it resolves to the same class discovery returns. Identity is
preserved end-to-end: `ListOutputItem.status is discovery.BlueprintStatus`
is `True`, so `isinstance` checks and `==` comparisons behave the same
regardless of import path. A guard test
(`tests/unit/models/test_status_single_definition.py`) asserts this.

What this module is *not*
-------------------------
* Not a renderer — no formatting, no ANSI, no column logic.
* Not a sorting layer — the command sorts once (per ADR-0012); the DTO
  preserves order verbatim.
* Not a persistence model — discovery returns richer `DiscoveredBlueprint`
  objects; this DTO is the *projection* surfaced to users and automation.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

# Reuse the shared constrained-string aliases so a change to the canonical
# blueprint ID / SemVer / category rules propagates here automatically.
from forge.models.blueprint import BlueprintId, Category, SemVer

# ``BlueprintStatus`` is defined in ``forge.models.status`` and re-exported
# here for two reasons:
#
#   1. **Layering.** ``forge.discovery`` needs to return status values and
#      may not import from ``forge.cli`` (ADR-0009), so the definition
#      cannot live in this file.
#
#   2. **Continuity.** Existing call sites — tests, `forge list` code,
#      anywhere that reads ``ListOutputItem.status`` — import the name
#      from this module. Re-exporting preserves those imports and, more
#      importantly, preserves *class identity*: the class below resolves
#      to the same object as ``forge.discovery.BlueprintStatus``. That is
#      what makes ``isinstance`` and ``==`` behave the same regardless of
#      which import path a caller used.
#
# The ``noqa`` is because ruff's ``F401`` cannot see that a re-export is
# intentional; ``__all__`` below is the canonical declaration, and the
# import is the mechanism.
from forge.models.status import BlueprintStatus  # (re-export)

# ---------------------------------------------------------------------------
# Item and envelope
# ---------------------------------------------------------------------------


class ListOutputItem(BaseModel):
    """A single row in the `forge list` output.

    Field set is *exactly* what the CLI contract guarantees. Adding a
    field is a **minor** version bump of the envelope; removing or
    renaming one is **major** (see `CLI_LIST_OUTPUT.md`).

    Constraints mirror the blueprint model's own rules so that a value
    that passed blueprint validation can never fail DTO construction —
    if that ever happens, it is a bug in the projection code, not in
    the user's data, and `strict` + `extra="forbid"` will make it
    obvious in tests rather than silent in production.
    """

    model_config = ConfigDict(
        frozen=True,  # immutable once built; renderers cannot mutate
        extra="forbid",  # no accidental fields; shape is the contract
        str_strip_whitespace=False,  # preserve exactly what discovery gave us
    )

    id: BlueprintId
    """Dotted blueprint identifier, e.g. ``python.fastapi.api``."""

    name: str = Field(min_length=1, max_length=120)
    """Human-readable name shown in the NAME column."""

    version: SemVer
    """Blueprint version (full SemVer), shown in the VERSION column."""

    category: Category
    """Coarse classifier (api, automation, …), shown in the CATEGORY column."""

    status: BlueprintStatus
    """Availability signal, shown in the STATUS column (see enum for semantics)."""


class ListOutput(BaseModel):
    """Root envelope for `forge list` output.

    An object envelope (not a bare array) is chosen deliberately:

    * It is **extensible**. Adding top-level metadata later (e.g.
      ``generated_at``, ``forge_version``, ``total``) never breaks
      consumers that already read ``blueprints``.
    * It carries its own **contract version**, so consumers can pin
      behaviour explicitly rather than inferring it from field presence.
    * It makes error-shaped envelopes possible in the future (e.g.
      ``{"schema_version": "1.0.0", "blueprints": [], "warning": "..."}``)
      without a second document format.

    Consumers must read ``blueprints`` and ignore unknown top-level keys.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = "1.0.0"
    """Version of *this* output contract (not the blueprint schema).

    Bump rules:

    * **major** — a required field is removed or renamed, or an existing
      field changes type in a non-compatible way.
    * **minor** — a field is added (consumers must ignore unknown fields).
    * **patch** — a documentation or wording change with no shape impact.
    """

    blueprints: list[ListOutputItem]
    """Ordered catalogue. The command sorts this list once (by ``id``
    ascending) before construction; consumers may rely on that ordering
    for the current schema version."""


__all__ = [
    "BlueprintStatus",  # re-exported; defined in forge.models.status
    "ListOutput",
    "ListOutputItem",
]
