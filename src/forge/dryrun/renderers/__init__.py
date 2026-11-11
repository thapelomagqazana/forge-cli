"""Renderers for the dry-run plan.

A renderer is a pure function from a ``GenerationPlan`` to a ``str``.
Nothing else. No I/O, no ANSI, no environment inspection, no clock, no
sorting — the DTO arrives already ordered (see
``DryRunService.plan``), and colour is the caller's concern
(``forge.cli.ui.emit``).

That rule is what makes every renderer in this codebase:

    * **snapshot-testable** — same DTO in, same bytes out, forever;
    * **composable** — the CLI, a future ``forge plan`` subcommand, a
      test that asserts a golden file, and a future HTTP endpoint all
      call the same function;
    * **reviewable** — a reviewer reads one file and knows the entire
      output contract, because there is nothing hidden behind an
      environment check or a terminal-width probe.

Why this façade exists
----------------------
Downstream code imports from here, never from ``human.py`` or
``json.py`` directly. That gives us:

    1. **Refactor freedom.** We can rename ``human.py`` to ``text.py``,
       split ``json.py`` into a shared ``forge.cli.renderers`` module
       (following ADR-0012's direction for other commands), or add a
       future ``markdown.py`` — as long as this file re-exports the
       existing names, no caller changes.

    2. **One obvious import.** CLI code reads
       ``from forge.dryrun.renderers import render_human, render_json``
       and immediately understands the dependency is bounded. If they
       see ``from forge.dryrun.renderers.human import _header`` they
       know something has gone wrong.

    3. **Contract clarity.** The names in ``__all__`` are the public
       surface. Everything else is internal, so adding a private helper
       to one renderer is not a breaking change to any consumer.

Why a JSON renderer is included even though the DTO has ``model_dump``
-----------------------------------------------------------------------
``GenerationPlan.model_dump(mode="json")`` produces a Python ``dict``,
not a string. Consumers that want a JSON *string* would otherwise each
have to write ``json.dumps(plan.model_dump(mode="json"), sort_keys=True,
ensure_ascii=False, separators=(",", ":"))`` — four load-bearing
arguments they would eventually get subtly wrong (forget
``ensure_ascii=False``, use a pretty print flag, use the wrong
separator). The renderer pins those four arguments in one file, next
to their tests (see ``test_json_contract.py``). It is a *contract
enforcement device*, not a convenience wrapper.

What is **not** exported
------------------------
* **No DTO re-exports.** ``PlannedFileDTO``, ``GenerationPlan``, and
  friends live in ``forge.dryrun`` (the module façade), not here.
  Renderers consume DTOs; exporting them from the renderer package
  would blur which module owns the type.

* **No ``render_markdown`` or ``render_yaml`` yet.** A renderer earns
  its way into this file when a real consumer exists. Adding an unused
  renderer now would (a) invent a contract before anyone needs it, and
  (b) suggest that the format is stable when it has no tests and no
  users. The module is designed to grow one function at a time.
"""

from __future__ import annotations

from forge.dryrun.renderers.human import render_human
from forge.dryrun.renderers.json import render_json

__all__ = [
    # Human — for terminals
    "render_human",
    # JSON — for automation, CI, and future HTTP surfaces
    "render_json",
]
