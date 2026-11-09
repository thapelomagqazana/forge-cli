"""Public façade for CLI output DTOs.

Consumers import from here, never from the individual modules directly:

    from forge.cli.schemas import ListOutput, ListOutputItem, BlueprintStatus

Why a façade?
-------------
* **Stable surface.** If we ever split or rename an internal module
  (`list_output.py` → `list/v1.py`, say), call sites are unaffected.
* **Discoverability.** One place lists every output contract the CLI
  publishes. A new contributor can see the whole output surface in one
  screen, which makes it obvious when a new format needs a new DTO.
* **Explicitness.** `__all__` is the contract of *this module*; the
  contract of *the CLI's output* is the union of the re-exported models
  and their docstrings. Keeping the two in sync is a review-time check,
  not an accident.

Convention
----------
Each submodule is named after the command whose output it defines
(`list_output.py` for `forge list`, and future `create_output.py`,
`validate_output.py`, …). Each submodule owns its own `schema_version`
in the envelope; this module does not re-export those constants
deliberately — consumers read the version *from the payload* they are
handed, not by importing a number that could drift between the module
and the runtime envelope.
"""

from __future__ import annotations

from forge.cli.schemas.list_output import (
    BlueprintStatus,
    ListOutput,
    ListOutputItem,
)

__all__ = [
    # `forge list` output contract (v1.0.0)
    "BlueprintStatus",
    "ListOutput",
    "ListOutputItem",
]
