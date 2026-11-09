"""Pure ASCII table renderer for `forge list` (and any future table-shaped DTO).

Design contract (see ADR-0012)
------------------------------
* **Pure.** No I/O, no clock, no environment lookups. Given the same
  DTO, produce the same string on every machine, in every locale.
* **ANSI-free.** Colour is applied by `ui.emit`. Keeping escape codes out
  of this module means snapshot tests don't need to strip them, and
  `render_table(...)` output can be safely embedded in logs, emails,
  or a future `--format=txt` export.
* **Order-preserving.** Callers sort once (per ADR-0012); this module
  renders in the order it receives. Re-sorting here would let the table
  and JSON outputs drift silently — the exact class of bug the DTO +
  renderer split was introduced to prevent.
* **Single-pass width calculation.** Column widths are derived from the
  header and the *first* rendered value per column, capped at sane
  maxima, and never wrap. Long values are truncated with a single `…`
  glyph so the ID column stays copy-pasteable.

Terminal-safety notes
---------------------
* We measure width in **codepoints** (`len`), not display columns. The
  bundled official blueprints are ASCII; user-supplied names with wide
  CJK glyphs may misalign by a few cells. Full grapheme/EAW handling is
  deferred to a future `--format=wide` mode rather than complicating the
  hot path and every snapshot in the repo.
* We never assume the terminal supports a specific width — the output
  is meant to be diff-able in CI and readable when piped to `less`.
"""

from __future__ import annotations

from collections.abc import Sequence

from forge.cli.schemas.list_output import ListOutputItem

# ---------------------------------------------------------------------------
# Column configuration
#
# Centralised so that future commands (e.g. `forge status`) can reuse the
# renderer by supplying their own column spec rather than growing an
# ever-widening `if command == "list"` branch here.
# ---------------------------------------------------------------------------

ColumnSpec = tuple[str, int]  # (header, max_codepoints)

_LIST_COLUMNS: tuple[ColumnSpec, ...] = (
    ("ID", 32),
    ("NAME", 32),
    ("VERSION", 10),
    ("CATEGORY", 12),
    ("STATUS", 8),
)

# Ellipsis is a single codepoint so truncation math stays trivial.
_ELLIPSIS = "…"
_COLUMN_SEP = "  "  # two spaces: readable, and a clean field separator


def _truncate(text: str, limit: int) -> str:
    """Truncate to `limit` codepoints, reserving one for the ellipsis.

    A `limit` of 0 or 1 degenerates gracefully — the header is guaranteed
    to fit because every cap below is at least the header length, but we
    still behave sanely if a future caller passes a tiny cap.
    """
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit == 1:
        return _ELLIPSIS
    return text[: limit - 1] + _ELLIPSIS


def _cells(item: ListOutputItem) -> tuple[str, ...]:
    """Extract the renderable cells for one row.

    Kept separate so the renderer can be reused for other DTOs by
    supplying a different `cells` callable without touching the layout
    code below.
    """
    return (
        item.id,
        item.name,
        item.version,
        item.category.value,
        item.status.value,
    )


def _column_widths(
    columns: Sequence[ColumnSpec],
    rows: Sequence[tuple[str, ...]],
) -> tuple[int, ...]:
    """Compute column widths: header width vs. widest value, capped.

    The cap is a *contract*: it keeps the table readable on 80-column
    terminals even when a user supplies a 200-character project name.
    Truncation is preferred over wrapping because a wrapped ID cannot be
    copy-pasted into `forge create <id>`, which is the primary use of
    this column.
    """
    widths: list[int] = []
    for index, (header, cap) in enumerate(columns):
        widest_value = max((len(row[index]) for row in rows), default=0)
        widths.append(min(cap, max(len(header), widest_value)))
    return tuple(widths)


def _format_row(cells: Sequence[str], widths: Sequence[int]) -> str:
    """Left-justify each cell to its column width and join.

    `rstrip()` removes the trailing padding after the last column so the
    rendered line has no trailing whitespace — a small thing that matters
    for `diff`-based snapshot tests.
    """
    padded = (
        _truncate(cell, widths[index]).ljust(widths[index]) for index, cell in enumerate(cells)
    )
    return _COLUMN_SEP.join(padded).rstrip()


def render_table(items: Sequence[ListOutputItem]) -> str:
    """Render `items` as a plain-text table. Callers supply an ordered list.

    The renderer is deliberately tolerant of the empty list — callers that
    want a special empty-state message (as `forge list` does) should check
    for emptiness *before* calling this function, per ADR-0012. Returning
    just the header (rather than raising) keeps the renderer usable as a
    building block in composite outputs.
    """
    headers = tuple(header for header, _ in _LIST_COLUMNS)
    rows = tuple(_cells(item) for item in items)
    widths = _column_widths(_LIST_COLUMNS, rows)

    header_line = _format_row(headers, widths)
    separator = _COLUMN_SEP.join("-" * width for width in widths)
    body_lines = [_format_row(row, widths) for row in rows]

    return "\n".join((header_line, separator, *body_lines))
