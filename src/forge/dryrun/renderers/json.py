"""Deterministic JSON renderer for the dry-run plan.

Pure function: ``GenerationPlan -> str``. No I/O, no ANSI, no clock, no
environment inspection. Same plan in, same bytes out — always.

Why not just ``plan.model_dump_json()``?
----------------------------------------
Pydantic's own serialiser is convenient but not *contracted*:

    * it does not guarantee key ordering — some versions sort, some
      preserve declaration order, and neither is documented as stable
      across minor releases;
    * its whitespace behaviour varies by version and by the presence of
      ``indent``;
    * it does not (as of v2.7) let us express "sort keys at every level,
      no whitespace, keep Unicode literal" in a single call.

The three arguments we pass to ``json.dumps`` below *are* the contract,
written in one place, reviewable in isolation. Version bumps of Pydantic
cannot silently change our wire format.

The four guarantees, each tested
--------------------------------
1. **Deterministic.** Identical plans produce byte-identical strings.
   ``sort_keys=True`` orders every mapping; ``separators=(",", ":")``
   removes whitespace variance; no reliance on Python's dict ordering.

2. **ANSI-free.** Colour is a presentation concern (see
   ``forge.cli.ui.emit``); nothing in this file can introduce an escape
   sequence. ``test_no_ansi_in_json`` asserts it against the full plan
   with all sections populated.

3. **Unicode-faithful.** ``ensure_ascii=False`` keeps non-ASCII
   characters literal in the output. A blueprint or a user's filesystem
   may legitimately contain ``café``, ``文件``, or an emoji path;
   escaping them to ``\\u2026`` produces output that is technically
   correct and practically useless (impossible to grep, impossible to
   read, breaks downstream diffing of plans). The output is UTF-8
   encoded by the caller — ``ui.emit`` writes to ``sys.stdout`` with
   the platform default, which is UTF-8 on every supported platform.

4. **Version-pinned.** The envelope includes ``schema_version`` (a
   field of ``GenerationPlan``, not something added here). Consumers
   that pin to ``1.0.0`` are safe when we add fields; every future
   change is additive by policy (see ``docs/reference/DRY_RUN_PLAN.md``).

What this renderer deliberately does *not* do
---------------------------------------------
* **No envelope wrapper.** Some CLIs wrap output in
  ``{"data": ..., "meta": {...}}`` for extensibility. We do not:
  ``GenerationPlan`` is already a versioned envelope, adding a second
  layer provides no leverage and complicates every consumer. If we
  ever need to add top-level metadata (duration, tool version), it goes
  *into* ``GenerationPlan`` under a new field and a schema_version bump
  — never outside it.

* **No pretty-printing.** JSON has one job here: be a stable machine
  contract. ``indent=2`` triples the byte count, breaks byte-equality
  if a nested object ever changes form, and helps nobody — humans read
  the *human* renderer, not this one. Debugging JSON is what ``jq`` is
  for.

* **No stream/file writing.** The caller (CLI, test, future HTTP
  endpoint) decides where the bytes go. Keeping the renderer pure means
  it is usable from every context without duplication.
"""

from __future__ import annotations

import json

from forge.dryrun.plan import GenerationPlan


def render_json(plan: GenerationPlan) -> str:
    """Render ``plan`` as a deterministic JSON string.

    The exact call below *is* the contract. Do not extract it into a
    shared helper without also moving the guarantees with it — the whole
    point of writing it inline is that a reviewer can see, at a glance,
    that no other behaviour is possible.

    Returns
    -------
    str
        A single-line JSON document ending with a trailing newline is
        **not** added here. Callers that write to a terminal add the
        newline themselves (``ui.emit`` does ``print(text)``); callers
        that stream to a file typically do not want one. Returning the
        bare document makes this renderer usable in both contexts
        without an option flag.
    """
    return json.dumps(
        plan.model_dump(mode="json"),
        sort_keys=True,  # guarantee #1: deterministic key order
        ensure_ascii=False,  # guarantee #3: Unicode-faithful output
        separators=(",", ":"),  # guarantee #1: no whitespace variance
    )
