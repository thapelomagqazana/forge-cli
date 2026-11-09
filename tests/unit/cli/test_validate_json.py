"""Contract tests for `forge validate --json`.

Scope
-----
This file is the *contract test* for the machine-readable output of
``forge validate``. It is deliberately separate from
``test_validate_all_errors.py`` (which tests the human renderer)
because the two audiences have incompatible contracts:

    * **Human output** promises readability. Its exact line breaks,
      spacing, and bullet glyph are cosmetic and may change between
      releases without notice.
    * **JSON output** promises *parsing stability*. Its key set, key
      order, null semantics, and numeric types are consumed by CI
      scripts and IDE integrations. A consumer parsing
      ``payload["valid"]`` must never see that key change name,
      disappear, or become ``null`` unexpectedly.

Mixing the two test files would mean a cosmetic change to the human
rendering (a new bullet glyph, a rewritten hint) could break a
snapshot that was supposed to guard automation. That coupling is
precisely what ADR-0012 ("versioned DTO, pluggable renderers")
exists to prevent, and it is why this file is its own unit.

The contract is documented in ``docs/reference/CLI_VALIDATE_OUTPUT.md``.
These tests are its executable form: if you change the JSON output,
the contract doc and this file must change together, and a reviewer
sees both diffs in the same PR.

How the tests set JSON mode
---------------------------
``--json`` is a **keyword parameter** on the callback
(``validate(path=..., json=True)``). Typer threads it through the
callback signature exactly as it would from the CLI; there is nothing
to monkeypatch, no ``sys.argv`` to mutate, and no fixture to enable.
A test reads as the CLI invocation it simulates.

This matters for isolation. The earlier draft of this file tried to
enable ``--json`` via fixtures that monkeypatched either a
``_json_requested`` helper (which does not exist) or ``sys.argv``
(which was never consulted). Neither worked; every test in the file
was silently exercising the *human* renderer, and the failures were
opaque ``JSONDecodeError`` exceptions against human output. Passing
the flag directly removes that class of bug entirely: if a test
wants JSON mode, the call site says so.

Why every call goes through ``_exit_code``
------------------------------------------
``validate`` raises ``typer.Exit`` on every exit path — the mechanism
Typer documents for exit codes under ``standalone_mode=False``. The
helper ``tests.unit.cli._validate_helpers._exit_code`` unwraps that
raise into an ``int`` so the test bodies can assert on the exit code
as a value. Every test in this file that invokes ``validate`` goes
through it, directly or via ``_invoke_json``.

Assertion discipline: parse, then assert
----------------------------------------
Every test parses the emitted string with ``json.loads`` **before**
asserting on any field. Two consequences:

    * A test cannot pass against output that is not valid JSON.
      (A test that asserted ``'"valid":true' in output`` could pass
      against a broken document that happened to contain that
      substring.)
    * Assertions target *fields* (``payload["valid"]``), not the raw
      string, so they stay stable when the renderer changes its
      whitespace policy, key order, or ``ensure_ascii`` setting —
      all of which are implementation details of the renderer, not
      of the contract.

The four contract promises
--------------------------
The test classes below are organised by the four promises the
contract makes:

    1. **Envelope shape** — every invocation produces the same
       top-level key set, regardless of validity.
    2. **Validity invariant** — ``valid`` is ``True`` if and only if
       ``errors`` is empty.
    3. **Error-entry shape** — every error carries ``path``,
       ``message``, ``hint`` (non-empty strings), ``code`` (string,
       possibly empty), and ``line`` / ``column`` (``int`` or
       ``null``).
    4. **Determinism** — identical inputs produce byte-identical
       output.

Plus two closing sections: mode independence (JSON and human never
bleed into each other) and contract-version pinning (the envelope
advertises ``schema_version``, and a bump is a deliberate change).

What is *not* covered here
--------------------------
* **Which errors appear for a given input.** That is the loader's
  job (``test_loader_multi_error.py``) and the human renderer's job
  (``test_validate_all_errors.py``). This file asserts that
  *whatever the loader returned* is serialised correctly — not that
  a particular input produces a particular set of errors. Splitting
  the two means a loader change and a renderer change each fail in
  one file, not both.
* **Human output.** Every assertion here is on ``json.loads`` output
  or on the absence of human markers. Whether the human report
  wraps, uses a bullet, or names fields is not this file's concern.
* **Typer's argument parsing.** The ``exists=True`` / ``readable=True``
  validators live in Typer; their behaviour is Typer's contract.
  This file assumes the callback is invoked, which is what happens
  after those validators pass.

Exit codes are asserted only where they interact with the JSON
envelope (a payload is *data*, but the process still exits 0 or 1).
The full exit-code contract is tested in
``test_validate_exit_codes.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from forge.cli.commands.validate import validate
from forge.cli.schemas.validate_output import ValidateOutput
from forge.core.exceptions import ExitCode
from tests.unit.cli._validate_helpers import _exit_code

# -----------------------------------------------------------------------------
# Fixtures — two blueprints with known, small outcomes
# -----------------------------------------------------------------------------
# Two violations (not three, not one) is the smallest number that
# proves "errors[] is a list, not a single entry" without making the
# fixture longer than the test that reads it. The specific violations
# are chosen to be unambiguous (missing `id` and malformed `version`
# are both top-level, both required, both named in their messages) so
# a reader of a failing test can predict the outcome by inspection.

_VALID_BLUEPRINT = """\
schema_version: "1.0.0"
id: sample.valid
name: Valid Blueprint
version: 1.0.0
description: A blueprint used only in JSON-contract tests.
category: api
verification:
  - id: has_readme
    type: required_file
    path: README.md
compatibility:
  min_forge_version: 1.0.0
"""

# Two independent schema violations:
#   1. `id` is missing (required).
#   2. `version` is "1.0", not SemVer.
_TWO_VIOLATIONS = """\
schema_version: "1.0.0"
name: Two Violations
version: "1.0"
description: A blueprint with two schema violations.
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


def _invoke_json(path: Path, capsys: pytest.CaptureFixture[str]) -> tuple[dict[str, Any], int]:
    """Run ``validate --json``; return ``(parsed_envelope, exit_code)``.

    Centralises three invariants every JSON test needs:

    * **Exit code.** ``validate`` raises ``typer.Exit`` on every exit
      path; the helper unwraps it into an ``int`` so tests can assert
      on the code as a value. See
      ``tests/unit/cli/_validate_helpers.py`` for the reasoning.
    * **stdout is a single parseable document.** A test cannot pass
      against output that is not valid JSON, so the parse happens
      once here rather than being re-asserted in every test.
    * **stderr is empty.** In JSON mode, human commentary has no
      home: the envelope is the report, and a stray ``ui.error``
      call in the JSON branch would corrupt a stream that consumers
      parse directly. Asserting ``captured.err == ""`` here turns
      "the JSON renderer accidentally prints to stderr" into a
      failure at the *call site* of every JSON test, not a
      mystery in one.

    Returns the parsed payload and the command's exit code, so each
    test can assert on the fields it cares about without re-running
    the command.
    """
    code = _exit_code(lambda: validate(path=path, json=True))
    captured = capsys.readouterr()

    assert captured.out.strip(), (
        "`--json` produced no stdout. The renderer must emit a JSON "
        "envelope even for a valid blueprint (with `errors: []`)."
    )
    assert captured.err == "", (
        f"`--json` wrote to stderr: {captured.err!r}. In JSON mode, "
        f"all output belongs in the envelope on stdout; anything else "
        f"is a bug."
    )
    payload = json.loads(captured.out)
    return payload, code


# -----------------------------------------------------------------------------
# 1 — Envelope shape
# -----------------------------------------------------------------------------
class TestEnvelopeShape:
    """Every invocation produces the same top-level key set.

    The envelope shape is the *first* thing a consumer sees, and the
    one thing a consumer never expects to vary between "valid" and
    "invalid" runs. A ``--json`` output that emits ``errors[]`` on
    success but omits it on failure (or vice versa) forces the
    consumer to write two code paths for one schema.
    """

    def test_valid_blueprint_emits_full_envelope(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A valid run emits the full envelope with ``errors: []``.

        This is the envelope's happy path: every field present, none
        omitted, ``valid`` true, ``errors`` an empty list.
        """
        manifest = _write_blueprint(tmp_path / "bp", _VALID_BLUEPRINT)

        payload, code = _invoke_json(manifest, capsys)

        assert code == ExitCode.SUCCESS
        assert set(payload) == {
            "schema_version",
            "valid",
            "source",
            "blueprint_id",
            "version",
            "errors",
        }
        assert payload["schema_version"] == "1.0.0"
        assert payload["valid"] is True
        assert payload["errors"] == []
        assert payload["blueprint_id"] == "sample.valid"
        assert payload["version"] == "1.0.0"

    def test_invalid_blueprint_emits_same_envelope_with_null_id(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An invalid run emits the *same* envelope, not a subset.

        ``blueprint_id`` and ``version`` are present but ``null``:
        the loader had no model to extract them from. Presenting them
        as null (rather than omitting the keys) keeps the key set
        identical across success and failure — the property that
        makes the contract enforceable.
        """
        manifest = _write_blueprint(tmp_path / "bp", _TWO_VIOLATIONS)

        payload, code = _invoke_json(manifest, capsys)

        assert code == ExitCode.USER_ERROR
        assert set(payload) == {
            "schema_version",
            "valid",
            "source",
            "blueprint_id",
            "version",
            "errors",
        }
        assert payload["valid"] is False
        # The two fields are present (in the key set above) but null.
        assert payload["blueprint_id"] is None
        assert payload["version"] is None
        assert len(payload["errors"]) == 2

    def test_source_echoes_the_user_path(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``source`` echoes the path the user passed, not a resolved one.

        A batch of ``forge validate --json`` invocations will often be
        consumed together (e.g. one job per blueprint). The ``source``
        field is what lets a consumer correlate a payload with the
        file that produced it. Echoing the user's path (rather than
        the resolved absolute path) is what a caller can *match against
        their own inputs*.
        """
        manifest = _write_blueprint(tmp_path / "bp", _VALID_BLUEPRINT)

        payload, _ = _invoke_json(manifest, capsys)

        # The source label is the path the test passed; the loader
        # may resolve it, but the command echoes the callback's
        # ``path`` argument, which is what the test supplied.
        assert str(manifest) in payload["source"] or payload["source"] == str(manifest)


# -----------------------------------------------------------------------------
# 2 — Validity invariant
# -----------------------------------------------------------------------------
class TestValidityInvariant:
    """``valid`` is the boolean shadow of ``len(errors) == 0``.

    The invariant is emitted explicitly so consumers never have to
    compute it. The tests below prove the two representations agree:
    a document that reports ``valid: true`` must have zero errors,
    and one that reports ``valid: false`` must have at least one.

    The rule is not cosmetic: a consumer that reads only ``valid``
    must get the same verdict as one that inspects ``errors``. If
    they ever disagreed, automation that trusted ``valid`` would
    silently ship broken blueprints.
    """

    def test_valid_true_implies_zero_errors(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A ``valid: true`` payload has an empty ``errors`` list."""
        manifest = _write_blueprint(tmp_path / "bp", _VALID_BLUEPRINT)

        payload, _ = _invoke_json(manifest, capsys)

        assert payload["valid"] is True
        assert payload["errors"] == []

    def test_valid_false_implies_at_least_one_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A ``valid: false`` payload has a non-empty ``errors`` list."""
        manifest = _write_blueprint(tmp_path / "bp", _TWO_VIOLATIONS)

        payload, _ = _invoke_json(manifest, capsys)

        assert payload["valid"] is False
        assert len(payload["errors"]) >= 1


# -----------------------------------------------------------------------------
# 3 — Error-entry shape
# -----------------------------------------------------------------------------
class TestErrorEntryShape:
    """Every entry in ``errors[]`` conforms to the documented shape.

    The per-error shape is what a consumer parses to display or act on
    a violation. It must be uniform: a consumer that renders a hint
    must find a hint on every entry, not on some.
    """

    def test_every_error_has_the_required_keys(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Each error has ``path``, ``message``, ``hint``, ``code``, ``line``, ``column``."""
        manifest = _write_blueprint(tmp_path / "bp", _TWO_VIOLATIONS)

        payload, _ = _invoke_json(manifest, capsys)

        expected_keys = {"path", "message", "hint", "code", "line", "column"}
        for entry in payload["errors"]:
            assert set(entry) == expected_keys

    def test_path_message_hint_are_non_empty_strings(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``path``, ``message``, ``hint`` are non-empty strings.

        Non-empty is the loader's guarantee: every ``ValidationError``
        carries a hint (a missing hint is a loader defect; see
        ``ValidationError``'s docstring), and every error has *some*
        path (the root, ``""``, when the error is document-wide — but
        the renderer represents that as the empty string, not as a
        missing key). The test enforces the string-typed, present
        contract for all three fields.

        It does *not* enforce non-empty for ``path`` (root errors
        legitimately have an empty path); the invariant there is
        "present and a string".
        """
        manifest = _write_blueprint(tmp_path / "bp", _TWO_VIOLATIONS)

        payload, _ = _invoke_json(manifest, capsys)

        for entry in payload["errors"]:
            assert isinstance(entry["path"], str)
            assert isinstance(entry["message"], str)
            assert entry["message"]  # non-empty
            assert isinstance(entry["hint"], str)
            assert entry["hint"]  # non-empty — this is the KPI

    def test_code_is_a_string(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """``code`` is a string (possibly empty), never null.

        ``code`` is a machine-readable error class. Some errors (from
        deep inside the loader) may not have a code assigned yet; the
        contract is that the *field* is always a string, so consumers
        can branch on ``code == "required.missing"`` without null
        checks. The empty string is the "no code known" value.
        """
        manifest = _write_blueprint(tmp_path / "bp", _TWO_VIOLATIONS)

        payload, _ = _invoke_json(manifest, capsys)

        for entry in payload["errors"]:
            assert isinstance(entry["code"], str)

    def test_line_and_column_are_int_or_null(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``line`` and ``column`` are ``int`` or ``null``, never a string.

        YAML parse errors carry line/column; schema and model errors
        typically do not. The field type is ``int | None`` in both
        cases — a consumer that displays "at line X" must be able to
        branch on ``entry["line"] is not None`` without parsing.
        """
        manifest = _write_blueprint(tmp_path / "bp", _TWO_VIOLATIONS)

        payload, _ = _invoke_json(manifest, capsys)

        for entry in payload["errors"]:
            assert entry["line"] is None or isinstance(entry["line"], int)
            assert entry["column"] is None or isinstance(entry["column"], int)


# -----------------------------------------------------------------------------
# 4 — Determinism
# -----------------------------------------------------------------------------
class TestDeterminism:
    """Byte-for-byte stability across runs, for the same input.

    Two prompts require this:

    * **Snapshot testing.** A CI job that diffs a ``forge validate
      --json`` report against yesterday's must not see unrelated
      churn because the renderer iterates a set or a dictionary in a
      hash-randomised order.
    * **Caching.** A consumer that caches validation results (an IDE
      doing this is very common) must be able to compute a stable
      hash. Any nondeterminism invalidates every cache.

    The renderer achieves this via ``sort_keys=True`` and
    ``separators`` (see ``render_validate_json``). The test proves
    the property, not the implementation — any future renderer that
    satisfies the property passes.
    """

    def test_two_runs_are_byte_identical(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Same input, two invocations, identical bytes on stdout."""
        manifest = _write_blueprint(tmp_path / "bp", _TWO_VIOLATIONS)

        _exit_code(lambda: validate(path=manifest, json=True))
        first = capsys.readouterr()

        _exit_code(lambda: validate(path=manifest, json=True))
        second = capsys.readouterr()

        assert first.out == second.out
        # stderr should be empty in JSON mode — errors are *data* in
        # the envelope, not commentary.
        assert first.err == ""
        assert second.err == ""


# -----------------------------------------------------------------------------
# 5 — Mode independence
# -----------------------------------------------------------------------------
class TestModeIndependence:
    """Human mode and JSON mode do not bleed into each other.

    A command with two output modes has two ways to fail: JSON mode
    accidentally rendering human text (which would make the payload
    unparseable), or human mode accidentally rendering JSON (which
    would confuse users). Both are covered by asserting that each
    mode's output lacks the other mode's characteristic markers.
    """

    def test_json_mode_output_is_pure_json(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """JSON mode emits nothing but a parseable document.

        The negative assertion (``• not in output``) is what
        distinguishes "valid JSON plus a stray human line" from "valid
        JSON". A renderer bug that leaked a human message into stdout
        would produce *parseable JSON after the JSON* only if the leak
        came after; the assertion catches both pre- and post-leak.
        """
        manifest = _write_blueprint(tmp_path / "bp", _TWO_VIOLATIONS)

        _exit_code(lambda: validate(path=manifest, json=True))
        captured = capsys.readouterr()

        # The output is parseable (this would raise if not).
        json.loads(captured.out)

        # And it does not contain the human renderer's bullet glyph.
        assert "•" not in captured.out
        # And it does not contain the human summary line.
        assert "validation error(s)" not in captured.out

    def test_human_mode_output_is_not_json(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Human mode emits text that is *not* a JSON document.

        Symmetric to the above. A stray ``{"valid": false, ...}`` line
        in human output would confuse a user and, if captured to a
        file that a tool expects to be human-readable, break every
        downstream script that parses the file's *lines*.
        """
        manifest = _write_blueprint(tmp_path / "bp", _TWO_VIOLATIONS)

        _exit_code(lambda: validate(path=manifest, json=False))
        captured = capsys.readouterr()

        # Human output is not valid JSON (``json.loads`` raises).
        with pytest.raises(json.JSONDecodeError):
            json.loads(captured.out or captured.err)
        # And it contains the human renderer's markers.
        assert "•" in captured.out + captured.err


# -----------------------------------------------------------------------------
# 6 — Contract version pin
# -----------------------------------------------------------------------------
class TestContractVersion:
    """The envelope advertises its schema version.

    ``schema_version`` is the field consumers branch on when they
    need to support multiple CLI versions in one codebase (an IDE
    integration talking to a user-installed ``forge`` is the canonical
    case). The test pins the version this CLI emits so that a bump is
    a deliberate change with a test update, not an accidental one.

    When the contract changes incompatibly, this test fails *and* the
    ``ValidateOutput`` DTO's ``schema_version`` field fails to
    construct (per the ``Literal`` type), *and*
    ``CLI_VALIDATE_OUTPUT.md`` must be updated. Three independent
    tripwires for one event is the right number when a downstream
    consumer's bugs cost more than three tests.
    """

    def test_schema_version_is_1_0_0(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The emitted envelope advertises ``schema_version == '1.0.0'``."""
        manifest = _write_blueprint(tmp_path / "bp", _VALID_BLUEPRINT)

        payload, _ = _invoke_json(manifest, capsys)

        assert payload["schema_version"] == "1.0.0"

    def test_dto_pins_the_same_version(self) -> None:
        """The DTO's default and the emitted field agree.

        Belt-and-braces: the ``ValidateOutput`` model's default is
        what constructors use, and the emitted field is what consumers
        see. If they ever drifted (someone changed the default in the
        DTO but not in the command, or vice versa), the command's
        serialised version would disagree with what the DTO advertises
        to its own tests. This assertion catches that drift cheaply.

        This test does not invoke ``validate``; it only constructs a
        ``ValidateOutput``. It therefore stays untouched by the
        exit-code mechanism change — it has never asserted on an exit
        code and never will.
        """
        output = ValidateOutput(valid=True, source="<test>")
        assert output.schema_version == "1.0.0"
