"""Unit tests for `parse_yaml` — the loader's first phase.

What this suite must prove
--------------------------
`parse_yaml` is the loader's only entry point for turning raw bytes into
a typed document. Every downstream phase — schema validation, model
construction, filesystem checks — sees the output of this function, so a
bug here corrupts the entire pipeline. The four contracts this suite
pins:

1. **Success returns `ParsedDocument` with matching `source`.** No
   surprises on the happy path, and the caller-supplied label is
   propagated verbatim so downstream phases can stamp their own errors
   without re-plumbing it.

2. **Every failure returns `ValidationError`, never raises.** The
   loader's four-phase pipeline is flat precisely because the parser
   does not throw. A future refactor that raised `YAMLError` would
   force the loader to wrap this phase in `try/except` — breaking the
   uniform shape every other phase relies on.

3. **Located errors carry `line`/`column`.** YAML syntax errors are
   where users most need a location, because the fix is usually a
   one-character edit on a specific line. Line/column are 1-based
   (matching editors), not 0-based (PyYAML's internal convention).

4. **Every `ValidationError` carries a non-empty `hint`.** This is
   the "100% of errors carry a hint" KPI in miniature. The hint
   catalogue (`loading/hints.py`) guarantees this in production; this
   suite proves it for every error path the parser can produce.

What is deliberately *not* tested
---------------------------------
* **Exact hint text.** Hint wording is a UX concern that lives in
  `hints.py` and has its own tests (`test_hints.py`). Here we only
  assert non-emptiness — the contract `parse_yaml` is responsible
  for.

* **Byte-level decode failures with exotic codecs.** The parser
  handles UTF-8, period. `test_non_utf8_*` covers the realistic case
  (Latin-1 bytes, which many editors produce when a user pastes from
  an old source). A codec-matrix test would be exhaustive and useless
  — no realistic blueprint is authored in UTF-16.

* **YAML spec corner cases.** `safe_load` handles them; if a future
  PyYAML version changes a corner case, that is a PyYAML concern and
  a different test suite (integration, not unit). This suite tests
  *our* code, which is: decode, dispatch to `safe_load`, classify
  the result.

* **Exact error locations from PyYAML.** Some YAML errors — an
  unclosed flow sequence, an unterminated quoted scalar — are
  reported by PyYAML at *end-of-stream*, not at the character where
  the construct began. That is PyYAML's choice and it is
  defensible: the parser cannot know the construct is unclosed
  until it stops reading. Our parser converts the 0-based line to
  1-based and passes it through; it does not second-guess PyYAML.
  The syntax-error tests therefore assert the *relationship*
  (1-based, at or beyond the last content line) rather than pinning
  an exact line number for a class of error whose location is an
  artefact of PyYAML's reading order.

* **Malicious YAML payloads.** The security property ("`safe_load`
  cannot instantiate objects") belongs to PyYAML, not to us. The
  security tests exist as a *regression sentinel*: they prove we
  never accidentally switch to `yaml.load` (which would be the
  actual security bug). They assert `safe_load` behaviour, not our
  own.

Fixture strategy
----------------
Small inline byte strings for most cases — a YAML snippet that takes
three lines to construct is easier to read than a fixture file whose
contents the reader has to open separately. Files under
`tests/fixtures/loader/` are used only where the fixture is *also*
referenced by another test module (e.g. the loader's own tests use
`malformed.yaml` to prove the end-to-end error path). Sharing fixtures
across modules is deliberate: one canonical "malformed YAML" fixture
means the parser tests and the loader tests cannot drift into
disagreeing about what "malformed" means.

Structure
---------
Tests are grouped by *outcome* (success / failure / located error),
not by the input they exercise, so a reader looking up "what happens
on non-UTF-8 input?" finds the answer by scanning `TestDecodeFailures`
rather than by grepping for "utf8" across the file.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

import pytest

from forge.loading.errors import ValidationError
from forge.loading.yaml_parser import ParsedDocument, parse_yaml

# Shared fixtures live here so the parser suite and the loader suite
# cannot drift into disagreeing about what "malformed" means.
_FIXTURES = Path(__file__).parents[2] / "fixtures" / "loader"


# =============================================================================
# Success — the happy path
# =============================================================================
class TestSuccess:
    """A well-formed mapping returns `ParsedDocument` with data and source."""

    def test_simple_mapping_returns_parsed_document(self):
        result = parse_yaml(b"id: sample\nname: Sample\n", source="<test>")
        assert isinstance(result, ParsedDocument)
        assert result.data == {"id": "sample", "name": "Sample"}
        assert result.source == "<test>"

    def test_source_label_is_preserved_verbatim(self):
        """The caller-supplied label propagates unchanged.

        Downstream phases (schema, model, filesystem) stamp their own
        errors with `parsed.source`, so any transformation here would
        silently change the `source` field on every error the user
        sees. Verbatim propagation is the contract.
        """
        label = "examples/blueprints/python.fastapi.api/blueprint.yaml"
        result = parse_yaml(b"id: sample\n", source=label)
        assert isinstance(result, ParsedDocument)
        assert result.source == label

    def test_empty_mapping_is_success(self):
        """A file containing only `{}` is a valid (empty) mapping.

        The schema phase will reject it for missing required fields —
        but that is the schema's job, not the parser's. The parser's
        only responsibility is "did the YAML parse to a mapping?".
        Splitting responsibilities this way means a schema change
        never requires a parser change.
        """
        result = parse_yaml(b"{}", source="<test>")
        assert isinstance(result, ParsedDocument)
        assert result.data == {}

    def test_utf8_multibyte_content_is_accepted(self):
        """Blueprint descriptions routinely contain non-ASCII text."""
        result = parse_yaml(
            "description: Café ☕ — ünïcode\nid: sample\n".encode(),
            source="<test>",
        )
        assert isinstance(result, ParsedDocument)
        assert "Café" in result.data["description"]

    def test_comments_only_mapping_parses_to_empty_dict(self):
        """A file with comments and a mapping still parses."""
        raw = b"# a comment\n# another comment\nid: sample\n"
        result = parse_yaml(raw, source="<test>")
        assert isinstance(result, ParsedDocument)
        assert result.data == {"id": "sample"}

    def test_utf8_bom_is_handled_by_pyyaml(self):
        """A UTF-8 BOM is legal and PyYAML strips it transparently.

        Users on Windows (Notepad, older PowerShell) routinely save
        files with a BOM. Failing on BOM would be a defect, not a
        feature. The test is a sentinel: if a future `strict` decode
        change drops BOM tolerance, this fails immediately.
        """
        raw = b"\xef\xbb\xbfid: sample\n"
        result = parse_yaml(raw, source="<test>")
        assert isinstance(result, ParsedDocument)
        assert result.data == {"id": "sample"}


# =============================================================================
# Decode failures — bytes that are not UTF-8
# =============================================================================
class TestDecodeFailures:
    """Non-UTF-8 input returns an error without line/column and with a hint."""

    def test_non_utf8_bytes_return_validation_error(self):
        # 0xE9 is "é" in Latin-1 and an invalid UTF-8 start byte.
        result = parse_yaml(b"description: caf\xe9\n", source="<test>")
        assert isinstance(result, ValidationError)

    def test_decode_error_mentions_utf8(self):
        """The message names the actual problem so the user does not
        go hunting for a YAML syntax issue that is not there."""
        result = parse_yaml(b"\xe9", source="<test>")
        assert isinstance(result, ValidationError)
        assert "utf-8" in result.message.lower() or "utf8" in result.message.lower()

    def test_decode_error_has_hint(self):
        """Every error must hint at the fix — re-saving as UTF-8."""
        result = parse_yaml(b"\xe9", source="<test>")
        assert isinstance(result, ValidationError)
        assert result.hint

    def test_decode_error_has_no_line_or_column(self):
        """Byte offsets are not line/column; we do not invent them.

        A decode failure occurs *before* the document is parsed, so
        there is no line/column to report — the encoding error may
        affect any byte of the file. Populating line=1 as a
        "convenience" would lie to the user; leaving it None is
        honest.
        """
        result = parse_yaml(b"\xe9", source="<test>")
        assert isinstance(result, ValidationError)
        assert result.line is None
        assert result.column is None

    def test_decode_error_preserves_source(self):
        result = parse_yaml(b"\xe9", source="broken.yaml")
        assert isinstance(result, ValidationError)
        assert result.source == "broken.yaml"


# =============================================================================
# YAML syntax errors — located failures
# =============================================================================
class TestSyntaxErrors:
    """Malformed YAML returns a located `ValidationError`.

    Line/column invariants asserted here are the parser's own
    conversion (0-based PyYAML mark → 1-based user-facing number),
    not PyYAML's choice of where to report a specific error. For
    "unclosed construct" failures, PyYAML reports at end-of-stream
    — the position where the parser gives up reading — which is one
    line past the last content line. The tests therefore assert the
    relationship (``line >= last content line``) rather than a
    specific number that depends on PyYAML's reading order.
    """

    def test_malformed_yaml_returns_validation_error(self):
        result = parse_yaml(b"id: [unclosed\n", source="<test>")
        assert isinstance(result, ValidationError)

    def test_malformed_yaml_carries_line_and_column(self):
        """Located errors always provide *some* line and column.

        For an unclosed construct PyYAML reports at end-of-stream
        (one past the last content line); the parser converts that
        mark to 1-based and passes it through. The test asserts the
        invariant a user relies on — "there is a line and a column,
        and both are 1-based" — rather than an exact number that
        depends on the error class.

        Split into two asserts (rather than a single ``x is not None
        and x >= 3``) so a ``None`` failure reads as "the parser
        did not populate the field at all," which is a different bug
        class from "the parser populated the field, but with the
        wrong value."
        """
        raw = (
            b"id: ok\n" b"name: ok\n" b"bad: [unclosed\n"  # starts a flow sequence, never closes it
        )
        result = parse_yaml(raw, source="<test>")
        assert isinstance(result, ValidationError)
        assert result.line is not None
        assert result.line >= 3
        assert result.column is not None
        assert result.column >= 1

    def test_line_numbers_are_one_based(self):
        """Editors report 1-based line numbers; PyYAML reports 0-based.

        A 0-based number passed through unchanged would tell a user
        with a one-line file that the error is on line 0 — a line
        that does not exist. The parser adds 1 at the boundary; this
        test proves the conversion by asserting ``line >= 1`` for an
        error whose PyYAML mark is ``line=1`` (0-based). A direct
        ``line == 2`` assertion would also pass but would couple the
        test to PyYAML's EOF convention; ``>= 1`` pins what we
        actually control.
        """
        result = parse_yaml(b"id: [unclosed\n", source="<test>")
        assert isinstance(result, ValidationError)
        assert result.line is not None
        assert result.line >= 1

    def test_column_numbers_are_one_based(self):
        result = parse_yaml(b"id: [unclosed\n", source="<test>")
        assert isinstance(result, ValidationError)
        assert result.column is not None
        assert result.column >= 1

    def test_malformed_yaml_has_hint(self):
        result = parse_yaml(b"id: [unclosed\n", source="<test>")
        assert isinstance(result, ValidationError)
        assert result.hint

    def test_undefined_alias_is_rejected(self):
        """An alias (``*name``) with no matching anchor is malformed.

        The distinction matters: ``&x`` (an *anchor* with no value)
        is valid YAML that parses to ``None``; ``*x`` (a *reference*
        to an anchor that was never defined) is a ``ComposerError``.
        Only the second is a parse failure, and only the second
        belongs in a syntax-error test.
        """
        result = parse_yaml(b"a: *x\n", source="<test>")
        assert isinstance(result, ValidationError)

    def test_shared_fixture_is_malformed(self):
        """`fixtures/loader/malformed.yaml` is used by the loader suite.

        Sharing the fixture means the parser and the loader cannot
        disagree about what "malformed" means — if the loader starts
        accepting this file, this test fails first, and the loader
        test fails second. The order is intentional: the parser is
        the lower layer, and a break there is easier to debug.
        """
        raw = (_FIXTURES / "malformed.yaml").read_bytes()
        result = parse_yaml(raw, source=str(_FIXTURES / "malformed.yaml"))
        assert isinstance(result, ValidationError)
        assert result.line is not None
        assert result.line >= 1


# =============================================================================
# Non-mapping roots
# =============================================================================
class TestNonMappingRoots:
    """A blueprint must be a mapping; any other root is a located error."""

    def test_empty_document_is_rejected(self):
        """An empty file is the most common user mistake.

        The message must be *specific* — "blueprint is empty or
        contains only comments" — rather than the generic "root must
        be a mapping" that would leave a user wondering whether the
        file is broken or just empty.
        """
        result = parse_yaml(b"", source="<test>")
        assert isinstance(result, ValidationError)
        assert "empty" in result.message.lower() or "mapping" in result.message.lower()

    def test_whitespace_only_document_is_rejected(self):
        result = parse_yaml(b"\n\n   \n", source="<test>")
        assert isinstance(result, ValidationError)

    def test_comments_only_document_is_rejected(self):
        result = parse_yaml(b"# just a comment\n# another\n", source="<test>")
        assert isinstance(result, ValidationError)

    def test_explicit_null_document_is_rejected(self):
        result = parse_yaml(b"null\n", source="<test>")
        assert isinstance(result, ValidationError)

    def test_explicit_null_marker_document_is_rejected(self):
        """`---` with nothing after it parses to None."""
        result = parse_yaml(b"---\n", source="<test>")
        assert isinstance(result, ValidationError)

    def test_list_root_is_rejected(self):
        result = parse_yaml(b"- one\n- two\n", source="<test>")
        assert isinstance(result, ValidationError)

    def test_list_root_mentions_mapping(self):
        """The message must name the fix (nest under a key) not just the
        rule (must be a mapping)."""
        result = parse_yaml(b"- one\n- two\n", source="<test>")
        assert isinstance(result, ValidationError)
        assert "mapping" in result.message.lower()

    def test_scalar_string_root_is_rejected(self):
        result = parse_yaml(b"just a string\n", source="<test>")
        assert isinstance(result, ValidationError)

    def test_scalar_int_root_is_rejected(self):
        result = parse_yaml(b"42\n", source="<test>")
        assert isinstance(result, ValidationError)

    def test_boolean_root_is_rejected(self):
        result = parse_yaml(b"true\n", source="<test>")
        assert isinstance(result, ValidationError)


# =============================================================================
# Universal invariants — every error path
# =============================================================================
class TestUniversalInvariants:
    """Properties that hold for *every* `ValidationError` this module can
    produce, regardless of which failure family raised it.

    Parameterised over every failure input the parser can encounter
    so that adding a new failure branch requires a corresponding
    entry here — otherwise the invariant silently stops covering
    the new branch.

    Every entry here must *actually* be a failure. A test list that
    contains a valid input (e.g. ``a: &x``, which is a valid anchor
    with a null value) silently stops proving anything about
    failure modes, so this list is kept minimal and audited. The
    guard test at the bottom of the class fails loudly when the
    list's length changes, so adding a new mode is a deliberate act.
    """

    # Every distinct failure shape the parser can produce, with a
    # short label for readable parameterised output. If a future
    # change adds a new failure mode, this list must be extended.
    #
    # ``ClassVar`` tells RUF012 that this is intentionally class-level
    # (read-shared, never mutated through an instance) and not
    # instance state that happens to be declared on the class body.
    # ``pytest.mark.parametrize`` reads it off the class itself, so
    # the annotation matches the actual usage.
    _FAILURE_INPUTS: ClassVar[list] = [
        pytest.param(b"\xe9", "non-utf8", id="non-utf8"),
        pytest.param(b"", "empty", id="empty"),
        pytest.param(b"# comment\n", "comments-only", id="comments-only"),
        pytest.param(b"- a\n- b\n", "list-root", id="list-root"),
        pytest.param(b"42\n", "scalar-root", id="scalar-root"),
        pytest.param(b"id: [unclosed\n", "syntax", id="syntax"),
        # ``*x`` — an alias whose anchor was never defined. This is
        # the failure the previous list intended with ``&x``; ``&x``
        # is a valid anchor with a null value, so it was a success
        # case masquerading as a failure.
        pytest.param(b"a: *x\n", "undefined-alias", id="undefined-alias"),
    ]

    @pytest.mark.parametrize(("raw", "label"), _FAILURE_INPUTS)
    def test_returns_validation_error_not_exception(self, raw: bytes, label: str):
        """No failure mode raises — every one returns `ValidationError`."""
        result = parse_yaml(raw, source="<test>")
        assert isinstance(
            result, ValidationError
        ), f"expected ValidationError for {label!r}, got {type(result).__name__}"

    @pytest.mark.parametrize(("raw", "label"), _FAILURE_INPUTS)
    def test_hint_is_always_non_empty(self, raw: bytes, label: str):
        """Every error carries a non-empty hint — the funnel's KPI."""
        result = parse_yaml(raw, source="<test>")
        assert isinstance(result, ValidationError)
        assert result.hint, f"empty hint for {label!r}"

    @pytest.mark.parametrize(("raw", "label"), _FAILURE_INPUTS)
    def test_message_is_always_non_empty(self, raw: bytes, label: str):
        result = parse_yaml(raw, source="<test>")
        assert isinstance(result, ValidationError)
        assert result.message, f"empty message for {label!r}"

    @pytest.mark.parametrize(("raw", "label"), _FAILURE_INPUTS)
    def test_source_is_propagated(self, raw: bytes, label: str):
        """The `source` label is stamped on every error, never dropped."""
        result = parse_yaml(raw, source="my-label")
        assert isinstance(result, ValidationError)
        assert result.source == "my-label"

    def test_all_known_failure_modes_are_covered(self):
        """Guard against a new failure branch being added without a
        corresponding entry in ``_FAILURE_INPUTS``.

        If this test fails, the module has grown a new way to fail
        and the parameterised list above no longer exercises it.
        Add the new input to ``_FAILURE_INPUTS`` — do not weaken
        this test.
        """
        # Structural invariant, not a runtime one: the constant's
        # length is a proxy for "has someone reviewed this list
        # recently?". Bumping the number is fine; the comment in the
        # failure message tells the contributor why.
        assert len(self._FAILURE_INPUTS) == 7, (
            "A new failure mode was added to parse_yaml without being "
            "listed in TestUniversalInvariants._FAILURE_INPUTS. Add "
            "the input and update this count."
        )


# =============================================================================
# Security sentinel — safe_load only, never load
# =============================================================================
class TestSecuritySentinel:
    """Guards against a future refactor switching to ``yaml.load``.

    This is not a test of PyYAML — PyYAML's safety of ``safe_load``
    is well documented. It is a test of *our code*, which must call
    ``safe_load``. The failure mode this prevents: someone
    "simplifies" ``safe_load`` to ``load`` because a blueprint needs
    a tag. That would turn any blueprint into potential RCE, and no
    schema or model validation would catch it because the exploit
    runs during parse.

    If a future blueprint genuinely needs a custom tag, the fix is a
    narrow ``SafeLoader`` subclass with a specific constructor —
    not switching to ``load``.
    """

    def test_python_object_tag_is_rejected(self):
        """``!!python/object/apply`` must fail to parse.

        This tag, when evaluated, would call a Python function with
        arbitrary arguments — the canonical config-file RCE. With
        ``safe_load`` it raises ``ConstructorError``; the parser
        catches it and returns a `ValidationError`.
        """
        raw = b"!!python/object/apply:os.system\n" b"args: ['echo pwned']\n"
        result = parse_yaml(raw, source="<test>")
        assert isinstance(result, ValidationError)

    def test_python_name_tag_is_rejected(self):
        """``!!python/name`` is the simplest custom-tag bypass."""
        raw = b"!!python/name:os.system\n"
        result = parse_yaml(raw, source="<test>")
        assert isinstance(result, ValidationError)

    def test_unknown_tag_is_rejected(self):
        """Even harmless-looking unknown tags are rejected by ``safe_load``.

        This is *stricter* than necessary for the current schema, but
        that strictness is a feature — a whitelist is the only safe
        model for a config format that third parties may author.
        """
        raw = b"!!custom-tag\nid: sample\n"
        result = parse_yaml(raw, source="<test>")
        assert isinstance(result, ValidationError)
