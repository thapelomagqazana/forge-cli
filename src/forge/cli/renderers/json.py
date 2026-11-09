r"""Stable JSON renderer for CLI output DTOs.

Design contract (see ADR-0012)
------------------------------
* **Pure.** No I/O, no clock, no environment lookups. Given the same DTO,
  produce byte-identical JSON on every machine, in every locale.
* **Deterministic.** Two runs with the same input yield identical bytes.
  This is what lets snapshot tests exist without fighting whitespace,
  and what makes `diff` a usable debugging tool for automation authors.
* **ANSI-free.** Escape codes never enter the data path; colour, if any,
  is applied by `ui.emit` at the very end — and for JSON we never apply
  any. `JSON` is for machines; machines don't render escape codes.
* **Contract-first.** The renderer serialises a versioned DTO
  (`ListOutput`, future `CreateOutput`, etc.), never a raw dict. Field
  names, types, and constraints are enforced by the model *before* this
  function is called, so the renderer's job is exactly one thing:
  turn the validated model into a deterministic byte string.


-------------------------------------------------
Docstrings are string literals, and Python's parser applies escape
processing to non-raw string literals — including docstrings. Writing
the *literal text* `\uXXXX` inside a normal docstring therefore raises
``SyntaxError: (unicode error) 'unicodeescape' codec can't decode bytes
... truncated \uXXXX escape`` at *import* time, not at test time.

That error is confusing in practice because it surfaces first through
whatever tool happens to parse the file — in this project, `coverage`
during pytest collection, which reports it as
``CoverageWarning: Couldn't parse Python file ... as Python source``
and aborts the whole test session with an INTERNALERROR. No test
actually runs. The traceback points at `line 1` (the docstring) rather
than the offending sequence, so the fix is not obvious from the error
alone.

A raw docstring tells Python "do not interpret escapes here". Since
this module's job is documenting byte-level output guarantees,
mentioning literal sequences such as `\uXXXX`, `\n`, and `\t` is
expected — the docstring must be able to do so without tripping the
parser. The `r` prefix is load-bearing; do not "clean it up".

The same fix applies to any module docstring that quotes escape
sequences, regexes, or platform paths (``C:\Users\...``).

Stability guarantees relied on by automation
--------------------------------------------
1. **Key order** — alphabetic (`sort_keys=True`). Not meaningful to the
   JSON spec, but meaningful to `diff`, code review, and eyeball tests.
2. **Whitespace** — none beyond `,` and `:` (`separators=(",", ":")`).
   No trailing newline is added; callers decide (see `ui.emit`).
3. **Encoding** — UTF-8, with `ensure_ascii=False` so non-ASCII names
   round-trip faithfully instead of being turned into `\uXXXX` sequences
   that then need re-decoding.
4. **Enum representation** — the DTO's string enums serialise as their
   string values (via `model_dump(mode="json")`), not as `{"value": ...}`
   wrappers. This is a Pydantic v2 default; documented here because it
   is part of the contract automation depends on.

What this renderer is *not*
---------------------------
* Not a pretty-printer. `--json` output is intended for pipes, not for
  reading; `jq` exists. A future `--json-pretty` would be a *separate*
  renderer, not a flag threaded through this one — keeping this module
  honest about its contract.
* Not an error serialiser. Error payloads have their own shape
  (`ValidationError.format()`); mixing them into the success envelope
  would make the contract conditional on runtime outcome, which is a
  well-known way to make automation brittle.
"""

from __future__ import annotations

import json

from pydantic import BaseModel


def render_json(output: BaseModel) -> str:
    """Serialise a validated DTO to a deterministic JSON string.

    Accepting `BaseModel` (not `ListOutput` specifically) is deliberate:
    the renderer has no opinion about which command produced the payload
    — that is what makes it reusable for `forge status --json`,
    `forge create --json`, etc., without growing a `if command == …`
    branch here. The type hint documents the *only* thing the renderer
    requires of its input: that it is a Pydantic model whose
    `model_dump(mode="json")` produces a JSON-serialisable structure.

    Parameters
    ----------
    output:
        A validated Pydantic model. The caller is responsible for having
        already constructed it (so field constraints, `extra="forbid"`,
        and any custom validators have run). Passing an unvalidated dict
        is a type error — and, more importantly, would let a shape the
        contract disallows reach automation.

    Returns
    -------
    A single-line JSON string, keys sorted, no whitespace variance, no
    trailing newline. The caller decides where to write it and whether
    to append a newline (see `ui.emit`).
    """
    # `model_dump(mode="json")` is the Pydantic v2 idiom for "produce a
    # plain Python structure suitable for `json.dumps`". In particular:
    #   * enums become their string values,
    #   * datetimes/Paths/UUIDs become strings,
    #   * nested models recurse into dicts,
    #   * `bytes` raises (which is correct — no DTO should carry bytes
    #     to a JSON surface).
    # Using `mode="python"` here would leave enums as enum instances,
    # which `json.dumps` cannot serialise; using `.dict()` (the v1
    # alias) is removed in v2. The explicit mode is the contract.
    payload = output.model_dump(mode="json")

    return json.dumps(
        payload,
        # Keys sorted for byte-level determinism. This is the single
        # most important flag for snapshot tests: without it, the
        # serialised order follows field declaration order, which is
        # stable *today* but becomes a hidden coupling the moment
        # anyone reorders a field or introduces a mixin.
        sort_keys=True,
        # No whitespace beyond the two mandatory separators. This is
        # what makes `render_json(x) == render_json(x)` a useful
        # assertion in tests and what makes piping into `jq` cheap.
        separators=(",", ":"),
        # Preserve UTF-8. Turning non-ASCII into `\uXXXX` would still
        # round-trip, but it makes the raw output unreadable to humans
        # eyeballing a diff and doubles the byte length for CJK names.
        ensure_ascii=False,
    )
