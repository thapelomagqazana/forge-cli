"""Unit tests for the `forge list` table renderer and command.

What this file covers
---------------------
The pure table renderer (`forge.cli.renderers.table`) and the testable
command seam (`forge.cli.commands.list.run`) in table mode. These are
tested *together* because the command's obligation is "produce this
table", and splitting them would require re-asserting the same layout
invariants twice.

What this file deliberately does NOT cover
------------------------------------------
* **Discovery** -- mocked at the service boundary. Table formatting has
  no opinion about where blueprints come from.
* **JSON output** -- lives in `test_list_json.py`.
* **Empty state** -- lives in `test_list_empty.py`. Note the *renderer's*
  empty case (`render_table([])`) is covered here because it is a
  renderer property; the *command's* empty-message behaviour is not.
* **Sorting** -- lives in `test_list_stable.py`. The renderer preserves
  input order; the *command* sorts (per ADR-0012). Testing sort here
  would confuse "who owns ordering?" -- the answer is the command, and
  the test that proves it lives there.

Snapshot update policy
----------------------
Snapshot fixtures under `tests/fixtures/list/snapshot_*.txt` are
*cosmetic*, not contractual (see the fixtures README). Regenerating is
expected and cheap -- but always inspect the diff before committing,
because a snapshot change that also changes column *content* (not just
widths) usually indicates a real bug in the DTO projection.

Regenerate with:
    UPDATE_SNAPSHOTS=1 uv run pytest tests/unit/cli/test_list_table.py --no-cov

Note: the `--no-cov` flag is required for single-file iteration. The
global coverage gate in `pyproject.toml` measures the whole `src/` tree
on every run, and a single-file run can never meet a 90% gate by
construction. Use `--no-cov` locally; let CI run the full suite with
coverage enabled.

Snapshot newline convention
---------------------------
The `_assert_snapshot` helper that used to live in this file has been
replaced by `tests.unit.cli._snapshot.assert_golden`, which is shared
with `test_list_json.py` and `test_list_empty.py`. The helper
normalises both sides to the payload (see its docstring), which fixes
the trailing-newline failure that all three files hit independently.
The move is not "extract a function for its own sake" -- it is what
stops the third copy of a helper from being fixed by the fourth
response to a fourth failure report.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from forge.cli.commands import list as list_cmd
from forge.cli.renderers.table import _LIST_COLUMNS, render_table
from forge.cli.schemas.list_output import BlueprintStatus, ListOutputItem
from forge.models.blueprint import (
    BlueprintSource,
    Category,
    DiscoveredBlueprint,
)
from tests.unit.cli._snapshot import FIXTURE_ROOT, assert_golden

# ---------------------------------------------------------------------------
# Shared fixtures and helpers
# ---------------------------------------------------------------------------

_FIXTURES = FIXTURE_ROOT  # re-exported for tests that need a path directly


def _row(
    id_: str = "python.fastapi.api",
    name: str = "Python FastAPI API",
    version: str = "1.0.0",
    category: Category = Category.api,
    status: BlueprintStatus = BlueprintStatus.ok,
) -> ListOutputItem:
    """Build a `ListOutputItem` with sane defaults for one-field tests.

    Defaults mirror the shipped `python.fastapi.api` blueprint so that
    tests read like real output -- a reviewer can glance at a test and
    know what the row represents without decoding dummy strings.

    Field-length constraints are honoured: ``name`` is capped at 120 and
    ``id`` must match the blueprint-ID pattern. Tests that need to
    exercise *truncation* must supply values that are valid per the DTO
    but exceed the *renderer's* column caps -- those are the only
    values the renderer is required to truncate. A value that violates
    the DTO is testing Pydantic, not the renderer, and would fail here
    with a confusing `ValidationError` rather than a useful message.
    """
    return ListOutputItem(
        id=id_,
        name=name,
        version=version,
        category=category,
        status=status,
    )


def _discovered(
    id_: str,
    name: str,
    version: str = "1.0.0",
    category: str = "api",
    source: BlueprintSource = BlueprintSource.OFFICIAL,
) -> DiscoveredBlueprint:
    """Build a `DiscoveredBlueprint` -- the discovery service's return type."""
    return DiscoveredBlueprint(
        id=id_,
        name=name,
        version=version,
        description="d",
        category=category,
        source=source,
    )


def _run_command(
    pairs: list[tuple[DiscoveredBlueprint, BlueprintStatus]],
    *,
    json_mode: bool = False,
    capsys: pytest.CaptureFixture[str],
) -> int:
    """Invoke `forge list` with mocked discovery; return the exit code.

    One helper, so every command test exercises the same wiring:
    mocked discovery service, real command seam, captured output. The
    command's contract is "given this discovery result, produce this
    output and this exit code" -- the helper makes that contract the
    *only* thing each test has to express.

    The mock patches `forge.cli.commands.list.BlueprintDiscoveryService`
    (the name the command module imports), not
    `forge.discovery.BlueprintDiscoveryService` (the name discovery
    defines). Patching the import site is what makes the mock effective
    regardless of when the command module was imported.
    """
    with patch(
        "forge.cli.commands.list.BlueprintDiscoveryService",
    ) as service_cls:
        service_cls.return_value.discover_with_status.return_value = pairs
        return list_cmd.run(SimpleNamespace(json=json_mode))


# ---------------------------------------------------------------------------
# Renderer -- pure-function tests
# ---------------------------------------------------------------------------


class TestRenderTableShape:
    """The table's structural invariants.

    These do not depend on any particular row content -- they assert on
    properties every table must have: headers, separator, columns, and
    line endings.
    """

    def test_headers_match_declared_column_spec(self) -> None:
        # The header row is derived from `_LIST_COLUMNS`, so this
        # asserts the column spec itself -- not just the output -- which
        # makes the "where do I add a column?" answer obvious to anyone
        # reading the test.
        expected = [header for header, _ in _LIST_COLUMNS]
        rendered = render_table([_row()])
        assert rendered.splitlines()[0].split() == expected

    def test_separator_row_uses_only_dashes_and_spaces(self) -> None:
        rendered = render_table([_row()])
        separator = rendered.splitlines()[1]
        assert separator, "separator must not be empty"
        assert set(separator) <= {"-", " "}, "separator must contain only dashes and column gaps"

    def test_every_row_contains_every_field(self) -> None:
        item = _row(
            id_="custom.id",
            name="Custom",
            version="2.3.4",
            category=Category.cli,
            status=BlueprintStatus.stale,
        )
        rendered = render_table([item])
        body = rendered.splitlines()[2]
        for expected in ("custom.id", "Custom", "2.3.4", "cli", "stale"):
            assert expected in body, f"{expected!r} missing from row: {body!r}"

    def test_columns_are_separated_by_two_spaces(self) -> None:
        # Two spaces is the documented column separator (see the
        # renderer's `_COLUMN_SEP`). A single space would still *look*
        # like a table but would make the ID column ambiguous for
        # values containing a single space (none today, but the
        # contract should be unambiguous).
        rendered = render_table([_row(id_="a.b", name="X")])
        header = rendered.splitlines()[0]
        assert "  " in header


class TestRenderTableOrdering:
    """The renderer must preserve the caller's order verbatim.

    Per ADR-0012, sorting lives in the *command* (once, for all
    renderers). A renderer that re-sorted would silently disagree with
    the JSON renderer whenever the two receive the same input -- the
    exact class of bug the DTO + renderer split was introduced to
    prevent. This test is the guard.
    """

    def test_rows_are_rendered_in_input_order(self) -> None:
        a, b, c = (_row(id_="z.z"), _row(id_="a.a"), _row(id_="m.m"))
        lines = render_table([a, b, c]).splitlines()
        body_ids = [line.split()[0] for line in lines[2:]]
        assert body_ids == [
            "z.z",
            "a.a",
            "m.m",
        ], "renderer must not sort; sorting belongs to the command"


class TestRenderTableTruncation:
    """Long values are truncated with a single `...`, never wrapped."""

    def test_long_id_is_truncated_with_ellipsis(self) -> None:
        # 45 chars: valid per the DTO (<= 128), but exceeds the ID
        # column cap (32). This is the input the renderer is actually
        # required to truncate.
        long_id = "python." + "segment." * 4 + "end"
        assert len(long_id) > 32
        assert len(long_id) <= 128
        rendered = render_table([_row(id_=long_id)])
        body = rendered.splitlines()[2]
        assert "\u2026" in body, "long values must be truncated with a single ellipsis glyph"
        assert long_id not in body, "full value must not appear"

    def test_truncated_rows_stay_on_one_line(self) -> None:
        # The critical property: truncation must never introduce a line
        # break, because a wrapped ID cannot be copy-pasted into
        # `forge create <id>` -- the primary use of that column.
        #
        # 100 chars is safely above the NAME column cap (32) and safely
        # below the DTO cap (120). A 200-char name would fail DTO
        # validation before the renderer ever saw it, testing Pydantic
        # rather than truncation.
        long_name = "x" * 100
        rendered = render_table([_row(name=long_name)])
        assert len(rendered.splitlines()) == 3, "header + separator + 1 row -- no wrapping"

    def test_short_values_are_not_truncated(self) -> None:
        rendered = render_table([_row(id_="a.b", name="X")])
        assert "\u2026" not in rendered


class TestRenderTableWhitespace:
    """Output must be diff-clean: no trailing whitespace, no ANSI."""

    def test_no_line_has_trailing_whitespace(self) -> None:
        rendered = render_table(
            [
                _row(id_="a.b", name="X"),
                _row(id_="longer.blueprint.id", name="A longer name"),
            ]
        )
        for index, line in enumerate(rendered.splitlines()):
            assert line == line.rstrip(), f"line {index} has trailing whitespace: {line!r}"

    def test_no_ansi_escape_sequences(self) -> None:
        # The renderer is documented as ANSI-free (ADR-0012). Colour is
        # applied by `ui.emit`. A stray escape would poison JSON output
        # and force every snapshot to strip codes.
        rendered = render_table([_row()])
        assert "\x1b" not in rendered, "renderer must never emit ANSI escapes"

    def test_no_trailing_newline(self) -> None:
        # `ui.emit` owns line termination; the renderer must return a
        # bare string. A double newline would produce a blank line at
        # the end of every `forge list` invocation.
        rendered = render_table([_row()])
        assert not rendered.endswith("\n")


class TestRenderTableEmpty:
    """The renderer's empty-list behaviour.

    Note: this is *renderer* behaviour, not *command* behaviour. The
    command prints "No blueprints found." without calling the renderer
    (see `test_list_empty.py`); the renderer's own empty case is a
    building-block contract -- "header + separator, no rows" -- kept
    honest so a future composite output (say, a report with multiple
    tables) can rely on it.
    """

    def test_empty_list_renders_header_and_separator_only(self) -> None:
        rendered = render_table([])
        lines = rendered.splitlines()
        assert len(lines) == 2, "header + separator, no body rows"
        assert lines[0].split() == [h for h, _ in _LIST_COLUMNS]
        assert set(lines[1]) <= {"-", " "}


# ---------------------------------------------------------------------------
# Command -- integration through the command seam
# ---------------------------------------------------------------------------


class TestListCommandTableOutput:
    """`forge list` (table mode) -- end-to-end through `list_cmd.run`."""

    def test_table_mode_prints_one_row_per_blueprint(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        pairs = [
            (_discovered("a.a", "Alpha"), BlueprintStatus.ok),
            (_discovered("b.b", "Beta", category="cli"), BlueprintStatus.ok),
        ]
        exit_code = _run_command(pairs, capsys=capsys)
        out = capsys.readouterr().out

        assert exit_code == 0
        assert "a.a" in out
        assert "b.b" in out
        assert "Alpha" in out
        assert "Beta" in out
        # Header + separator + 2 rows = 4 non-empty lines.
        non_empty = [ln for ln in out.splitlines() if ln.strip()]
        assert len(non_empty) == 4

    def test_table_mode_exits_zero_on_success(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        pairs = [(_discovered("a.a", "Alpha"), BlueprintStatus.ok)]
        assert _run_command(pairs, capsys=capsys) == 0

    def test_table_mode_preserves_status_column(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        pairs = [
            (_discovered("a.a", "Alpha"), BlueprintStatus.ok),
            (_discovered("b.b", "Beta"), BlueprintStatus.stale),
        ]
        _run_command(pairs, capsys=capsys)
        out = capsys.readouterr().out
        assert "ok" in out
        assert "stale" in out

    def test_table_mode_does_not_call_json_renderer(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Guard against an accidental wiring regression: if the command
        # always rendered JSON regardless of `args.json`, this test
        # would pass (JSON also contains "a.a") -- so we assert
        # specifically on the header, which is table-only, and on the
        # absence of JSON's opening brace.
        pairs = [(_discovered("a.a", "Alpha"), BlueprintStatus.ok)]
        _run_command(pairs, json_mode=False, capsys=capsys)
        out = capsys.readouterr().out
        assert "ID" in out.splitlines()[0]
        assert "{" not in out, "table mode must not emit JSON"


# ---------------------------------------------------------------------------
# Snapshot fixtures -- the reviewed artefact
# ---------------------------------------------------------------------------


class TestSnapshots:
    """Compare rendered output against checked-in fixtures.

    Two snapshot families live in `tests/fixtures/list/`:

    * `snapshot_*.txt` -- table appearance. Cosmetic; may change between
      minor releases, but always *deliberately*. Regenerate with
      ``UPDATE_SNAPSHOTS=1`` and inspect the diff before committing.
    * `golden_*.json` -- JSON contract. Also exercised by
      `test_list_json.py`; not duplicated here to keep this file focused
      on table rendering.

    All fixtures go through the shared helper
    ``tests.unit.cli._snapshot.assert_golden``, which normalises the
    trailing-newline convention (files end with ``\\n``; payloads do
    not). The helper is shared with ``test_list_json.py`` and
    ``test_list_empty.py``, so a future change to the convention
    touches one place, not three.

    If a snapshot fixture is missing and ``UPDATE_SNAPSHOTS`` is not
    set, the helper fails with a message naming the exact command to
    regenerate it, rather than letting ``FileNotFoundError`` bubble up
    from ``Path.read_text``. That message is the fix -- regeneration is
    the expected response, not an investigation.
    """

    def test_snapshot_official_table(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        pairs = [
            (_discovered("python.automation.cli", "Python Automation CLI"), BlueprintStatus.ok),
            (_discovered("python.fastapi.api", "Python FastAPI API"), BlueprintStatus.ok),
            (
                _discovered(
                    "typescript.frontend.react",
                    "TypeScript React Frontend",
                    category="frontend",
                ),
                BlueprintStatus.ok,
            ),
        ]
        _run_command(pairs, capsys=capsys)
        out = capsys.readouterr().out
        # `ui.emit` appends exactly one trailing newline; the helper
        # normalises it away along with the fixture file's newline.
        # No `.rstrip("\n")` at the call site: the rule lives in the
        # helper, so every test in every file that uses it follows the
        # same rule.
        assert_golden(
            out,
            "snapshot_official.txt",
            contract_blurb=(
                "This is a *snapshot* (cosmetic), not a contract. A "
                "content diff here usually means a DTO projection bug; "
                "a width-only diff is expected churn."
            ),
        )

    def test_snapshot_truncation_table(self) -> None:
        # The ID column cap is 32 codepoints; this input is 45 chars,
        # comfortably over the cap and comfortably under the DTO's 128
        # cap, so the renderer is what produces the truncation.
        long_id = "python.fastapi.api.very.long.id.that.exceeds.the.cap"
        rendered = render_table([_row(id_=long_id, name="Demo")])
        assert_golden(
            rendered,
            "snapshot_truncation.txt",
            contract_blurb=(
                "Truncation is the renderer's contract: the ellipsis "
                "glyph and column widths are user-visible. A change "
                "here is consumer-facing for anyone parsing the table."
            ),
        )

    def test_snapshot_empty_renders_header_only(self) -> None:
        # The renderer's own empty case -- distinct from the command's
        # "No blueprints found." message (see test_list_empty.py).
        rendered = render_table([])
        assert_golden(
            rendered,
            "snapshot_empty_renderer.txt",
            contract_blurb=(
                "The renderer's empty case is a building block: a "
                "future composite report (multiple tables) relies on "
                "'header + separator, no rows' being stable."
            ),
        )


# ---------------------------------------------------------------------------
# Guard tests -- regression protection for the renderer's contract
# ---------------------------------------------------------------------------


class TestRendererContractGuards:
    """Small guards that catch the most common ways to break the renderer.

    These are not exhaustive; they are the failure modes that have
    actually happened in review. Anything subtler is caught by the
    shape and snapshot tests above.
    """

    def test_renderer_module_does_not_import_ui(self) -> None:
        # The renderer returns a string; `ui` writes it. An import of
        # `ui` inside the renderer would invert that direction and
        # make the renderer untestable without mocking stdout.
        source = Path("src/forge/cli/renderers/table.py").read_text()
        assert "from forge.cli import ui" not in source
        assert "from forge.cli.ui import" not in source
        assert "import forge.cli.ui" not in source
