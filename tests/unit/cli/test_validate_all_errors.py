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

What is not covered here
------------------------
The loader's own aggregation (its "do not short-circuit" rule, its
phase ordering) is covered exhaustively in
``tests/unit/loading/test_loader_multi_error.py``. Re-testing it here
would duplicate coverage without testing anything new — this file is
about the *command*'s use of what the loader returned. The two layers
have distinct failure modes:

    * Loader fails to aggregate → ``test_loader_multi_error`` fails.
    * Command drops errors from the loader's ``Err`` → this file fails.

Keeping the two separate is what lets a future refactor of either
layer be validated independently.

Why the callback is called directly
-----------------------------------
Same reasoning as ``test_validate_paths.py`` and
``test_validate_exit_codes.py``: the subject is the command's
rendering of a known ``Err``, not Typer's dispatch. Direct invocation
is faster, deterministic, and points failures at our logic rather
than Click's.

Whitespace-insensitive matching
-------------------------------
The ``ui`` layer may wrap long messages at a terminal width. Every
assertion that inspects rendered output compares against
whitespace-stripped text — same discipline as
``test_validate_paths.py``, and for the same reason: the test asserts
*what information* reaches the user, not *where line breaks fall*.
The helper is duplicated by three lines rather than imported from a
sibling test module: importing a helper from a test file couples the
two files' imports and makes a future move of one break the other.
Three lines is cheaper than that coupling.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from forge.cli.commands.validate import validate
from forge.core.exceptions import ExitCode

# -----------------------------------------------------------------------------
# Whitespace-insensitive matching
# -----------------------------------------------------------------------------
_WS = re.compile(r"\s+")


def _compact(text: str) -> str:
    """Return ``text`` with every run of whitespace collapsed to nothing.

    Collapsing to *empty* (not a single space) is deliberate: it lets
    a token match across a wrap point that splits the token at any
    character. See the module docstring for the full rationale.
    """
    return _WS.sub("", text)


# -----------------------------------------------------------------------------
# Fixtures — blueprints with a known number of distinct violations
# -----------------------------------------------------------------------------
# Each fixture is written to produce an exact, small number of
# violations, and the count is documented on the fixture. A test that
# asserts "at least N errors" would pass even if the loader started
# returning N+5 errors for unrelated reasons; asserting the exact
# count catches both under-reporting (the bug this file is about) and
# over-reporting (which would be a new loader bug worth a test).

# Three independent schema violations in one document:
#   1. `id` is missing (required).
#   2. `version` is "1.0", not SemVer.
#   3. `category` is "nope", not one of the enum values.
#
# The loader's schema phase returns all three in one pass (schema
# validation collects, it does not short-circuit), so the command has
# three errors to render.
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

# A single violation, used to prove the "one error" boundary of the
# aggregation logic (it should still render correctly with one).
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
        """A document with three violations renders three error blocks.

        The loader is designed to surface *all* violations from a
        phase, not just the first (a user fixes everything in one
        pass). The command must render that full list, which means a
        document with three violations must show three ``• `` bullets
        — not one, not two.
        """
        manifest = _write_blueprint(tmp_path / "bp", _THREE_SCHEMA_VIOLATIONS)

        code = validate(path=manifest)
        captured = capsys.readouterr()
        rendered = captured.out + captured.err

        # Exit code is still 1: multiplicity of errors does not change
        # the *category* of failure.
        assert code == ExitCode.USER_ERROR

        # Three bullets, one per violation. Counting bullets (rather
        # than occurrence of specific tokens) proves aggregation at
        # the render layer, independent of *which* violations the
        # loader happened to report.
        assert rendered.count("•") == 3

    def test_summary_line_names_the_count(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The summary line states the count, so a user knows the scope.

        Before scanning 3 (or 30) bulleted details, a user wants to
        know "how bad is it". The summary line answers that: it is
        emitted before the details, and its number is the same number
        as the details that follow.
        """
        manifest = _write_blueprint(tmp_path / "bp", _THREE_SCHEMA_VIOLATIONS)

        validate(path=manifest)
        captured = capsys.readouterr()
        compact = _compact(captured.out + captured.err)

        # "3 validation error(s)" appears, modulo whitespace.
        assert "3validationerror" in compact.lower()

    def test_single_violation_produces_one_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The boundary case: one violation, one bullet.

        Proves the renderer is not accidentally dropping or doubling
        the single-error case — both directions are bugs the
        aggregation tests must catch.
        """
        manifest = _write_blueprint(tmp_path / "bp", _ONE_SCHEMA_VIOLATION)

        validate(path=manifest)
        captured = capsys.readouterr()
        rendered = captured.out + captured.err

        assert rendered.count("•") == 1


# -----------------------------------------------------------------------------
# Completeness — every violation appears by content, not just by count
# -----------------------------------------------------------------------------
class TestAggregationCompleteness:
    """Counting bullets is necessary but not sufficient.

    A command could (hypothetically) render "3 bullets" by duplicating
    one error three times. This test class proves that all three
    distinct violations are present by checking for the field each
    violation concerns — ``id``, ``version``, ``category``. If any of
    those tokens is missing from the output, the aggregation is
    dropping content even if the bullet count is right.
    """

    def test_all_three_field_names_appear(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The three violated fields are all named in the output.

        The loader's error messages are field-specific (e.g. "field
        required: id", "not a valid SemVer: 1.0"). Naming the field is
        what makes the report actionable — a user reading "3 errors"
        without field names would have to diff the document against
        the schema themselves.
        """
        manifest = _write_blueprint(tmp_path / "bp", _THREE_SCHEMA_VIOLATIONS)

        validate(path=manifest)
        captured = capsys.readouterr()
        compact = _compact(captured.out + captured.err).lower()

        # Each of the three violations names its field. If any is
        # missing, the command dropped one of the loader's errors.
        assert "id" in compact  # missing-required violation
        assert "version" in compact  # SemVer violation
        assert "category" in compact  # enum violation

    def test_every_error_has_a_hint(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Every rendered error carries an actionable hint.

        The loader's contract is that ``hint`` is mandatory on every
        ``ValidationError`` (see ``ValidationError``'s docstring).
        The command's renderer uses ``ValidationError.format()``,
        which includes the hint when present. This test verifies the
        *end-to-end* result: the number of ``hint:`` tokens in the
        output equals the number of ``•`` bullets.

        If this fails, the loader produced an error without a hint
        (a defect the loader's own tests should have caught) or the
        renderer dropped it (a defect in the command). Either way,
        the report is not actionable.
        """
        manifest = _write_blueprint(tmp_path / "bp", _THREE_SCHEMA_VIOLATIONS)

        validate(path=manifest)
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
    """The same input produces the same report, byte for byte.

    Determinism is a contract for two reasons:

    * **CI diffing.** A validation report that appears on every run
      must not shuffle line order between runs, or a "no changes"
      run appears as a change.
    * **Snapshot testing.** ``test_validate_all_errors``'s siblings
      (and any future golden-file test) rely on the same input
      producing the same output. A renderer that uses a set's
      iteration order — a common bug — passes each individual
      assertion but fails this one.
    """

    def test_two_runs_produce_identical_output(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Rendering the same input twice yields byte-identical output.

        This catches the "renderer iterates a set" bug in one
        assertion: if any part of the report's order were
        non-deterministic, a large enough document (or, occasionally,
        a small one) would fail this test.
        """
        manifest = _write_blueprint(tmp_path / "bp", _THREE_SCHEMA_VIOLATIONS)

        validate(path=manifest)
        first = capsys.readouterr()
        first_rendered = first.out + first.err

        validate(path=manifest)
        second = capsys.readouterr()
        second_rendered = second.out + second.err

        assert first_rendered == second_rendered


# -----------------------------------------------------------------------------
# Boundary — one error, many errors, and the summary line
# -----------------------------------------------------------------------------
class TestAggregationBoundaries:
    """The edges of the count: 1, 3, and the summary's agreement with reality.

    These are separated from the multiplicity tests above because
    they check the *relationship* between the count and the
    rendering, not the count itself.
    """

    def test_summary_count_matches_rendered_bullets(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The number in the summary line equals the number of bullets.

        A renderer that lists N bullets but says "N+1 errors" (or
        "N-1") undermines user trust in the whole report. The count
        is not a user-facing approximation; it is the authoritative
        statement of scope.
        """
        manifest = _write_blueprint(tmp_path / "bp", _THREE_SCHEMA_VIOLATIONS)

        validate(path=manifest)
        captured = capsys.readouterr()
        rendered = captured.out + captured.err

        # Extract the number that precedes "validation error" in the
        # summary. Both surfaces may be wrapped, so operate on the
        # whitespace-stripped form and extract there.
        compact = _compact(rendered)
        match = re.search(r"(\d+)validationerror", compact)
        assert match is not None, "summary line not found in output"
        summary_count = int(match.group(1))

        assert summary_count == rendered.count("•")

    def test_no_errors_summary_on_success(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A valid blueprint emits no error summary at all.

        Conversely, a *valid* validation must not accidentally render
        "0 validation error(s)" — a summary line implies an error
        report follows, and an empty one looks like a bug. The success
        path is a single success line, not a summary + empty list.
        """
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

        validate(path=manifest)
        captured = capsys.readouterr()
        rendered = captured.out + captured.err

        assert "•" not in rendered
        assert "validationerror" not in _compact(rendered).lower()
