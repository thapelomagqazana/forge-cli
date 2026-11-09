"""Shared golden-fixture helper for the CLI unit tests.

Why this file exists
--------------------
Three test files (`test_list_json.py`, `test_list_empty.py`,
`test_list_table.py`) each grew their own `_assert_golden` /
`_assert_snapshot` helper. All three eventually hit the same bug — a
fixture file on disk ends with a newline (POSIX text-file convention),
while the payload being compared does not (`ui.emit` appends the
framing newline; the renderer does not put it in the payload) — and
all three had to be fixed independently, in the same way.

The lesson is not "we made a mistake"; it is "the same function written
three times will be fixed three times, and each fix will look like a
new bug in review." One helper, one rule, is cheaper than three
helpers that must be kept in sync.

The rule
--------
Files on disk end with a newline. Payloads do not. The helper
normalises both sides to the payload:

    * **Read side:** the file's trailing ``"\\n"`` is stripped before
      comparison.
    * **Write side:** ``UPDATE_SNAPSHOTS=1`` writes ``actual + "\\n"``,
      so the file on disk keeps canonical POSIX text-file form even
      when regenerated.

The read side uses ``.rstrip("\\n")``, not ``.rstrip()``: stripping
only the newline preserves a payload whose last character is a space.
``.rstrip()`` would silently mangle such a payload and make the test
incapable of catching a real bug where the renderer emitted a trailing
space.

Usage
-----
    from tests.unit.cli._snapshot import assert_golden, FIXTURE_ROOT

    def test_something(capsys):
        ...
        assert_golden(out, "snapshot_official.txt")

The helper takes the *actual* output — including any trailing newline
that ``ui.emit`` added. Normalisation lives here, not at the call site,
so every test in every file that uses the helper follows the same rule.

Why not ``tests/unit/cli/conftest.py``
--------------------------------------
A conftest fixture would require each test to take a fixture argument,
which adds a parameter to every signature and hides the rule from the
call site. A module-level function is explicit: a reviewer seeing
``assert_golden(out, "…")`` knows exactly where the rule lives.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

FIXTURE_ROOT = Path(__file__).resolve().parents[2] / "fixtures" / "list"

_UPDATE = os.environ.get("UPDATE_SNAPSHOTS") == "1"


def assert_golden(
    actual: str,
    fixture_name: str,
    *,
    contract_blurb: str = "",
) -> None:
    """Assert ``actual`` matches the golden fixture, with normalisation.

    Parameters
    ----------
    actual
        The output captured from the command or renderer. May include
        a trailing newline (``ui.emit`` adds one); the helper
        normalises it away before comparing.
    fixture_name
        The fixture file's name, relative to ``FIXTURE_ROOT``.
    contract_blurb
        Optional one-line description of *why* the fixture is a
        contract (e.g. "the JSON envelope for ``forge list --json``").
        Rendered in the failure message so a reviewer sees the stakes
        without opening another file.
    """
    fixture_path = FIXTURE_ROOT / fixture_name

    if _UPDATE:
        fixture_path.parent.mkdir(parents=True, exist_ok=True)
        # Write in canonical POSIX text-file form (trailing newline).
        # The read side strips exactly that one ``\n`` before
        # comparing, so the newline is a file convention, not part of
        # the payload. Removing the ``+ "\n"`` here would make every
        # editor-touched fixture regress to the same failure on the
        # next commit; leave it.
        fixture_path.write_text(actual.rstrip("\n") + "\n", encoding="utf-8")
        return

    if not fixture_path.exists():
        pytest.fail(
            f"Golden fixture {fixture_name!r} does not exist.\n"
            f"Regenerate it with:\n"
            f"    UPDATE_SNAPSHOTS=1 uv run pytest "
            f"{Path(__file__).name} --no-cov\n"
            f"and inspect the diff before committing."
        )

    expected = fixture_path.read_text(encoding="utf-8").rstrip("\n")
    actual_payload = actual.rstrip("\n")

    if actual_payload == expected:
        return

    blurb = f"\n{contract_blurb}\n" if contract_blurb else ""
    pytest.fail(
        f"\nGolden fixture mismatch for {fixture_name}.{blurb}"
        f"If this change is intentional, regenerate with:\n"
        f"    UPDATE_SNAPSHOTS=1 uv run pytest "
        f"{Path(__file__).name} --no-cov\n"
        f"and inspect the diff before committing.\n\n"
        f"--- expected ---\n{expected}\n"
        f"--- actual ---\n{actual_payload}\n"
    )
