"""Deterministic JSON renderer for `forge validate --json`.

Contract discipline (see ADR-0012, ADR-0013)
--------------------------------------------
This renderer produces the machine-readable form of `ValidateOutput`. It
is a *public contract*, so the following invariants are non-negotiable:

* **Deterministic.** Identical inputs produce byte-identical output.
  Keys are sorted, whitespace is minimised, non-ASCII is preserved. This
  is what makes `tests/contract/test_validate_output_contract.py`'s
  golden file meaningful and what makes a `forge validate --json` diff
  in CI readable.

* **ANSI-free.** No escape sequences, ever. Colour belongs to
  ``ui.emit``; a renderer that injects it would corrupt pipelines that
  pipe our stdout into ``jq``.

* **No trailing newline.** ``ui.emit`` adds exactly one newline. If the
  renderer added one too, every fixture and every `jq`-consumed stream
  would see two. (This is the same rule ``render_json`` follows for
  `forge list` — see `renderers/__init__.py` principle 3.)

* **Pure.** No I/O, no clock, no environment. The DTO carries everything
  the output needs, so a renderer that reaches for the filesystem or
  `datetime` has misunderstood its role.

* **Do not reshape.** The DTO in, the DTO out — field order, field set,
  and null semantics are decided by `ValidateOutput` and its contract
  document. A renderer that renames keys or drops nulls is a second,
  undocumented contract layered on top of the first.

Why a separate file from ``validate_human``?
--------------------------------------------
The two renderers share *nothing* except the DTO they consume:
different audiences (humans vs. machines), different invariants
(stability under reading vs. stability under parsing), different
evolution pressures (wording may drift; JSON may not). Keeping them in
one module would invite a shared helper and a shared helper would
eventually grow a branch that changes one format to fix the other.
Renderers stay single-format, single-file — that rule is enforced by
review, and this file is the concrete form of it for `--json`.
"""

from __future__ import annotations

import json

from forge.cli.schemas.validate_output import ValidateOutput


def render_validate_json(output: ValidateOutput) -> str:
    """Serialise a ``ValidateOutput`` as deterministic JSON.

    Uses ``model_dump(mode="json")`` so optional fields (``line``,
    ``column``, ``blueprint_id``, ``version``) are emitted as JSON
    ``null`` rather than omitted — consumers can rely on the key set
    being identical across every invocation, valid or invalid.
    """
    return json.dumps(
        output.model_dump(mode="json"),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
