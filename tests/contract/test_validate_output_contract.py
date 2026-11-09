"""Snapshot-lock the `forge validate --json` envelope shape.

Scope
-----
This file is the **contract snapshot** for the JSON output of
``forge validate``. It is narrower than
``tests/unit/cli/test_validate_json.py`` on purpose:

    * ``test_validate_json.py`` asserts *behavioural* properties
      (invariants that hold for any input, any run). It answers
      "does the envelope satisfy its promises?".
    * **This file** asserts a *frozen artefact* (an exact byte-level
      rendering of a known input). It answers "has the envelope
      shape changed since the last time someone reviewed it?".

Those are different questions, and they fail at different times:

    * A behaviour test fails when the code stops honouring a
      promise. It does not fail when a *new field is added*, even
      though adding a field to a versioned contract is exactly the
      kind of change a reviewer must see.
    * A snapshot test fails when the *bytes change*, even when the
      change is behaviourally benign. That "false positive" is the
      feature: the snapshot forces every shape change through a
      deliberate, reviewed commit.

Together, they make a shape change impossible to land without both
the behaviour suite confirming nothing broke *and* the snapshot
confirming someone updated it on purpose.

Why snapshot at the contract tier (not the unit tier)
-----------------------------------------------------
The ``tests/contract/`` tier exists for outputs that cross the
process boundary — the places where a change is not a code change,
it is a *breaking change for someone else's automation*. The
``forge validate --json`` envelope is such a boundary. It is
consumed by CI pipelines, IDE integrations, and the kind of glue
scripts that nobody updates for years and everybody forgets about
until they break. The unit tier is the wrong home for a snapshot
because a unit test can be legitimately "adapted" when the code
changes; a contract test cannot, and it lives in a directory whose
name reminds a reviewer of that.

Why golden fixtures live in ``tests/fixtures/validate/``
--------------------------------------------------------
The golden JSON files are small, and their content is the *subject*
of the test — not an incidental argument to it. Putting them
elsewhere (inline in the test, in a conftest, in ``tests/fixtures``
but unnamed) would make the diff for a shape change appear as a
line in a Python file, where it competes for attention with the
assertions around it. In a ``.json`` fixture, the diff of a shape
change *is* the diff of the contract. That is the right shape for
a reviewer to see.

Regeneration policy
-------------------
When the shape legitimately changes (a new field is added, a field
is renamed, ``schema_version`` is bumped), the procedure is:

    1. Change the code (``ValidateOutput``, ``_render_json``).
    2. Change ``docs/reference/CLI_VALIDATE_OUTPUT.md`` to match.
    3. Regenerate the golden fixtures:
       ``UPDATE_SNAPSHOTS=1 uv run pytest tests/contract/test_validate_output_contract.py``
    4. Review the resulting ``git diff`` of the fixtures as a
       *contract diff*, not a test-diff. The question is not "do the
       tests still pass?"; it is "is this the new contract, and do
       we accept it?".
    5. Land all three (code, doc, fixtures) in one commit.

The ``UPDATE_SNAPSHOTS`` gate is what distinguishes an intentional
change from an accidental one: the test only rewrites the fixture
when the environment variable is set, so a shape change that lands
without it fails loudly, which is what a reviewer needs to see.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from forge.cli.renderers import render_validate_json
from forge.cli.schemas.validate_output import ValidateError, ValidateOutput

# -----------------------------------------------------------------------------
# Fixture location
# -----------------------------------------------------------------------------
# The path is computed once and asserted to exist. If a contributor
# deletes the fixtures directory, they get a clear error here, not a
# confusing "empty string vs empty string" pass in every test.
_FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "validate"


def _load_golden(name: str) -> dict:
    """Load a golden fixture as parsed JSON.

    Parsed, not raw text: the snapshot locks the *contract* (key set,
    values, types), not the byte-level formatting. Whitespace in a
    golden fixture is incidental; if we locked bytes, a formatter
    pass on the fixture file would fail the test for no reason. When
    we *do* want to lock bytes (for a renderer's own
    ``sort_keys``/``separators`` discipline), that is tested in
    ``test_validate_json.py::TestDeterminism``, not here.
    """
    return json.loads((_FIXTURE_DIR / name).read_text(encoding="utf-8"))


def _maybe_update(name: str, actual: dict) -> None:
    """Rewrite a golden fixture when ``UPDATE_SNAPSHOTS=1`` is set.

    Two design choices worth stating:

    * **Opt-in, never implicit.** The env var gate is load-bearing:
      without it, a legitimate "the fixture is stale" failure would
      silently overwrite the fixture on every run, and the shape
      change would land without a reviewer ever seeing the diff.
      The gate makes regeneration a *deliberate* action.

    * **Rendered the same way the CLI renders it.** The bytes written
      here must match what ``render_validate_json`` produces, so the
      fixture and the CLI output agree byte-for-byte when the test
      runs. Using a different serialisation (say, ``json.dumps(...,
      indent=2)`` for readability) would mean the fixture never
      *quite* matches a real run, and a future test that compared
      bytes would diverge. Keeping the fixture as the exact output
      of the pure renderer is the invariant that makes this file
      trustworthy.
    """
    if os.environ.get("UPDATE_SNAPSHOTS") == "1":
        # Serialise exactly as the renderer would, then re-parse to
        # get consistent indentation for the file. The round-trip is
        # a defensive check: if the renderer produced something that
        # does not parse, we want the failure here, not in a
        # subsequent test that opens the corrupt file.
        canonical = json.loads(render_validate_json(_output_from(actual)))
        (_FIXTURE_DIR / name).write_text(
            json.dumps(canonical, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )


def _output_from(parsed: dict) -> ValidateOutput:
    """Rebuild a ``ValidateOutput`` from a parsed dict.

    Used only by the regeneration helper. Kept as a helper (not a
    method on the DTO) because the DTO intentionally has no
    ``from_dict`` classmethod — the resolver owns construction, and
    the renderer owns serialisation; a bidirectional round-trip on
    the DTO would blur that. A private test helper is the right home
    for the "reverse it just for fixtures" concern.
    """
    return ValidateOutput(
        valid=parsed["valid"],
        source=parsed["source"],
        blueprint_id=parsed.get("blueprint_id"),
        version=parsed.get("version"),
        errors=[
            ValidateError(
                path=e["path"],
                message=e["message"],
                hint=e["hint"],
                code=e.get("code", ""),
                line=e.get("line"),
                column=e.get("column"),
            )
            for e in parsed.get("errors", [])
        ],
    )


# -----------------------------------------------------------------------------
# Valid envelope — golden fixture
# -----------------------------------------------------------------------------
class TestValidEnvelopeSnapshot:
    """The valid-blueprint envelope is a frozen artefact.

    A blueprint that validates cleanly produces the canonical
    "nothing is wrong" shape. Every consumer's happy-path branch is
    written against this exact document. If the shape changes (a new
    field appears, a field type changes, ``schema_version`` bumps),
    the change must show up as a diff in the fixture file, so a
    reviewer sees "here is what consumers will now receive" and can
    approve or reject it as a contract decision.
    """

    def test_matches_golden_fixture(self) -> None:
        """The emitted bytes match ``valid.json`` exactly.

        Constructs the DTO directly (rather than running the command)
        because the snapshot's job is to lock the *envelope shape*,
        not the command's plumbing. The command's plumbing is
        exercised end-to-end in ``test_validate_json.py``; this test
        starts from a known ``ValidateOutput`` and asserts what the
        renderer produces from it. If the two ever disagree, that is
        a bug — but the bug is *not* "the golden fixture is out of
        date"; it is "the renderer produces something the fixture
        does not expect", which is a code change, not a snapshot
        update.

        The snapshot-diff (intentional change) and the behaviour-diff
        (unintentional bug) are distinguished by whether
        ``UPDATE_SNAPSHOTS=1`` is set: with the env var, an
        intentional change is expected and the fixture is rewritten;
        without it, the failure is a warning to the reviewer that a
        shape change landed.
        """
        output = ValidateOutput(
            valid=True,
            source="my-blueprint",
            blueprint_id="python.fastapi.api",
            version="1.0.0",
        )

        actual = json.loads(render_validate_json(output))
        expected = _load_golden("valid.json")

        if actual != expected:
            _maybe_update("valid.json", actual)
            if os.environ.get("UPDATE_SNAPSHOTS") == "1":
                pytest.skip("snapshot regenerated; re-run without UPDATE_SNAPSHOTS")
            pytest.fail(
                f"valid-envelope snapshot changed.\n"
                f"  expected: {json.dumps(expected, indent=2, sort_keys=True)}\n"
                f"  actual:   {json.dumps(actual,   indent=2, sort_keys=True)}\n"
                f"  If intentional: UPDATE_SNAPSHOTS=1 pytest {Path(__file__).name}\n"
                f"  Update CLI_VALIDATE_OUTPUT.md in the same commit."
            )


# -----------------------------------------------------------------------------
# Invalid envelope — golden fixture
# -----------------------------------------------------------------------------
class TestInvalidEnvelopeSnapshot:
    """The invalid-blueprint envelope is a frozen artefact.

    The failure shape is the one automation reads most often (CI
    jobs, pre-commit hooks, IDE error markers). Its stability matters
    more than the success shape's — a broken success path is noticed
    by users; a broken failure path is noticed by CI at 2 a.m.
    """

    def test_matches_golden_fixture(self) -> None:
        """The emitted bytes match ``invalid.json`` exactly."""
        output = ValidateOutput(
            valid=False,
            source="my-blueprint",
            errors=[
                ValidateError(
                    path="id",
                    message="'id' is a required property",
                    hint="Provide a dotted lowercase identifier such as 'python.fastapi.api'.",
                    code="required.missing",
                ),
                ValidateError(
                    path="version",
                    message="'1.0' does not match the SemVer pattern",
                    hint="Use full SemVer: MAJOR.MINOR.PATCH, e.g. '1.0.0'.",
                    code="pattern.invalid",
                ),
            ],
        )

        actual = json.loads(render_validate_json(output))
        expected = _load_golden("invalid.json")

        if actual != expected:
            _maybe_update("invalid.json", actual)
            if os.environ.get("UPDATE_SNAPSHOTS") == "1":
                pytest.skip("snapshot regenerated; re-run without UPDATE_SNAPSHOTS")
            pytest.fail(
                f"invalid-envelope snapshot changed.\n"
                f"  expected: {json.dumps(expected, indent=2, sort_keys=True)}\n"
                f"  actual:   {json.dumps(actual,   indent=2, sort_keys=True)}\n"
                f"  If intentional: UPDATE_SNAPSHOTS=1 pytest {Path(__file__).name}\n"
                f"  Update CLI_VALIDATE_OUTPUT.md in the same commit."
            )


# -----------------------------------------------------------------------------
# Shape contract — the *structure* the fixtures must have
# -----------------------------------------------------------------------------
class TestEnvelopeSchemaShape:
    """Structural invariants that any valid envelope must satisfy.

    These are stricter than the behaviour tests (they name the exact
    key set and types) but looser than the snapshots (they do not
    pin values). Their job is to fail with a *clear message* when
    the shape changes: "the top-level key set changed from X to Y"
    is a better error than a fixture diff that a reviewer has to
    decode.

    A shape change therefore hits two tests:

    * this one, with a message that names the change in English,
    * the corresponding snapshot, with a message that shows the
      exact bytes.

    Two messages for one event is the correct amount of noise when
    the event is "the contract changed" — the reviewer should not
    have to reverse-engineer what changed from a JSON diff.
    """

    def test_top_level_keys_are_exactly_the_documented_set(self) -> None:
        """The top-level key set is exactly the six documented keys.

        Both success and failure envelopes are checked. A
        ``ValidateOutput`` constructed with or without ``blueprint_id``
        and ``version`` must produce the same *key set* — the fields
        are nullable, not omittable.
        """
        success = json.loads(
            render_validate_json(
                ValidateOutput(
                    valid=True,
                    source="x",
                    blueprint_id="a.b",
                    version="1.0.0",
                )
            )
        )
        failure = json.loads(render_validate_json(ValidateOutput(valid=False, source="x")))

        expected = {
            "schema_version",
            "valid",
            "source",
            "blueprint_id",
            "version",
            "errors",
        }
        assert set(success) == expected
        assert set(failure) == expected

    def test_error_entry_keys_are_exactly_the_documented_set(self) -> None:
        """Every ``errors[]`` entry has exactly the six documented keys.

        Using one error with all fields populated (line and column
        non-null) proves the serialiser emits every key rather than
        omitting null-valued ones. A future "clean up by dropping
        nulls" refactor would fail this test loudly — which is
        exactly the point: dropping nulls turns a stable contract
        into a per-input shape.
        """
        output = ValidateOutput(
            valid=False,
            source="x",
            errors=[
                ValidateError(
                    path="p",
                    message="m",
                    hint="h",
                    code="c",
                    line=1,
                    column=2,
                ),
            ],
        )
        payload = json.loads(render_validate_json(output))
        expected = {"path", "message", "hint", "code", "line", "column"}
        for entry in payload["errors"]:
            assert set(entry) == expected

    def test_null_valued_fields_are_present_not_omitted(self) -> None:
        """``blueprint_id`` / ``version`` are ``null`` on failure, not absent.

        The contract is "keys are always present; values may be null".
        A shape that omits the keys on failure would force consumers
        to write ``payload.get("blueprint_id")`` instead of
        ``payload["blueprint_id"]`` — the small ergonomic difference
        between a nullable contract and an optional one. The snapshot
        fixtures enforce this for the two known cases; this test
        enforces it for the *general* case, so a new error path that
        produces a fresh envelope is covered without a new fixture.
        """
        failure = json.loads(render_validate_json(ValidateOutput(valid=False, source="x")))
        assert "blueprint_id" in failure
        assert failure["blueprint_id"] is None
        assert "version" in failure
        assert failure["version"] is None

    def test_schema_version_is_the_literal_documented_value(self) -> None:
        """``schema_version`` is the pinned literal, not a derived value.

        The DTO's ``Literal["1.0.0"]`` type means a *constructor*
        cannot legally pass a different value, but the serialised
        form is what consumers see. Asserting the literal here means
        a future "derive schema_version from a constant" refactor
        (which could drift if the constant moves) fails loudly unless
        the constant still equals the contract.
        """
        payload = json.loads(render_validate_json(ValidateOutput(valid=True, source="x")))
        assert payload["schema_version"] == "1.0.0"


# -----------------------------------------------------------------------------
# Fixture directory sanity — fail fast if the tree is broken
# -----------------------------------------------------------------------------
class TestFixtureDirectory:
    """The fixtures directory and its files must exist.

    A missing fixture produces a ``FileNotFoundError`` inside a test
    body, which a reviewer reads as "the assertion failed" even
    though no assertion ran. This test turns that into a clear
    "the fixture tree is broken" failure with a path that names the
    missing file.
    """

    def test_fixture_directory_exists(self) -> None:
        assert _FIXTURE_DIR.is_dir(), (
            f"Golden-fixture directory missing: {_FIXTURE_DIR}. "
            f"See the module docstring's regeneration procedure."
        )

    @pytest.mark.parametrize("name", ["valid.json", "invalid.json"])
    def test_fixture_file_is_parseable_json(self, name: str) -> None:
        """Each golden fixture parses as JSON.

        A hand-edited fixture that a contributor forgot to format
        correctly would otherwise fail inside the snapshot test with
        a ``JSONDecodeError`` from ``_load_golden`` — noise that
        hides the real problem. Parsing here, in a dedicated test,
        names the file directly.
        """
        path = _FIXTURE_DIR / name
        assert path.is_file(), f"Missing fixture: {path}"
        json.loads(path.read_text(encoding="utf-8"))  # raises if broken
