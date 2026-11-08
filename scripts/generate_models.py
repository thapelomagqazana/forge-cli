#!/usr/bin/env python3
"""Regenerate `forge.models.blueprint` from the canonical JSON Schema.

This script is the **single** generator for ``src/forge/models/blueprint.py``.
It lives in ``scripts/`` — outside the ``forge`` package on purpose.

Why a script and not a module inside the package
------------------------------------------------
A generator that lives at ``forge.models._generator`` cannot run when
``forge.models.blueprint`` is broken: invoking ``python -m
forge.models._generator`` triggers ``import forge.models``, whose
``__init__.py`` imports ``forge.models.blueprint`` — the very file the
generator exists to repair. That is a bootstrap deadlock: the tool that
fixes the file cannot be invoked when the file needs fixing.

Putting the generator in ``scripts/`` breaks the deadlock by
construction. The script imports only the standard library; it never
imports ``forge``. It is runnable from any state of the package,
including the state a contributor will be in when something has gone
wrong and they need to regenerate.

What it does
------------
1. Invokes ``datamodel-code-generator`` to produce the raw model file
   from ``src/forge/schemas/blueprint.schema.json``.
2. Reads the *currently committed* ``blueprint.py`` to extract every
   ``# region: hand-tuned`` block, anchored by the class name the block
   appears inside.
3. Re-injects those blocks into the freshly generated body, at the end
   of each owning class's body.
4. Composes the result with the file header and writes it back.

Invariants this script enforces
-------------------------------
* **No hand edit outside a hand-tuned region survives.** The generator's
  output is authoritative; anything the human added elsewhere is erased.
  That is the point — it is what makes ``make models-check`` a meaningful
  CI signal (ADR-0008).

* **Hand-tuned regions survive regeneration** as long as their owning
  class still exists in the regenerated output. If a block's anchor is
  missing — because the schema renamed a class, or the generator was
  invoked with different flags — the script fails loudly rather than
  silently dropping behaviour. A silent drop is a bug that only surfaces
  at runtime; a loud failure is a bug that surfaces immediately.

* **The schema is the single source of truth.** Any class name or field
  constraint that needs to be authoritative belongs in the schema (as a
  ``$defs`` entry with a ``title``, or an inline definition with a
  ``title``), not in this script. The script cannot inject names that
  the schema does not declare.

* **The committed file is the seed.** On the *first* run against a fresh
  checkout, the hand-tuned regions must already exist in
  ``blueprint.py``; this script preserves them, it does not invent them.
  That is why the hand-written baseline must be committed before the
  generator ever runs on a real change.

Exit codes (for ``make`` and CI, not the user-facing CLI contract)
------------------------------------------------------------------
    0  Success: file regenerated (or, with ``--check``, in sync).
    1  Check failed: regeneration would change the committed file.
    2  Generator error: ``datamodel-code-generator`` failed or the
       hand-tuned anchors are missing.
    3  Configuration error: the schema file is missing.

The exit codes are documented here because this script is a developer
tool, not part of the ``forge`` command-line interface. They are for
``make`` and CI, not for ``$?`` branching by end users.

Invocation
----------
    python scripts/generate_models.py            # regenerate in place
    python scripts/generate_models.py --check    # verify, no write

Both are also available through the ``Makefile``:

    make models          # → python scripts/generate_models.py
    make models-check    # → python scripts/generate_models.py --check
"""

from __future__ import annotations

import argparse
import difflib
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Final

# ---------------------------------------------------------------------------
# Repository-relative paths.
#
# Resolved from ``__file__`` so the script works regardless of the
# caller's CWD — ``make`` runs from the repo root, but a contributor
# running ``python scripts/generate_models.py`` from ``docs/`` must also
# work. Hard-coding paths relative to ``Path.cwd()`` is the classic
# source of "works on my machine" bugs in dev-tooling.
#
# ``parents[1]`` because this file is at ``scripts/generate_models.py``;
# one ``parent`` walks to the repo root. If the script ever moves, this
# is the only line that needs to change.
# ---------------------------------------------------------------------------
_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
_SCHEMA_PATH: Final[Path] = _REPO_ROOT / "src" / "forge" / "schemas" / "blueprint.schema.json"
_OUTPUT_PATH: Final[Path] = _REPO_ROOT / "src" / "forge" / "models" / "blueprint.py"

# The header we prepend to every generated file. Defined here, in one
# place; ``--check`` compares the whole file including the header, so
# drift in the header text is caught the same way any other drift is.
_FILE_HEADER: Final[str] = (
    "# AUTO-GENERATED FROM src/forge/schemas/blueprint.schema.json\n"
    "# Regenerate with `make models` (see scripts/generate_models.py).\n"
    "# Hand edits are permitted ONLY inside `# region: hand-tuned` blocks;\n"
    "# the parity test guards everything else.\n"
)

# The three generator flags that determine the class-name output shape.
# Kept as named constants so a reader can see, without scrolling through
# the argument list, what makes the generator produce the names the
# façade expects:
#
#   * ``--use-title-as-name`` makes the schema's ``title`` fields
#     authoritative over the generator's derived names. This is why the
#     schema carries ``"title": "Blueprint"``, ``"title": "FileEntry"``,
#     etc. — the titles are the class names.
#   * ``--class-name Blueprint`` names the root model when no title is
#     present. It is a fallback, not an override: if the schema's root
#     ``title`` is set, the title wins.
#   * ``--collapse-root-models`` prevents the generator from emitting a
#     ``RootModel`` wrapper around the root object.
#
# When one of these changes, the change is a deliberate act that
# requires updating the schema's ``title`` fields (for the first) or
# re-committing the generated file (for the others). Keeping them
# named here makes the deliberate-act requirement visible.
_GENERATOR_ARGS: Final[tuple[str, ...]] = (
    "-m",
    "datamodel_code_generator",
    "--input-file-type",
    "jsonschema",
    "--output-model-type",
    "pydantic_v2.BaseModel",
    "--target-python-version",
    "3.11",
    "--use-standard-collections",
    "--use-union-operator",
    "--use-schema-description",
    "--use-field-description",
    "--use-title-as-name",
    "--field-constraints",
    "--use-annotated",
    "--use-double-quotes",
    "--strict-nullable",
    "--disable-timestamp",
    "--class-name",
    "Blueprint",
    "--collapse-root-models",
)

# Markers delimiting a hand-tuned block. The opening marker must be on
# its own line (modulo leading whitespace) so that prose mentions of the
# phrase — e.g. in this script's own docstring, or in a class docstring —
# are not mistaken for block markers. The closing marker is a bare
# ``# endregion`` so a nesting mistake is caught immediately.
_HAND_TUNED_START: Final[re.Pattern[str]] = re.compile(r"^[ \t]*# region: hand-tuned[ \t]*$")
_HAND_TUNED_END: Final[re.Pattern[str]] = re.compile(r"^[ \t]*# endregion[ \t]*$")

# Matches ``class Foo(...):`` at column zero. Used to attach a
# hand-tuned block to the class it appears inside. We deliberately look
# only at column-zero ``class`` statements: nested classes are not a
# pattern this project uses, and supporting them would require a real
# parser for no benefit.
_CLASS_DEF: Final[re.Pattern[str]] = re.compile(r"^class\s+(\w+)\b")


# ---------------------------------------------------------------------------
# Line predicates used by the class-body detector.
#
# These are extracted into named functions rather than inlined as a
# compound boolean, for two reasons:
#
#   1. **They answer different questions.** ``_is_top_level`` asks a
#      structural question about Python syntax ("does this line start
#      at column zero?"). ``_is_top_level_comment`` asks a semantic
#      question ("does this top-level line close a class body, or is it
#      just commentary about the class?"). Combining them into a single
#      ``and`` hides that distinction and makes future edits risky —
#      someone who needs to extend the carve-out (e.g. to allow module-
#      level docstrings before the next definition) would have to
#      re-derive the whole condition from scratch.
#
#   2. **They are individually testable.** The tests in
#      ``tests/unit/models/test_generated_in_sync.py`` exercise them
#      directly, which catches regressions like "blank lines are
#      treated as class-body terminators" — a bug that is otherwise
#      invisible until a class whose body ends with a blank line gets
#      its hand-tuned block reinserted in the wrong place.
# ---------------------------------------------------------------------------
def _is_top_level(line: str) -> bool:
    """Return True if ``line`` starts at column zero (i.e. top-level code).

    A "top-level" line is what ends a class body: it is a line whose
    first character is not whitespace. Blank lines are not "top-level"
    — they do not end a class body — and neither are lines consisting
    only of whitespace. Both cases are covered by the ``bool(line)``
    guard and the ``not line[0].isspace()`` check together.
    """
    return bool(line) and not line[0].isspace()


def _is_top_level_comment(line: str) -> bool:
    """Return True if ``line`` is a comment at column zero.

    A comment at column zero is legal Python and does *not* close a
    class body — it is commentary *about* the class that follows, not a
    new top-level definition. That is the one case where a column-zero
    line does not terminate the current class body. This predicate is
    the carve-out; ``_is_top_level`` is the general rule.
    """
    return line.lstrip().startswith("#")


# ---------------------------------------------------------------------------
# Hand-tuned block extraction and reinsertion.
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class HandTunedBlock:
    """One ``# region: hand-tuned`` block, anchored to its owning class.

    Attributes:
        class_name: The class the block appears inside. Used as the
            reinsertion anchor: the regenerated file must contain a
            ``class <class_name>`` statement, and the block is appended
            to the end of that class's body.
        body: The verbatim text between the markers, including both
            markers themselves but *not* surrounding blank lines. Kept
            verbatim so whitespace and internal comments survive.
    """

    class_name: str
    body: str


def _extract_hand_tuned(source: str) -> list[HandTunedBlock]:
    """Parse every hand-tuned block from ``source``, in file order.

    The algorithm:

    1. Walk lines, tracking the most recent column-zero ``class`` name.
    2. On a ``# region: hand-tuned`` line, remember the current class
       and start buffering.
    3. On the matching ``# endregion``, close the block and record it.
    4. If the file ends inside a block, raise — that means the source
       is corrupt and we must not silently drop behaviour.

    Nesting is rejected: the schema-driven generator cannot produce
    nested regions, and a human-written nested region is almost
    certainly a mistake. Failing loudly beats guessing.

    Blocks outside any class are also rejected — the reinsertion logic
    has no anchor to attach them to, so accepting them here would just
    move the failure to a harder-to-diagnose place.

    This function is the load-bearing half of the preservation
    mechanism. It is paired with ``_reinject_hand_tuned``; between the
    two, a block that exists in the current file appears in the
    regenerated file at the end of its owning class's body.
    """
    blocks: list[HandTunedBlock] = []
    current_class: str | None = None
    buffer: list[str] = []
    in_block = False

    for lineno, line in enumerate(source.splitlines(), start=1):
        # Track the most recent column-zero ``class`` definition. This
        # is what attaches a block to the class it appears inside. A
        # ``class`` line inside a block is ignored (the ``not in_block``
        # guard), because a hand-tuned block may legitimately contain
        # nested class definitions and we do not want to re-anchor the
        # block to a class that only exists inside it.
        class_match = _CLASS_DEF.match(line)
        if class_match and not in_block:
            current_class = class_match.group(1)

        if _HAND_TUNED_START.match(line):
            if in_block:
                raise RuntimeError(
                    f"nested `# region: hand-tuned` at line {lineno}; " "nesting is not supported"
                )
            if current_class is None:
                raise RuntimeError(
                    f"`# region: hand-tuned` at line {lineno} is outside any class; "
                    "blocks must live inside a class body to have an anchor"
                )
            in_block = True
            buffer = [line]
            continue

        if in_block:
            buffer.append(line)
            if _HAND_TUNED_END.match(line):
                blocks.append(
                    HandTunedBlock(
                        class_name=current_class,
                        body="\n".join(buffer),
                    )
                )
                in_block = False
                buffer = []

    if in_block:
        raise RuntimeError("unterminated `# region: hand-tuned` block at end of file")

    return blocks


def _find_class_body_extent(lines: list[str], class_name: str) -> tuple[int, int]:
    """Return ``(start, end)`` line indices for the body of ``class_name``.

    ``start`` is the index of the first line *after* the class header;
    ``end`` is the index of the first line that ends the class body —
    that is, a line at column zero that is not a comment. The block is
    inserted at ``end``, before any trailing blank lines, so the class
    body stays visually attached.

    Raises ``RuntimeError`` if the class is not found — this is the
    anchor-missing case that a rename in the generator would produce,
    and it must fail loudly (see module docstring, exit code 2).

    The anchor is looked up by class name, not by line number, on
    purpose. Line-number anchoring was the obvious first design and it
    fails the moment any field order changes: the generator re-emits
    fields in schema order, so any change to the schema shifts lines
    and silently misattaches blocks. Class-name anchoring survives field
    reordering, docstring changes, and even the insertion of new
    classes — the only thing it rejects is the one case where rejection
    is correct: the class was renamed (a real behavioural change that
    needs a human in the loop).
    """
    class_re = re.compile(rf"^class\s+{re.escape(class_name)}\b")
    for i, line in enumerate(lines):
        if class_re.match(line):
            # Find the end of this class's body: the first line at
            # column zero that is a *definition* rather than a comment.
            # The two predicates are named because they answer two
            # different questions; see their docstrings above.
            for j in range(i + 1, len(lines)):
                candidate = lines[j]
                if _is_top_level(candidate) and not _is_top_level_comment(candidate):
                    return i + 1, j
            return i + 1, len(lines)
    raise RuntimeError(
        f"cannot reinsert hand-tuned block: class '{class_name}' not found "
        "in regenerated output (was it renamed in the schema, or were the "
        "generator flags changed?)"
    )


def _reinject_hand_tuned(generated: str, blocks: list[HandTunedBlock]) -> str:
    """Insert each ``blocks`` item at the end of its owning class's body.

    Multiple blocks for the same class are inserted in declaration order,
    separated by a single blank line, so the result reads as though a
    human had written the class in one pass. Insertion works *backwards*
    by class position so earlier insertions do not shift the line
    numbers of later ones — the classic off-by-N bug when mutating a
    list while computing indices from the pre-mutation state.
    """
    if not blocks:
        return generated

    lines = generated.splitlines()

    # Group by class, preserving per-class declaration order. Dict
    # preserves insertion order in Python 3.7+, which is what we want:
    # the first block for a class appears first in the output.
    grouped: dict[str, list[str]] = {}
    for block in blocks:
        grouped.setdefault(block.class_name, []).append(block.body)

    # Resolve extents *before* mutating. Once we start inserting, line
    # numbers shift; computing them all up front (and inserting from
    # the last class backwards) keeps every index valid.
    extents = [(class_name, *_find_class_body_extent(lines, class_name)) for class_name in grouped]
    # Sort by insertion point, descending, so earlier inserts do not
    # invalidate later ones.
    extents.sort(key=lambda t: t[2], reverse=True)

    for class_name, _start, end in extents:
        bodies = grouped[class_name]
        # Each block gets a blank line before it for readability; the
        # blank line after comes from the class's own trailing structure
        # (which we are inserting *before*), so we do not add one.
        insertion = ["", *bodies]
        lines[end:end] = insertion

    return "\n".join(lines) + ("\n" if generated.endswith("\n") else "")


# ---------------------------------------------------------------------------
# Generator invocation.
# ---------------------------------------------------------------------------
def _run_generator(schema_path: Path, output_path: Path) -> None:
    """Invoke ``datamodel-code-generator`` to produce the raw model file.

    Runs the generator as a subprocess rather than importing it as a
    library for a specific reason: the generator's public API is not
    stable across minor versions, and the CLI is the documented
    interface. Importing the library to avoid a subprocess would couple
    this script to internals that have broken between 0.25.x patch
    releases. The subprocess boundary is cheap (one per regeneration)
    and version-proof.

    Raises ``RuntimeError`` if the generator exits non-zero, with its
    captured stderr attached — "it failed" without the tool's own
    diagnostic is the worst possible developer experience.

    On the ``# noqa: S603`` below
    ------------------------------
    Bandit's S603 rule fires on any ``subprocess`` call whose argument
    list is not a fixed literal, on the theory that a computed argument
    might carry attacker-controlled input. That theory does not apply
    here, and the annotation records why:

    * ``sys.executable`` is the interpreter currently running this
      script — not a caller-supplied path.
    * The module name (``"datamodel_code_generator"``) and every flag
      in ``_GENERATOR_ARGS`` are compile-time string literals.
    * The only computed arguments are ``schema_path`` and
      ``output_path``, both derived from ``_REPO_ROOT`` (itself derived
      from ``__file__``) and neither sourced from environment variables,
      argv, or any user-controlled input. A malicious schema *content*
      cannot influence the *path* at which the generator is invoked.

    The correct response is therefore not to change the code but to
    document the deliberate decision. Suppressing the rule at the call
    site (rather than globally in ``pyproject.toml``) keeps the guard
    active for every *other* subprocess call this project ever adds —
    including future ones that might genuinely be risky.
    """
    args = [
        sys.executable,
        *_GENERATOR_ARGS,
        "--input",
        str(schema_path),
        "--output",
        str(output_path),
    ]
    result = subprocess.run(
        args,  # noqa: S603 — args are compile-time literals; see docstring
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "datamodel-code-generator failed:\n"
            f"  command: {' '.join(args)}\n"
            f"  stderr:\n{result.stderr}"
        )


def _render(schema_path: Path, existing_output: Path | None) -> str:
    """Produce the final file text: header + generated body + hand-tuned blocks.

    ``existing_output`` is the currently-committed file, or ``None`` on
    a first-ever generation (no hand-tuned blocks to preserve). All the
    interesting logic lives in the helpers; this function is the
    composition step, kept small so the flow is readable end to end.
    """
    with tempfile.TemporaryDirectory(prefix="forge-models-") as tmp:
        raw_output = Path(tmp) / "blueprint.py"
        _run_generator(schema_path, raw_output)
        body = raw_output.read_text(encoding="utf-8")

    blocks = (
        _extract_hand_tuned(existing_output.read_text(encoding="utf-8"))
        if existing_output is not None and existing_output.exists()
        else []
    )

    return _FILE_HEADER + "\n" + _reinject_hand_tuned(body, blocks)


# ---------------------------------------------------------------------------
# CLI.
# ---------------------------------------------------------------------------
def _build_arg_parser() -> argparse.ArgumentParser:
    """Build the argument parser.

    A dedicated parser (rather than ``sys.argv`` parsing) gives
    contributors ``--help`` for free and makes the ``--check`` flag
    self-documenting in CI logs.
    """
    parser = argparse.ArgumentParser(
        prog="python scripts/generate_models.py",
        description=(
            "Regenerate src/forge/models/blueprint.py from the canonical "
            "blueprint JSON Schema. Run after changing the schema; CI runs "
            "with --check to reject drift."
        ),
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help=(
            "regenerate into memory and compare against the committed file; "
            "exit 1 on any difference (used by CI and `make models-check`)"
        ),
    )
    return parser


def _print_diff(expected: str, actual: str, output_path: Path) -> None:
    """Print a unified diff of the drift for the developer to act on.

    Uses ``difflib`` rather than shelling out to ``diff`` so the output
    is identical across platforms — CI logs from a macOS developer and a
    Linux runner must be comparable, or "works on my machine" debugging
    becomes guesswork.
    """
    diff = difflib.unified_diff(
        actual.splitlines(keepends=True),
        expected.splitlines(keepends=True),
        fromfile=f"{output_path} (committed)",
        tofile=f"{output_path} (regenerated)",
        n=3,
    )
    sys.stderr.writelines(diff)


def main(argv: list[str] | None = None) -> int:
    """Entry point. See the module docstring for exit codes and semantics.

    ``argv`` is accepted so tests can invoke ``main(["--check"])``
    without spawning a subprocess. ``None`` means "use ``sys.argv[1:]``"
    — the standard argparse convention.
    """
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    if not _SCHEMA_PATH.exists():
        print(f"✗ schema not found at {_SCHEMA_PATH}", file=sys.stderr)
        return 3

    try:
        expected = _render(_SCHEMA_PATH, _OUTPUT_PATH)
    except RuntimeError as exc:
        print(f"✗ regeneration failed: {exc}", file=sys.stderr)
        return 2

    if args.check:
        if not _OUTPUT_PATH.exists():
            print(
                f"✗ {_OUTPUT_PATH} does not exist; run `make models` to create it",
                file=sys.stderr,
            )
            return 1
        actual = _OUTPUT_PATH.read_text(encoding="utf-8")
        if expected != actual:
            print(
                "✗ generated models are out of sync with the schema — "
                "run `make models` and commit the result",
                file=sys.stderr,
            )
            _print_diff(expected, actual, _OUTPUT_PATH)
            return 1
        print("✓ models in sync with schema")
        return 0

    _OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    _OUTPUT_PATH.write_text(expected, encoding="utf-8")
    print(f"✓ wrote {_OUTPUT_PATH.relative_to(_REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
