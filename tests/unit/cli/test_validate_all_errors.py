"""Tests for `forge validate`'s error-aggregation contract.

Scope
-----
This file is the *aggregation contract test* for `forge validate`.
It verifies the behaviour the loader's aggregation pipeline exists to
provide: **one validation pass reports every violation, not just the
first.** A test here that passes but a test in `test_loader_*` fails
means the loader is fine and the *command* is dropping errors
somewhere between the loader's `Err` and the renderer. A test in
`test_loader_*` that fails but a test here passes is impossible: this
command cannot render errors the loader did not return.

The contract has four parts, all tested here:

1. **Multiplicity.** A document with N distinct violations produces
   N entries in the report — not one.
2. **Completeness.** Every violation the loader reported is rendered
   (nothing silently swallowed by dedup, filtering, or truncation).
3. **Hints.** Every rendered error carries an actionable hint. An
   error without a hint is a defect in the loader, not something the
   command should paper over.
4. **Determinism.** Two runs over the same input produce identical
   output. That makes the report diffable in CI and greppable in a
   terminal.

Why every test passes ``json=False`` explicitly
-----------------------------------------------
The callback signature is

    validate(path=..., json: bool = typer.Option(False, "--json"))

which, when called directly (as these tests do), binds ``json`` to
an ``typer.models.OptionInfo`` instance, not to ``False``. Typer
unpacks that during CLI dispatch; a direct Python call does not. The
default *value* of the ``OptionInfo`` is ``False``, so behaviour is
still correct after the command's ``_coerce_bool`` guard — but the
tests should not rely on that guard silently firing. They should say
what mode they intend, at the call site, so a reader knows the test
is exercising human mode on purpose and not by accident.

The same discipline is used in ``test_validate_json.py``, where
every test passes ``json=True`` explicitly.

Why every call goes through ``_exit_code``
------------------------------------------
``validate`` raises ``typer.Exit`` on every exit path — the mechanism
Typer documents for exit codes under ``standalone_mode=False``. The
helper unwraps that raise into an ``int`` so the test bodies can
continue to assert on the exit code as a value. See
``tests/unit/cli/_validate_helpers.py`` for the full reasoning.

What is not covered here
------------------------
The loader's own aggregation is covered exhaustively in
``tests/unit/loading/test_loader_multi_error.py``. This file is about
the *command*'s use of what the loader returned.

Whitespace-insensitive matching
-------------------------------
The ``ui`` layer may wrap long messages at a terminal width. Every
assertion that inspects rendered output compares against
whitespace-stripped text — same discipline as
``test_validate_paths.py``, and for the same reason: the test asserts
*what information* reaches the user, not *where line breaks fall*.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from forge.cli.commands.validate import validate
from forge.core.exceptions import ExitCode
from tests.unit.cli._validate_helpers import _exit_code

# -----------------------------------------------------------------------------
# Whitespace-insensitive matching
# -----------------------------------------------------------------------------
_WS = re.compile(r"\s+")


def _compact(text: str) -> str:
    """Return ``text`` with every run of whitespace collapsed to nothing."""
    return _WS.sub("", text)


# -----------------------------------------------------------------------------
# Fixtures — blueprints with a known number of distinct violations
# -----------------------------------------------------------------------------
_THREE_SCHEMA_VIOLATIONS = """\
schema_version: "1.0.0"
name: Multi Error
version: "1.0"
description: A blueprint with three independent schema violations.
category: nope
verification:
  - id: has_readme
    type: required_file
    path: README.md
compatibility:
  min_forge_version: 1.0.0
"""

_ONE_SCHEMA_VIOLATION = """\
schema_version: "1.0.0"
id: sample.single
name: Single Error
version: "1.0"
description: A blueprint with exactly one schema violation.
category: api
verification:
  - id: has_readme
    type: required_file
    path: README.md
compatibility:
  min_forge_version: 1.0.0
"""


def _write_blueprint(directory: Path, body: str) -> Path:
    """Write ``blueprint.yaml`` in ``directory`` and return its path."""
    directory.mkdir(parents=True, exist_ok=True)
    manifest = directory / "blueprint.yaml"
    manifest.write_text(body, encoding="utf-8")
    return manifest


# -----------------------------------------------------------------------------
# Multiplicity — N violations produce N rendered errors
# -----------------------------------------------------------------------------
class TestAggregationMultiplicity:
    """Every violation the loader reports is rendered, not just the first."""

    def test_three_violations_produce_three_errors(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A document with three violations renders three error blocks."""
        manifest = _write_blueprint(tmp_path / "bp", _THREE_SCHEMA_VIOLATIONS)

        code = _exit_code(lambda: validate(path=manifest, json=False))
        captured = capsys.readouterr()
        rendered = captured.out + captured.err

        assert code == ExitCode.USER_ERROR
        assert rendered.count("•") == 3

    def test_summary_line_names_the_count(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The summary line states the count, so a user knows the scope."""
        manifest = _write_blueprint(tmp_path / "bp", _THREE_SCHEMA_VIOLATIONS)

        _exit_code(lambda: validate(path=manifest, json=False))
        captured = capsys.readouterr()
        compact = _compact(captured.out + captured.err)

        assert "3validationerror" in compact.lower()

    def test_single_violation_produces_one_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The boundary case: one violation, one bullet."""
        manifest = _write_blueprint(tmp_path / "bp", _ONE_SCHEMA_VIOLATION)

        _exit_code(lambda: validate(path=manifest, json=False))
        captured = capsys.readouterr()
        rendered = captured.out + captured.err

        assert rendered.count("•") == 1


# -----------------------------------------------------------------------------
# Completeness — every violation appears by content, not just by count
# -----------------------------------------------------------------------------
class TestAggregationCompleteness:
    """Counting bullets is necessary but not sufficient."""

    def test_all_three_field_names_appear(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The three violated fields are all named in the output."""
        manifest = _write_blueprint(tmp_path / "bp", _THREE_SCHEMA_VIOLATIONS)

        _exit_code(lambda: validate(path=manifest, json=False))
        captured = capsys.readouterr()
        compact = _compact(captured.out + captured.err).lower()

        assert "id" in compact
        assert "version" in compact
        assert "category" in compact

    def test_every_error_has_a_hint(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Every rendered error carries an actionable hint."""
        manifest = _write_blueprint(tmp_path / "bp", _THREE_SCHEMA_VIOLATIONS)

        _exit_code(lambda: validate(path=manifest, json=False))
        captured = capsys.readouterr()
        rendered = captured.out + captured.err

        bullets = rendered.count("•")
        hints = rendered.count("hint:")
        assert bullets == 3
        assert (
            hints == bullets
        ), f"{bullets} errors but {hints} hints — every error must carry a hint"


# -----------------------------------------------------------------------------
# Determinism — two runs produce identical output
# -----------------------------------------------------------------------------
class TestAggregationDeterminism:
    """The same input produces the same report, byte for byte."""

    def test_two_runs_produce_identical_output(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Rendering the same input twice yields byte-identical output."""
        manifest = _write_blueprint(tmp_path / "bp", _THREE_SCHEMA_VIOLATIONS)

        _exit_code(lambda: validate(path=manifest, json=False))
        first = capsys.readouterr()
        first_rendered = first.out + first.err

        _exit_code(lambda: validate(path=manifest, json=False))
        second = capsys.readouterr()
        second_rendered = second.out + second.err

        assert first_rendered == second_rendered


# -----------------------------------------------------------------------------
# Boundary — one error, many errors, and the summary line
# -----------------------------------------------------------------------------
class TestAggregationBoundaries:
    """The edges of the count: 1, 3, and the summary's agreement with reality."""

    def test_summary_count_matches_rendered_bullets(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The number in the summary line equals the number of bullets."""
        manifest = _write_blueprint(tmp_path / "bp", _THREE_SCHEMA_VIOLATIONS)

        _exit_code(lambda: validate(path=manifest, json=False))
        captured = capsys.readouterr()
        rendered = captured.out + captured.err

        compact = _compact(rendered)
        match = re.search(r"(\d+)validationerror", compact)
        assert match is not None, "summary line not found in output"
        summary_count = int(match.group(1))

        assert summary_count == rendered.count("•")

    def test_no_errors_summary_on_success(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A valid blueprint emits no error summary at all."""
        valid_blueprint = """\
schema_version: "1.0.0"
id: sample.valid
name: Valid Blueprint
version: 1.0.0
description: A blueprint with no violations.
category: api
verification:
  - id: has_readme
    type: required_file
    path: README.md
compatibility:
  min_forge_version: 1.0.0
"""
        manifest = _write_blueprint(tmp_path / "bp", valid_blueprint)

        _exit_code(lambda: validate(path=manifest, json=False))
        captured = capsys.readouterr()
        rendered = captured.out + captured.err

        assert "•" not in rendered
        assert "validationerror" not in _compact(rendered).lower()
