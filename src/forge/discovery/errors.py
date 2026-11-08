"""Discovery-scoped errors.

Why a dedicated module
----------------------
Discovery needs to signal "this specific candidate could not be turned
into a blueprint" without telling the *resolver* which of the many
underlying failures (unreadable file, YAML syntax, non-mapping root,
schema violation, model validation) actually occurred. The resolver
treats all of them identically: log, skip, continue.

Collapsing the failure space into a single exception type is what lets
the resolver stay one branch wide. It is not a loss of information —
the ``reason`` string carries the full underlying detail, so warnings
and error messages remain specific even though the *type* is uniform.

Why this module re-exports instead of defining
----------------------------------------------
``MalformedBlueprintError`` and ``DiscoveryError`` are defined in
``forge.core.exceptions``, not here. The ``core`` module owns the
exception hierarchy (ADR-0005); its docstrings document the exit-code
contract, the traceback-preservation reasoning, and the field-level
stability guarantees. This module exists so discovery code has a
narrow import surface:

    from forge.discovery.errors import MalformedBlueprintError

without reaching into ``core`` directly. That keeps the layering
story intact (``discovery`` imports ``core``) while giving the
subsystem a stable import path for its own error type.

A second class definition here would be worse than useless:

* Two classes with the same name and the same base would silently
  diverge as soon as one of them gained a field or changed its
  message format. A resolver that catches the discovery-local class
  would not catch the ``core`` version raised by a future parser, and
  the error would escape as an internal error to the CLI.
* ``frozen=True`` on an ``Exception`` subclass is a documented
  footgun (see ``forge.core.exceptions``'s module docstring): it
  breaks ``__traceback__`` assignment under ``contextlib.__exit__``.
  The ``core`` class uses ``frozen=False`` deliberately; a local
  definition would have to either repeat that reasoning (which is
  what the ``core`` docstring already does) or re-introduce the bug.
* ``exit_code = 1`` (a raw ``int``) is not a subtype of the base's
  ``exit_code: ExitCode`` annotation; mypy correctly rejects it. The
  ``core`` definition uses ``ExitCode.BLUEPRINT_ERROR``.

Relationship to ``forge.core.exceptions``
-----------------------------------------
``MalformedBlueprintError`` derives from ``DiscoveryError``, which
derives from ``ForgeError``. This preserves the architecture's
invariant that any Forge failure carries a stable ``exit_code`` and
a human-readable ``__str__``, so CLI boundaries never need a special
case for discovery failures — they translate uniformly like
everything else.

The module is deliberately thin (two names, both re-exported) so the
discovery subsystem stays extractable: it could become its own
distribution tomorrow without dragging half of ``forge.core`` along
with it.

See also
--------
* ``docs/adr/0005-stabilize-exit-codes-as-a-public-contract.md`` for
  the exit-code contract these errors participate in.
* ``forge.core.exceptions`` for the full exception hierarchy and the
  reasoning behind each subclass.
"""

from __future__ import annotations

from forge.core.exceptions import (
    DiscoveryError,
    MalformedBlueprintError,
)

__all__ = ["DiscoveryError", "MalformedBlueprintError"]
