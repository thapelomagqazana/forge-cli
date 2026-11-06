#!/usr/bin/env python3
"""Rebuild the ADR index from the ADR files.

This script scans ``docs/adr/`` for files matching ``NNNN-*.md``, parses
the title, status, and date from each, and regenerates
``docs/adr/README.md`` with a stable, deterministic table.

Design principles
-----------------
1. **The index is generated, never hand-edited.** A hand-edited index
   drifts from reality. A generated index cannot.
2. **Deterministic output.** Running the script twice produces
   byte-identical output. This makes ``git diff`` a reliable signal
   for "the index is stale."
3. **Graceful parsing.** Missing status or date does not crash the
   script; it renders the cell as ``—`` so the table stays useful.
4. **Exit code 1 on drift.** When the script changes the file, it
   exits with code 1 so pre-commit and CI can detect and report drift.

Usage
-----
    python3 scripts/adr/index.py            # rebuild and report
    python3 scripts/adr/index.py --check    # verify only, do not write
    python3 scripts/adr/index.py --quiet    # suppress the summary
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# --- Configuration -----------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
ADR_DIR = REPO_ROOT / "docs" / "adr"
INDEX_FILE = ADR_DIR / "README.md"
FILENAME_PATTERN = re.compile(r"^(\d{4})-(.+)\.md$")
HEADER_PATTERN = re.compile(r"^#\s+(\d{4})\.\s+(.+?)\s*$", re.MULTILINE)
STATUS_PATTERN = re.compile(r"^\s*-\s*\*\*Status:\*\*\s*(.+?)\s*$", re.MULTILINE)
DATE_PATTERN = re.compile(r"^\s*-\s*\*\*Date:\*\*\s*(.+?)\s*$", re.MULTILINE)


# --- Data model --------------------------------------------------------------
class AdrRecord:
    """A single ADR parsed from a markdown file."""

    __slots__ = ("number", "title", "status", "date", "filename")

    def __init__(self, number: str, title: str, status: str, date: str, filename: str) -> None:
        self.number = number
        self.title = title
        self.status = status
        self.date = date
        self.filename = filename


# --- Parsing -----------------------------------------------------------------
def parse_adr(path: Path) -> AdrRecord | None:
    """Parse a single ADR file into an ``AdrRecord``.

    Returns ``None`` if the file does not match the expected format
    (e.g., it is a template or a README). The caller is responsible for
    filtering those out before calling this function.
    """
    match = FILENAME_PATTERN.match(path.name)
    if match is None:
        return None

    number, _slug = match.groups()
    text = path.read_text(encoding="utf-8")

    header_match = HEADER_PATTERN.search(text)
    if header_match is None:
        # The file exists but does not have a top-level heading.
        # Fall back to the slug so the table is still useful.
        title = _slug.replace("-", " ").title()
    else:
        title = header_match.group(2)

    status_match = STATUS_PATTERN.search(text)
    status = status_match.group(1) if status_match else "—"

    date_match = DATE_PATTERN.search(text)
    date = date_match.group(1) if date_match else "—"

    return AdrRecord(number, title, status, date, path.name)


def collect_adrs() -> list[AdrRecord]:
    """Collect and sort all ADRs in ``docs/adr/``.

    Files that do not match ``NNNN-*.md`` are ignored, so ``README.md``
    and ``template.md`` never appear in the index.
    """
    records: list[AdrRecord] = []
    for path in sorted(ADR_DIR.glob("*.md")):
        record = parse_adr(path)
        if record is not None:
            records.append(record)
    # Sort numerically (not lexically) so 0010 comes after 0009.
    records.sort(key=lambda r: int(r.number))
    return records


# --- Rendering ---------------------------------------------------------------
def render_index(records: list[AdrRecord]) -> str:
    """Render the index markdown from the parsed records.

    The output is deterministic: given the same records, it produces
    byte-identical text. This is what allows ``--check`` to detect drift.
    """
    lines = [
        "# Architecture Decision Records",
        "",
        "This directory contains the Architecture Decision Records (ADRs) for Forge CLI.",
        "",
        "An ADR is a short document that captures a significant architectural decision",
        "and its consequences. ADRs are immutable: once accepted, they are not edited.",
        "If a decision changes, a new ADR supersedes the old one.",
        "",
        "## Index",
        "",
        "| Number | Title | Status | Date |",
        "| :--- | :--- | :--- | :--- |",
    ]

    if not records:
        lines.append('| — | _No ADRs yet. Create one with `make adr title="..."`._ | — | — |')
    else:
        for record in records:
            link = f"[{record.number}]({record.filename})"
            lines.append(f"| {link} | {record.title} | {record.status} | {record.date} |")

    lines.extend(
        [
            "",
            "## Creating a new ADR",
            "",
            "```bash",
            'make adr title="Your decision title"',
            "```",
            "",
            "Or, if you do not have `make`:",
            "",
            "```bash",
            'bash scripts/adr/new.sh "Your decision title"',
            "```",
            "",
            "The script creates `docs/adr/NNNN-your-decision-title.md` from",
            "[`template.md`](template.md), pre-filling the number, date, and status.",
            "",
            "## Status values",
            "",
            "| Status | Meaning |",
            "| :--- | :--- |",
            "| **Proposed** | Under discussion. Not yet binding. |",
            "| **Accepted** | Approved and in effect. |",
            "| **Deprecated** | No longer relevant, but not replaced. |",
            "| **Superseded** | Replaced by a newer decision. Link to the new ADR. |",
            "| **Rejected** | Considered and declined. Kept for historical record. |",
            "",
            "## Template",
            "",
            "The ADR template lives in [`template.md`](template.md). It follows",
            "Michael Nygard's format, the de facto standard used by AWS, Google,",
            "Microsoft, and the Rust project.",
            "",
        ]
    )
    return "\n".join(lines)


# --- Command -----------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(
        description="Rebuild the ADR index from the ADR files.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify the index is up to date without writing it. Exits 1 on drift.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress the summary output.",
    )
    args = parser.parse_args()

    if not ADR_DIR.is_dir():
        print(f"Error: ADR directory not found at {ADR_DIR}", file=sys.stderr)
        return 2

    records = collect_adrs()
    new_content = render_index(records)

    if args.check:
        if not INDEX_FILE.exists():
            print(f"Error: {INDEX_FILE} does not exist.", file=sys.stderr)
            print("Run 'python3 scripts/adr/index.py' to generate it.", file=sys.stderr)
            return 1
        current_content = INDEX_FILE.read_text(encoding="utf-8")
        if current_content != new_content:
            print(
                f"Error: {INDEX_FILE} is out of date.",
                file=sys.stderr,
            )
            print(
                "Run 'python3 scripts/adr/index.py' to regenerate it.",
                file=sys.stderr,
            )
            return 1
        if not args.quiet:
            print(f"✅ ADR index is up to date ({len(records)} records).")
        return 0

    # Write path.
    old_content = INDEX_FILE.read_text(encoding="utf-8") if INDEX_FILE.exists() else ""
    INDEX_FILE.write_text(new_content, encoding="utf-8")

    if old_content != new_content:
        if not args.quiet:
            print(f"✅ ADR index regenerated ({len(records)} records).")
        # Exit 1 so pre-commit and CI know a change occurred and must be staged.
        return 1

    if not args.quiet:
        print(f"✅ ADR index already up to date ({len(records)} records).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
