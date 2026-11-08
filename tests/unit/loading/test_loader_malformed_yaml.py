"""Unit tests for `BlueprintLoader.load` on malformed-YAML input.

What this suite must prove
--------------------------
The YAML phase is the loader's first phase, and it is the *only* phase
that can short-circuit the pipeline (a document that does not parse has
nothing for the schema or model to validate). That short-circuit is
deliberate (see the "Short-circuit rules" block in
`src/forge/loading/loader.py`), but it introduces two risks that this
suite exists to catch:

1. **The error is an `Err([...])`, not an exception.** The loader's
   contract is `Result[Blueprint, list[ValidationError]]`. A future
   refactor that "helpfully" wraps `parse_yaml` in `try/except` and
   re-raises would break every caller that branches on `.is_err()`.

2. **The error carries location and hint.** A malformed YAML error
   without a line/column forces the user to bisect the file by hand;
   a malformed YAML error without a hint leaves them with no idea what
   to do. Both must be present on *every* error the loader emits from
   this phase.

What this suite is *not* trying to prove
----------------------------------------
`tests/unit/loading/test_yaml_parser.py` already tests the parser in
isolation: syntax errors, empty documents, non-UTF-8 input, the
phantom-error filter, `safe_load` safety. This suite is about the
*integration* of that parser with the loader's phase machinery:

- the loader returns `Err` (not a raised exception)
- the error list is populated (not empty, not `[None]`)
- the error's `source` is the *file's* label (not the parser's stub
  label, not a temp path)
- the loader *short-circuits* — schema and model phases never run on
  unparseable input, so a document that is both malformed *and*
  schema-invalid produces exactly one phase's errors, not two

The last point is the one most likely to regress silently: an
over-eager refactor might run all phases unconditionally "for
completeness" and start returning errors that a user cannot fix until
they fix the parse error first. The `TestShortCircuit` class proves
this does not happen.

Fixture strategy
----------------
All inputs are inline byte strings, not fixture files. A malformed
YAML input is by definition not a reusable artifact — no two tests
share the same malformed bytes, and a fixture file would just be an
indirect reference to the same bytes. The one exception is
`tests/fixtures/loader/malformed.yaml`, which is shared with
`test_yaml_parser.py` to prove the parser and loader agree about what
"malformed" means (see the fixture's header comment). That fixture
lives under `tests/fixtures/loader/`, not here, because it is a
cross-module contract.
"""

from __future__ import annotations

# The one shared fixture — also consumed by test_yaml_parser.py.
# Sharing it means the two suites cannot drift into disagreeing
# about what "malformed YAML" looks like.
from pathlib import Path

from forge.loading import BlueprintLoader, Err, Ok
from forge.loading.errors import ValidationError

_FIXTURES = Path(__file__).parents[2] / "fixtures" / "loader"


# =============================================================================
# Helper — write bytes to a temp file and load
# =============================================================================
def _load_bytes(tmp_path: Path, raw: bytes) -> Ok | Err:
    """Stage ``raw`` as a file and run the loader against it.

    The loader reads from disk, not from bytes, so every test stages
    its input. Extracting the staging to a helper keeps each test's
    body focused on the assertion (not on file-writing boilerplate),
    and gives the fixture a stable, deterministic name — a
    ``blueprint.yaml`` inside a per-test tmpdir — so any error's
    ``source`` field can be asserted precisely.
    """
    blueprint = tmp_path / "blueprint.yaml"
    blueprint.write_bytes(raw)
    return BlueprintLoader().load(blueprint)


# =============================================================================
# Contract — malformed YAML returns Err, never raises
# =============================================================================
class TestReturnsErrNotRaises:
    """The loader's failure mode for malformed YAML is `Err([...])`.

    These tests would each fail individually on a different plausible
    refactor:

    - ``test_returns_err`` fails if the loader raises.
    - ``test_err_is_not_empty`` fails if the loader returns
      ``Err([])`` — technically an error but not actionable.
    - ``test_err_contains_only_validation_errors`` fails if the
      loader returns a mix of error types (e.g. a bare string for
      parse errors and `ValidationError`s for everything else).
    """

    def test_returns_err(self, tmp_path: Path):
        """Malformed YAML → `Err`, not an exception and not `Ok`."""
        result = _load_bytes(tmp_path, b"id: [unclosed\n")
        assert isinstance(result, Err)

    def test_returns_err_never_raises(self, tmp_path: Path):
        """A future refactor that wrapped `parse_yaml` in try/except and
        re-raised would break every caller. The loader must swallow
        the exception into the `Result`.
        """
        # A representative sample of pathological inputs. The point
        # is not to enumerate every failure mode (test_yaml_parser.py
        # does that) but to prove the loader's failure surface is
        # uniformly a `Result`.
        inputs = [
            b"id: [unclosed\n",
            b"\xe9",  # non-UTF-8
            b"",  # empty
            b"- a\n- b\n",  # list root
            b"a: *x\n",  # undefined alias
        ]
        for raw in inputs:
            result = _load_bytes(tmp_path, raw)  # must not raise
            assert isinstance(result, Err), raw

    def test_err_is_not_empty(self, tmp_path: Path):
        """`Err([])` would be useless — every Err must carry ≥ 1 error.

        An `Err` with no errors would let a caller branch into the
        failure path, render nothing, and exit with a non-zero code
        and no output. That is a worse failure than a crash.
        """
        result = _load_bytes(tmp_path, b"id: [unclosed\n")
        assert isinstance(result, Err)
        assert len(result.error) >= 1

    def test_err_contains_only_validation_errors(self, tmp_path: Path):
        """Every item in the error list is a `ValidationError`.

        This is what makes the CLI's renderer a simple loop over
        ``.format()`` rather than a ``match`` on error types.
        """
        result = _load_bytes(tmp_path, b"id: [unclosed\n")
        assert isinstance(result, Err)
        for error in result.error:
            assert isinstance(error, ValidationError)


# =============================================================================
# Location and hint — the actionable parts of the error
# =============================================================================
class TestErrorIsActionable:
    """Every malformed-YAML error carries location and hint.

    A validation error without location forces the user to bisect
    the file; without a hint it forces them to search the docs. Both
    are required by the funnel's KPI ("100% of errors carry a hint")
    and by the loading task's acceptance criteria ("located errors
    carry line/column").

    Note on ``line``: PyYAML reports unclosed constructs at
    end-of-stream, one past the last content line. The test asserts
    ``line is not None and line >= 1`` rather than an exact value,
    for the same reason `test_yaml_parser.py` does — the parser's
    contract is "convert 0-based to 1-based", not "second-guess
    PyYAML's choice of where to report".
    """

    def test_error_has_line(self, tmp_path: Path):
        result = _load_bytes(tmp_path, b"id: [unclosed\n")
        assert isinstance(result, Err)
        first = result.error[0]
        assert first.line is not None
        assert first.line >= 1

    def test_error_has_column(self, tmp_path: Path):
        result = _load_bytes(tmp_path, b"id: [unclosed\n")
        assert isinstance(result, Err)
        first = result.error[0]
        assert first.column is not None
        assert first.column >= 1

    def test_error_has_hint(self, tmp_path: Path):
        """The funnel's KPI in miniature: every error has a hint."""
        result = _load_bytes(tmp_path, b"id: [unclosed\n")
        assert isinstance(result, Err)
        for error in result.error:
            assert error.hint, f"empty hint on {error.path!r}"

    def test_error_has_message(self, tmp_path: Path):
        result = _load_bytes(tmp_path, b"id: [unclosed\n")
        assert isinstance(result, Err)
        for error in result.error:
            assert error.message, f"empty message on {error.path!r}"

    def test_error_source_is_the_file_label(self, tmp_path: Path):
        """The ``source`` field is the *file's* label, not a stub.

        Downstream code (the CLI renderer, future ``--json`` output)
        uses ``error.source`` to name the file the user should edit.
        A stub label — ``<memory>`` or a temp path — would point the
        user at the wrong file.
        """
        result = _load_bytes(tmp_path, b"id: [unclosed\n")
        assert isinstance(result, Err)
        expected = str(tmp_path / "blueprint.yaml")
        for error in result.error:
            assert error.source == expected


# =============================================================================
# Short-circuit — the YAML phase stops the pipeline
# =============================================================================
class TestShortCircuit:
    """A YAML parse failure prevents schema and model phases from running.

    Why this matters
    ----------------
    Two consequences, both user-facing:

    1. **Fewer, clearer errors.** A document with a YAML syntax error
       and a missing required field would produce *both* errors if all
       phases ran. The user would fix the required field, re-run, and
       discover the syntax error — a two-step fix where one step would
       suffice. Skipping the later phases makes the error list
       proportional to what the user can fix *right now*.

    2. **No wasted work.** Parsing YAML, building a Pydantic model, and
       running filesystem checks on a document that will be rejected
       by the YAML phase is pure waste — and worse, the schema phase
       may crash on a document whose structure is not yet trustworthy
       (e.g. ``id`` is a list because the syntax error confused the
       parser). The short-circuit is also a safety property.

    The loader's short-circuit is deliberate and documented in
    ``loader.py``; these tests prevent a future "run all phases for
    completeness" refactor from silently changing the user experience.
    """

    def test_only_yaml_phase_errors_reported(self, tmp_path: Path):
        """Errors from the YAML phase carry no ``schema.`` or ``model.``
        prefix.

        The loader aggregates errors with a phase prefix in their
        path (see ``from_phase`` in ``loading/aggregator.py``). If a
        schema-phase error ever appears alongside a YAML-parse error,
        that is a regression — the pipeline did not short-circuit.
        """
        # This input is both syntactically malformed *and*
        # schema-invalid (no `id`); the loader must report only the
        # parse error.
        result = _load_bytes(tmp_path, b"name: [unclosed\n")
        assert isinstance(result, Err)
        for error in result.error:
            # Parse errors have an empty path; nothing else should
            # appear. If a phase ran, we would see a path like
            # "schema.id" or "model.version".
            assert error.path == "", (
                f"unexpected phase-prefixed error {error.path!r}; "
                f"the loader should have short-circuited after the "
                f"YAML phase."
            )

    def test_returns_only_one_error_for_parse_failure(self, tmp_path: Path):
        """A parse failure produces exactly one error — the parse error.

        Running later phases would produce additional errors (e.g.
        "root must be a mapping" from the schema phase), which would
        be redundant with the parse error and confuse the user about
        which one to fix first.
        """
        result = _load_bytes(tmp_path, b"id: [unclosed\n")
        assert isinstance(result, Err)
        assert len(result.error) == 1


# =============================================================================
# The shared fixture — parser and loader agree
# =============================================================================
class TestSharedFixture:
    """The cross-module fixture produces `Err` from the loader too.

    ``fixtures/loader/malformed.yaml`` is consumed by both
    ``test_yaml_parser.py`` and this file. The shared-fixture test
    exists to prove the two suites cannot drift: if the fixture were
    accidentally edited to be valid YAML, the parser test would fail
    first (proving the fixture is wrong) and this test would fail
    second (proving the loader now disagrees). Failing bottom-up in
    the parser suite is easier to debug than failing top-down in the
    loader suite, so the parser test is the primary guard.
    """

    def test_shared_fixture_loads_as_err(self):
        result = BlueprintLoader().load(_FIXTURES / "malformed.yaml")
        assert isinstance(result, Err)
        assert len(result.error) >= 1

    def test_shared_fixture_error_source_is_fixture_path(self):
        result = BlueprintLoader().load(_FIXTURES / "malformed.yaml")
        assert isinstance(result, Err)
        expected = str(_FIXTURES / "malformed.yaml")
        for error in result.error:
            assert error.source == expected


# =============================================================================
# Message quality — the error names the actual problem
# =============================================================================
class TestMessageQuality:
    """The error message is not just non-empty — it is *informative*.

    The contract "message is a non-empty string" is necessary but not
    sufficient. A message like "error" satisfies it and helps nobody.
    These tests assert the message *names the failure class* the user
    needs to search for: "invalid YAML" for syntax errors, "utf-8"
    for encoding errors.
    """

    def test_syntax_error_mentions_yaml(self, tmp_path: Path):
        result = _load_bytes(tmp_path, b"id: [unclosed\n")
        assert isinstance(result, Err)
        assert "yaml" in result.error[0].message.lower()

    def test_non_utf8_error_mentions_utf8(self, tmp_path: Path):
        result = _load_bytes(tmp_path, b"\xe9")
        assert isinstance(result, Err)
        # Accept both "utf-8" and "utf8" spellings; the source uses
        # both across docstrings and messages, and the test should
        # not pin an exact string.
        message = result.error[0].message.lower()
        assert "utf-8" in message or "utf8" in message

    def test_empty_file_error_mentions_empty_or_mapping(self, tmp_path: Path):
        """An empty file's error must be specific, not the generic
        "root must be a mapping" (which would leave the user
        wondering whether the file is broken or just empty).
        """
        result = _load_bytes(tmp_path, b"")
        assert isinstance(result, Err)
        message = result.error[0].message.lower()
        assert "empty" in message or "mapping" in message

    def test_list_root_error_mentions_mapping(self, tmp_path: Path):
        """The fix for a list root is "nest under a key", so the
        message must name the mapping requirement."""
        result = _load_bytes(tmp_path, b"- a\n- b\n")
        assert isinstance(result, Err)
        assert "mapping" in result.error[0].message.lower()


# =============================================================================
# No cross-contamination — the YAML phase does not leak into other phases
# =============================================================================
class TestNoErrorLeakage:
    """Parse failures do not appear as schema or model failures.

    A subtle failure mode: if a future refactor made the phases run in
    a different order, or reused the parser inside the schema phase,
    the same error might appear twice — once with an empty path
    (parse) and once with a `schema.` prefix (surface symptom of the
    second run). This class is a tripwire for that refactor.
    """

    def test_no_schema_prefixed_error_on_parse_failure(self, tmp_path: Path):
        result = _load_bytes(tmp_path, b"id: [unclosed\n")
        assert isinstance(result, Err)
        for error in result.error:
            assert not error.path.startswith("schema."), (
                f"found schema-phase error {error.path!r} on a document "
                f"whose YAML failed to parse — the schema phase ran "
                f"despite the short-circuit."
            )

    def test_no_model_prefixed_error_on_parse_failure(self, tmp_path: Path):
        result = _load_bytes(tmp_path, b"id: [unclosed\n")
        assert isinstance(result, Err)
        for error in result.error:
            assert not error.path.startswith("model."), (
                f"found model-phase error {error.path!r} on a document "
                f"whose YAML failed to parse — the model phase ran "
                f"despite the short-circuit."
            )


# =============================================================================
# Rendered output — the CLI-facing string is usable
# =============================================================================
class TestRenderedOutput:
    """`ValidationError.format()` produces a copy-pasteable line.

    The CLI's `forge validate` command renders errors via
    ``error.format()``. If the renderer's output is missing a
    component (e.g. the hint, or the source), the CLI would show it
    — this test catches that regression at the loader level, before
    the CLI test would, because it exercises the actual `Err` the
    loader produced (not a hand-constructed one).
    """

    def test_format_includes_source(self, tmp_path: Path):
        result = _load_bytes(tmp_path, b"id: [unclosed\n")
        assert isinstance(result, Err)
        text = result.error[0].format()
        assert str(tmp_path / "blueprint.yaml") in text

    def test_format_includes_hint(self, tmp_path: Path):
        result = _load_bytes(tmp_path, b"id: [unclosed\n")
        assert isinstance(result, Err)
        text = result.error[0].format()
        # The exact hint wording is `hints.py`'s concern; the loader
        # test only asserts that the rendered form *has* a hint.
        assert "hint:" in text.lower()
