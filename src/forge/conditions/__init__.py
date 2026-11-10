"""Public façade for the conditions subsystem.

Why a façade?
-------------
Every module in Forge that has an internal structure (loading, resolution,
planning, rendering, conditions) exposes a *tiny* public surface and hides
its internals. This module is that surface for conditions. Callers write::

    from forge.conditions import ConditionalEvaluator, ConditionError

and nothing else. Everything behind that — the grammar data, the parser,
the AST, the type-compatibility rule, the `_pretty` helper — is
implementation detail and free to change without touching a single call
site.

What is exported
----------------
* ``ConditionalEvaluator`` — the pure function-like orchestrator.
  Construct once, call ``.evaluate(blueprint, variables)``; stateless,
  thread-safe, safe to cache.
* ``ConditionError`` — the aggregate exception raised at the CLI
  boundary. Belongs on the public surface because callers must be able
  to catch it explicitly (never ``except Exception``).
* ``Finding`` — the individual structured error record. Exposed because
  callers frequently need to iterate findings for their own reporting
  (JSON output, wizard prompts, CI annotations), and forcing them to
  reach into ``ConditionError.findings[0].<field>`` without a named
  type would be hostile.

What is *deliberately not* exported
-----------------------------------
* ``grammar`` — the grammar is a versioned public contract, but a
  contract that lives in its *documentation* (``CONDITION_GRAMMAR.md``)
  and its *behaviour* (tests). Exposing the data module would tempt
  callers to write ``for rule in GRAMMAR: ...`` and couple themselves to
  the internal shape of each rule.
* ``parser`` — parsing is the evaluator's concern. A caller that wants
  to parse conditions in isolation is almost certainly about to
  re-implement part of the evaluator, which is exactly what a façade
  prevents. If a future need arises (e.g. an editor plugin), it can be
  exposed under its own ADR.
* ``ast`` — the AST is an internal seam between parser and evaluator
  (see ``ast.py``'s docstring for the full rationale). Exposing node
  classes would tempt hand-construction, bypassing the parser and its
  guarantees.
* ``errors`` — the module is public API, but its *contents* are already
  re-exported at this level. Re-exporting the module would give callers
  two equivalent import paths and force us to keep both stable.

Design notes
------------
* **No side effects at import.** Importing this module must not touch
  the filesystem, compile regexes eagerly beyond what `parser` already
  does, log, or register anything. A pure façade keeps ``import
  forge.conditions`` free of surprises for libraries that merely want to
  reference the types.
* **Version-tagged.** The subsystem has a documented grammar version
  (``CONDITION_GRAMMAR.md`` says ``v1.0.0``). ``__version__`` mirrors it
  so callers and CI can assert compatibility without reading docs. It
  describes the *DSL version*, not the Python package version.
* **Explicit ``__all__``.** Downstream tooling (autocompletion, static
  analysis, docs) reads ``__all__`` as the public API. Only the three
  names above appear; the list will grow only by deliberate change.
"""

from __future__ import annotations

from forge.conditions.errors import ConditionError, Finding
from forge.conditions.evaluator import ConditionalEvaluator

# ---------------------------------------------------------------------------
# DSL version
#
# Kept in lockstep with:
#   * the versioned table at the top of docs/reference/CONDITION_GRAMMAR.md,
#   * any future `dsl_version` field added to blueprint metadata.
#
# Bumping this is a breaking change for blueprints that were authored
# against the previous version and are validated against the new one.
# That is why it is deliberately separate from the Python package version
# in pyproject.toml — the two evolve on different schedules.
# ---------------------------------------------------------------------------
__version__: str = "1.0.0"


__all__ = [
    "ConditionError",
    "ConditionalEvaluator",
    "Finding",
    "__version__",
]
