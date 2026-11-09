"""Human renderer for `forge validate`.

Design principles
-----------------
* **Pure** — takes a `ValidateOutput` DTO, returns a `str`. No I/O, no
  filesystem, no environment reads. That makes it trivially unit-testable
  and snapshot-stable, and lets any caller (CLI, future `forge doctor`,
  embedding tests) render the same report without re-implementing format.

* **ANSI-free** — colour is the job of `ui.emit`/`ui.error` at the very
  last moment. Keeping escape codes out of the string means JSON mode
  cannot accidentally inherit colour, snapshot fixtures need no stripping,
  and piping to a log file stays clean. This mirrors the discipline
  established for `forge list` (ADR-0012).

* **No wrapping** — the file-path of an error (or the hint itself) may be
  long. We never wrap it. A wrapped `hint:` is hard to copy-paste, hard
  to grep, and breaks the "one logical line per fact" property. If a
  caller wants wrapping, they wrap the *rendered string* at the terminal
  layer, not here.

* **Stable layout per error** — three lines, always in this order:

      • <path>  (<source>:<line>:<column>)
          <message>
          hint: <hint>

  The order is (where → what → fix), which is the order a human reads to
  act on a problem. Tests rely on this; the contract doc records it. If
  the order changes, it is a deliberate, reviewed change — not an
  accidental shuffle during a refactor.

* **Summary line first** — the first line is either a single success
  line or an `N validation error(s) in <source>:` line. A user scanning
  terminal output should learn "am I okay?" from the first line without
  counting bullets.

Non-goals
---------
* No colour, no emoji beyond the leading ✓/✗ marker (ASCII-adjacent, safe
  in most terminals and preserved in logs).
* No sorting of errors — the loader already returns a deterministic,
  path-sorted list. Re-sorting here would diverge from `--json` output
  and break the "table and JSON agree" invariant (ADR-0012).
* No truncation of paths, messages, or hints — an error report is
  consumed with intent, not skimmed; full fidelity is more valuable
  than compactness.
"""

from __future__ import annotations

from forge.cli.schemas.validate_output import ValidateError, ValidateOutput


def render_validate_human(output: ValidateOutput) -> str:
    """Render a `ValidateOutput` as a human-readable multi-line report.

    On success returns a single line:
        ✓ Blueprint <id> v<version> is valid.

    On failure returns a summary line followed by one three-line block per
    error, in the loader's stable order.
    """
    if output.valid:
        ident = output.blueprint_id or "<unknown>"
        version = output.version or "?"
        return f"✓ Blueprint {ident} v{version} is valid."

    lines: list[str] = [f"✗ {len(output.errors)} validation error(s) in {output.source}:"]
    for err in output.errors:
        lines.extend(_render_error(err, output.source))
    return "\n".join(lines)


def _render_error(err: ValidateError, source: str) -> list[str]:
    """Render one error as three lines: location, message, hint.

    Kept as a small helper so the block layout is testable in isolation
    and so a future "compact mode" can reuse the pieces without copying
    the format.
    """
    return [
        f"  • {_location(err, source)}",
        f"      {err.message}",
        f"      hint: {err.hint}",
    ]


def _location(err: ValidateError, source: str) -> str:
    """Build the `path (source:line:col)` locator for one error.

    `path` and coordinates are both optional in the DTO:

    * `path == ""` means the error applies to the document as a whole
      (e.g. unreadable YAML, missing manifest). We render `<root>` so the
      line still has a consistent shape and never begins with a bare
      parenthesis.
    * `line`/`column` are `None` when the parser could not locate the
      failure. We drop the coordinates from the locator rather than
      printing `:None:None`, which would be noise.

    The result always ends with the source label so a user scanning a
    batch validate report can tell which file each error came from.
    """
    path = err.path or "<root>"
    if err.line is not None and err.column is not None:
        return f"{path}  ({source}:{err.line}:{err.column})"
    return f"{path}  ({source})"
