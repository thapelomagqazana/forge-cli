"""Shared status vocabulary for discovered blueprints.

Why this module exists (and is not part of ``blueprint.py``)
------------------------------------------------------------
``BlueprintStatus`` is used by two layers that must not depend on each
other:

* ``forge.discovery`` needs to report, per blueprint, whether the file
  on disk is fully usable (``ok``) or parseable-but-incomplete
  (``stale``). This answer is derived from a loader pass, not from
  anything CLI-specific.
* ``forge.cli.schemas.list_output`` needs the same enum in its output
  DTO so table and JSON renderers see the same type (per ADR-0012).

Putting the enum in either consumer forces an import in the forbidden
direction:

* In ``forge.cli``, ``forge.discovery`` would need to import the CLI —
  violating ADR-0009 ("nothing in this package imports the CLI").
* In ``forge.models.blueprint``, ``forge.discovery`` would be fine, but
  ``forge.models.blueprint`` is *generated* (ADR-0008) and any hand
  addition there is erased on the next ``make models`` run, or forces
  the file to grow a hand-tuned region that is not tied to the schema.

So the enum lives in its own hand-written module under ``forge.models``
— sibling to the generated ``blueprint.py``, not part of it. Both
consumers import from here; the CLI's schemas module re-exports the
name so ``from forge.cli.schemas.list_output import BlueprintStatus``
keeps working, and the discovery façade re-exports it so
``from forge.discovery import BlueprintStatus`` keeps working too.

Identity invariant
------------------
There is exactly *one* ``BlueprintStatus`` class in the codebase.
Consumers that import it via ``forge.cli.schemas.list_output`` or via
``forge.discovery`` receive the **same object** (not two
structurally-identical enums). That is what makes ``isinstance`` and
``==`` behave the same regardless of which import path a caller used,
and what lets table and JSON renderers accept status values from the
service without a conversion step. A guard test in
``tests/unit/models/test_status_single_definition.py`` asserts this
invariant by walking ``sys.modules`` and counting definitions.
"""

from __future__ import annotations

from enum import Enum


class BlueprintStatus(str, Enum):
    """Availability signal for a discovered blueprint.

    Values are stable strings (not integers) so that logs, JSON
    envelopes, and dashboards remain human-readable without an enum
    lookup table, and so that adding a new status later is additive for
    consumers that ignore unknown values.

    Semantics
    ---------
    ``ok``
        The blueprint parses, validates against schema and model, and
        every referenced template file exists.

    ``stale``
        The blueprint parses and validates, but at least one referenced
        template file is missing. Metadata is still trustworthy; the
        *content* is out of date. The user can inspect it, but
        ``forge create`` from this blueprint will fail at render time
        until the files are restored.

    ``error``
        Reserved. Currently suppressed by the default catalogue
        (discovery skips malformed blueprints, per ADR-0009). Exists
        in the enum so a future ``--include-broken`` mode can surface
        it under the *same* JSON envelope without a schema-version bump
        for consumers that already handle the field's full value set.
    """

    ok = "ok"
    stale = "stale"
    error = "error"
