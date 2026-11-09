"""Output renderers — pure functions from DTO to string.

What a renderer is
------------------
A renderer is a *pure function* with a single job: take a validated
output DTO (a Pydantic model from ``forge.cli.schemas``) and return
the string that represents it in one specific format. Nothing more.

That definition is deliberately narrow, and the narrowness is the
point. Renderers exist so that:

* **Formatting is testable in isolation.** ``render_table(items)``
  can be unit-tested without touching stdout, without mocking a
  console, and without a terminal. Every test in
  ``tests/unit/cli/test_list_table.py`` proves this: the renderer
  tests don't use ``capsys`` at all.

* **Adding a format is additive.** ``--format=yaml``, ``--format=csv``,
  ``--format=markdown`` each becomes one new file in this package and
  one new ``if`` in the command. No existing renderer changes, no
  consumer of the current formats is at risk.

* **The command stays a composition.** The command body collects data,
  projects it into a DTO, and picks a renderer — nothing else. It
  never formats. The moment a command body starts building strings,
  it has become a second renderer that no test suite exercises
  through the same seam as the first.

The renderer contract
---------------------
Every renderer in this package MUST:

1. **Be pure.** No I/O, no clock, no environment lookups, no global
   state. Given the same DTO, produce the same string on every
   machine, in every locale, at every time of day. (This is what
   makes snapshot tests meaningful; a renderer that reads the clock
   produces snapshots that fail in the CI timezone.)

2. **Return ANSI-free text.** Colour is applied by ``ui.emit`` (see
   ``forge.cli.ui``'s design principle 7). A renderer that injects
   escape codes breaks JSON validity, pollutes golden fixtures, and
   forces every downstream test to strip codes it did not ask for.

3. **Not append a trailing newline.** ``ui.emit`` owns line
   termination, so every payload ends with exactly one newline
   regardless of which renderer produced it. A renderer that appends
   its own newline produces two, breaking the byte-for-byte fixture
   contract. (JSON mode, in particular, must not end with a newline
   inside the *string* — the caller will add one.)

4. **Preserve input order.** Sorting and filtering belong to the
   command, once, before the DTO is built (per ADR-0012). A renderer
   that re-sorts silently disagrees with every other renderer that
   receives the same DTO — the exact class of bug the DTO + renderer
   split exists to prevent.

5. **Accept the DTO, not a dict.** The DTO is the contract
   (``forge.cli.schemas``); accepting a raw dict would let an
   accidental shape reach the output. If a renderer needs to be
   generic across DTOs (as ``render_json`` is), the parameter is
   typed as ``BaseModel`` — the constraint is "validated Pydantic
   model", not "raw mapping".

6. **Not import ``forge.cli.ui``.** Import direction matters:
   ``ui`` depends on nothing in this package, and this package must
   not depend on ``ui``. A renderer that imports ``ui`` has almost
   certainly started printing, which violates principle 1.

Public surface
--------------
The package re-exports exactly the renderers that exist today,
grouped by the command whose DTO each one consumes. Grouping keeps
the surface honest as the catalogue grows: a reader can tell at a
glance which command each format serves, and a new command's
renderers slot into a new, obviously-named group rather than
lengthening a flat list.

* ``forge list``
    * ``render_table``        — plain ASCII table (human terminals).
    * ``render_json``         — deterministic JSON (automation).
                                Generic over any validated Pydantic
                                model, so future commands reuse it
                                without a new renderer.

* ``forge validate``
    * ``render_validate_human`` — multi-line error report (terminals).
    * ``render_validate_json``  — deterministic JSON envelope
                                  (see ``CLI_VALIDATE_OUTPUT.md``).

Adding a renderer is intentionally a small change here (one import,
one ``__all__`` entry). The deliberate friction is that adding it
*also* requires:

* a fixture set under ``tests/fixtures/`` (contract or snapshot per
  its tier — see the fixtures README),
* a document in ``docs/reference/`` if it is machine-readable (JSON
  is a *contract*; markdown is not),
* a test file that asserts the format-specific properties (the shared
  shape/whitespace/ordering invariants live in per-renderer test
  files, not here).

A renderer that ships without those three is a renderer no one can
safely change later, which is the definition of technical debt this
package exists to prevent.

What this module is NOT
-----------------------
* **Not a factory.** There is no ``get_renderer(format)`` here. The
  command picks the renderer directly by name — ``render_table`` or
  ``render_json``, ``render_validate_human`` or
  ``render_validate_json`` — because the set of formats per command
  is small and a lookup table would obscure which formats are
  supported at each call site. When a command grows past two formats,
  the factory can be added *here* without touching callers, which is
  the correct time to introduce the indirection.

* **Not a place for cross-cutting logic.** Colour, indentation,
  pagination, and terminal-width detection are *not* the renderer's
  concern. Terminal-width detection in particular is a common
  temptation — resist it. A renderer that adapts to the terminal
  produces different strings on different machines, which makes
  snapshot tests impossible and lets two users see two tables for
  the same data. The command can adapt *around* a renderer if
  needed (e.g. choose a wide or narrow renderer based on width);
  the renderer itself must not vary.

* **Not a schema module.** The DTOs live in ``forge.cli.schemas``.
  Importing a DTO here is fine; *defining* one here would blur the
  contract/implementation line that this package is built on.

Layering recap
--------------
    cli/commands/list.py       -> collects data, sorts once, picks renderer
    cli/commands/validate.py   -> loads blueprint, picks renderer, returns exit code
    cli/schemas/list_output.py     -> the list DTO (contract)
    cli/schemas/validate_output.py -> the validate DTO (contract)
    cli/renderers/table.py            -> DTO -> string
    cli/renderers/json.py             -> DTO -> string (generic JSON)
    cli/renderers/validate_human.py   -> DTO -> string
    cli/renderers/validate_json.py    -> DTO -> string
    cli/ui.py                         -> writes the string to stdout (+ newline, colour)

The dependency arrow points *down* the list, never up: renderers do
not import commands, ``ui`` does not import renderers, and
``schemas`` imports nothing from ``forge.cli`` at all. That is what
makes the DTOs reusable from non-CLI entry points (a future HTTP
catalog endpoint, an editor integration) without pulling the CLI in
with them.
"""

from __future__ import annotations

# --- `forge list` renderers ------------------------------------------------
from forge.cli.renderers.json import render_json
from forge.cli.renderers.table import render_table

# --- `forge validate` renderers -------------------------------------------
from forge.cli.renderers.validate_human import render_validate_human
from forge.cli.renderers.validate_json import render_validate_json

__all__ = [
    # `forge list` — table format for human terminals.
    "render_table",
    # `forge list` — generic deterministic JSON for automation.
    # (Signature accepts any validated Pydantic model; new commands with
    # a flat envelope reuse this without a new renderer.)
    "render_json",
    # `forge validate` — multi-line error report for human terminals.
    # Prints every error from the loader with path, message, and hint.
    "render_validate_human",
    # `forge validate` — deterministic JSON envelope for CI.
    # Shape is locked by CLI_VALIDATE_OUTPUT.md and its contract test.
    "render_validate_json",
]
