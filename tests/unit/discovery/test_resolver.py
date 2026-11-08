"""Unit tests for ``forge.discovery.resolver``.

Scope
-----
The resolver is the *single referee* for blueprint discovery policy:

* **Precedence** — local beats official when two sources offer the
  same id.
* **Deduplication** — the catalogue contains one entry per id.
* **Warning emission** — collisions and malformed candidates are
  logged once, with both sides named.
* **Malformed skipping** — one bad file never takes down the
  catalogue.
* **Stable projection** — ``to_metadata`` returns the catalogue
  sorted by id.

Every other module in the discovery package defers these decisions to
``resolver.py``. Sources *propose*; the resolver *disposes*. That is
the entire design rationale documented in ADR-0009, and this file
exists to make any break to it loud and immediate.

What is tested here
-------------------
* ``_PRECEDENCE`` — the numeric table is exposed, stable, and
  ordered as documented (local > official).
* ``resolve()`` — deduplicates by id using the table, warns on both
  winning and losing collisions, skips malformed candidates with a
  warning, and returns unordered winners (sorting is the projection's
  job).
* ``to_metadata()`` — projects to ``DiscoveredBlueprint``, sorted by
  id, with the correct source tag.
* ``_parse()`` — converts every failure mode (empty bytes, YAML
  syntax, non-mapping root, schema violation, model violation) into
  a single ``MalformedBlueprintError`` so the resolver has one
  branch to reason about.

What is *not* tested here
-------------------------
* Schema and model validation logic — those are tested in
  ``tests/unit/schemas`` and ``tests/unit/models``. ``_parse`` is
  tested here only for its *translation* behaviour (does it
  normalize every failure into ``MalformedBlueprintError``?), not for
  the correctness of the validation it delegates to.
* Source I/O — tested in ``test_sources.py``.
* Service-level orchestration — tested in ``test_service.py``.

Design invariants this file pins
--------------------------------
1. **Precedence is data, not code.** ``_PRECEDENCE`` is a module-level
   dict; changing the rule is a one-line edit. This file asserts both
   the numeric values and the resulting behaviour, so a change to the
   table that does not update the tests is caught in CI.
2. **The winner is deterministic.** When two candidates tie on
   precedence (which cannot happen today — there are two values — but
   is possible if a future source is added at the same priority),
   the first-seen wins. That is what "the resolver's input order
   determines tie-breaking" means in practice.
3. **Warnings name both sides.** A duplicate warning that says
   "duplicate blueprint" without naming the winner and the loser is a
   puzzle; the user needs both to know which file to delete. The
   tests assert on both label substrings, not just on the count of
   warnings.
4. **Malformed is skipped, not fatal.** A single bad candidate must
   not take down the catalogue. This is tested both by asserting the
   warning fires and by asserting the good candidates still appear in
   the result.
5. **Ordering is a projection concern.** ``resolve`` returns an
   unordered list; ``to_metadata`` sorts. Splitting the two means
   ``resolve`` can be used by future callers (a validator, a
   collision reporter) that want to inspect the natural order without
   paying for a sort they do not need.
"""

from __future__ import annotations

import logging

import pytest

from forge.discovery.errors import MalformedBlueprintError
from forge.discovery.resolver import (
    _PRECEDENCE,
    ResolvedCandidate,
    _parse,
    resolve,
    to_metadata,
)
from forge.discovery.sources import Candidate
from forge.models import Blueprint
from forge.models.blueprint import BlueprintSource, DiscoveredBlueprint

# -----------------------------------------------------------------------------
# Fixtures and helpers
# -----------------------------------------------------------------------------
# A minimal valid manifest template. Shared by every fixture so a
# schema change has to update one string, and so the tests stay
# focused on resolver behaviour rather than on "is this document
# valid?" — that question has its own suite.
_VALID_MANIFEST_TEMPLATE = """\
schema_version: "1.0.0"
id: {id}
name: {name}
version: 1.0.0
description: resolver fixture
category: api
verification:
  - id: ok
    type: required_file
    path: README.md
compatibility:
  min_forge_version: 1.0.0
"""


def _manifest(blueprint_id: str, name: str = "Fixture") -> bytes:
    """Return a valid manifest as UTF-8 bytes."""
    return _VALID_MANIFEST_TEMPLATE.format(id=blueprint_id, name=name).encode("utf-8")


def _candidate(
    blueprint_id: str,
    name: str = "Fixture",
    *,
    origin: BlueprintSource = BlueprintSource.OFFICIAL,
    label: str | None = None,
) -> Candidate:
    """Build a well-formed candidate with a syntactically valid manifest.

    ``label`` defaults to a plausible provenance string so failure
    messages from ``_parse`` look like real ones. Passing an explicit
    label lets a test control what appears in a warning assertion
    without depending on the manifest content — the label and the
    manifest's id are independent in the resolver's view.
    """
    return (
        label or f"{origin.value}:{blueprint_id}",
        _manifest(blueprint_id, name),
        origin,
    )


def _malformed_candidate(
    *,
    origin: BlueprintSource = BlueprintSource.LOCAL,
    label: str = "local:broken",
) -> Candidate:
    """Build a candidate whose bytes are not a valid manifest."""
    return (label, b"not: a: mapping: at: the: root", origin)


# -----------------------------------------------------------------------------
# _PRECEDENCE — the rule, as data
# -----------------------------------------------------------------------------
class TestPrecedenceTable:
    """The precedence rule is a module-level constant, not scattered logic."""

    @pytest.mark.unit()
    def test_table_has_exactly_two_entries(self) -> None:
        """The current design has two layers; a third is a new ADR.

        A test that only checked the numeric values would let a future
        contributor add a third layer without updating the ADR, the
        documentation, or this test. Asserting the length forces the
        conversation: adding a layer means updating the test (which
        is fine) and that update is the trigger for updating the docs.
        """
        assert len(_PRECEDENCE) == 2

    @pytest.mark.unit()
    def test_local_beats_official(self) -> None:
        """Local has higher precedence than official.

        This is *the* rule. Everything else in the discovery package
        exists to enforce it correctly. If this test fails, either
        the table was reversed (a serious bug — users' overrides stop
        working) or a third layer was added and the comparison is
        incomplete.
        """
        assert _PRECEDENCE[BlueprintSource.LOCAL] > _PRECEDENCE[BlueprintSource.OFFICIAL]

    @pytest.mark.unit()
    def test_values_are_integers(self) -> None:
        """The values are comparable with ``>``, not strings or enums.

        ``resolve`` compares precedence with ``>``. If a future
        contributor changes the values to strings (say, "high"/"low"),
        the comparison silently changes semantics. Asserting the type
        keeps the comparison meaningful.
        """
        for value in _PRECEDENCE.values():
            assert isinstance(value, int)


# -----------------------------------------------------------------------------
# resolve() — happy path
# -----------------------------------------------------------------------------
class TestResolveHappyPath:
    """``resolve`` accepts valid candidates without warning."""

    @pytest.mark.unit()
    def test_returns_every_candidate_for_distinct_ids(self) -> None:
        """Distinct ids all survive resolution."""
        candidates = [
            _candidate("python.fastapi.api"),
            _candidate("python.automation.cli"),
            _candidate("typescript.frontend.react", origin=BlueprintSource.LOCAL),
        ]

        resolved = resolve(candidates)

        assert {r.blueprint.id for r in resolved} == {
            "python.fastapi.api",
            "python.automation.cli",
            "typescript.frontend.react",
        }

    @pytest.mark.unit()
    def test_returns_resolved_candidate_objects(self) -> None:
        """Each item is a ``ResolvedCandidate`` with blueprint, label, origin.

        The service consumes this shape directly; a refactor that
        returned tuples or plain ``Blueprint`` instances would break
        the provenance data the CLI uses in ``forge list``.
        """
        resolved = resolve([_candidate("python.fastapi.api")])

        assert len(resolved) == 1
        item = resolved[0]
        assert isinstance(item, ResolvedCandidate)
        assert isinstance(item.blueprint, Blueprint)
        assert item.blueprint.id == "python.fastapi.api"
        assert item.source_label == "official:python.fastapi.api"
        assert item.origin is BlueprintSource.OFFICIAL

    @pytest.mark.unit()
    def test_empty_input_returns_empty_list(self) -> None:
        """An empty candidate stream yields an empty catalogue, not an error."""
        assert resolve([]) == []

    @pytest.mark.unit()
    def test_no_warnings_on_clean_input(self, caplog: pytest.LogCaptureFixture) -> None:
        """Valid, non-duplicate candidates produce no warnings."""
        with caplog.at_level(logging.WARNING, logger="forge.discovery"):
            resolve([_candidate("python.fastapi.api")])

        assert caplog.records == []


# -----------------------------------------------------------------------------
# resolve() — precedence
# -----------------------------------------------------------------------------
class TestResolvePrecedence:
    """``resolve`` correctly applies the ``_PRECEDENCE`` table."""

    @pytest.mark.unit()
    def test_local_overrides_official_regardless_of_input_order(self) -> None:
        """Local wins whether it appears before or after the official one.

        The precedence rule is a property of the *sources*, not of
        input order. A naive implementation that "kept the first"
        would fail half of this test depending on which order the
        sources happened to run in — a bug that would surface as
        inconsistent user reports ("sometimes my override works").
        """
        for candidates in (
            [
                _candidate("python.fastapi.api"),
                _candidate("python.fastapi.api", origin=BlueprintSource.LOCAL),
            ],
            [
                _candidate("python.fastapi.api", origin=BlueprintSource.LOCAL),
                _candidate("python.fastapi.api"),
            ],
        ):
            resolved = resolve(candidates)
            assert len(resolved) == 1
            assert resolved[0].origin is BlueprintSource.LOCAL

    @pytest.mark.unit()
    def test_official_never_overrides_local(self) -> None:
        """The rule does not accidentally reverse.

        A future contributor trying to "simplify" precedence with
        ``min(...)`` instead of ``max(...)`` would pass the symmetry
        test above but invert the rule. This test catches that
        specifically.
        """
        candidates = [
            _candidate("python.fastapi.api", origin=BlueprintSource.LOCAL),
            _candidate("python.fastapi.api", origin=BlueprintSource.OFFICIAL),
        ]

        resolved = resolve(candidates)

        assert resolved[0].origin is BlueprintSource.LOCAL

    @pytest.mark.unit()
    def test_returns_winning_blueprint_not_losing_blueprint(self) -> None:
        """The winning *document* is returned, not just the winning origin.

        The label and origin are bookkeeping; the ``blueprint`` field
        is what the caller actually uses. If a refactor picked the
        right origin but kept the wrong document, callers would get
        the official manifest's name, description, and variables
        while ``forge list`` showed ``[local]`` next to them — a
        highly confusing bug. Asserting on ``.name`` here ties the
        two together.
        """
        candidates = [
            _candidate("python.fastapi.api", "Official Name"),
            _candidate("python.fastapi.api", "Local Name", origin=BlueprintSource.LOCAL),
        ]

        resolved = resolve(candidates)

        assert len(resolved) == 1
        assert resolved[0].blueprint.name == "Local Name"


# -----------------------------------------------------------------------------
# resolve() — duplicate warnings
# -----------------------------------------------------------------------------
class TestResolveDuplicateWarnings:
    """Duplicate ids produce exactly one warning naming both sides."""

    @pytest.mark.unit()
    def test_winner_wins_warning_names_both_sides(self, caplog: pytest.LogCaptureFixture) -> None:
        """When local overrides official, the warning names both files.

        The user needs to know *which file* to delete to stop the
        warning. A message that says only "duplicate found" leaves them
        searching. A message that names both the winner and loser lets
        them find the loser and act.
        """
        winner = _candidate(
            "python.fastapi.api",
            "Local",
            origin=BlueprintSource.LOCAL,
            label="local:/abs/path",
        )
        loser = _candidate(
            "python.fastapi.api",
            "Official",
            origin=BlueprintSource.OFFICIAL,
            label="bundled:python.fastapi.api",
        )

        with caplog.at_level(logging.WARNING, logger="forge.discovery"):
            resolve([loser, winner])

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        message = warnings[0].getMessage()
        assert "local:/abs/path" in message
        assert "bundled:python.fastapi.api" in message
        assert "python.fastapi.api" in message

    @pytest.mark.unit()
    def test_loser_wins_warning_names_both_sides(self, caplog: pytest.LogCaptureFixture) -> None:
        """When the official candidate arrives second, the warning still names both.

        The warning fires once per collision, in whichever direction
        it is discovered. A test that only exercised the "local
        arrives second" case would leave the branch where "local
        arrives first and official loses" untested — and that branch
        has its own log message format.
        """
        winner = _candidate(
            "python.fastapi.api",
            "Local",
            origin=BlueprintSource.LOCAL,
            label="local:/abs/path",
        )
        loser = _candidate(
            "python.fastapi.api",
            "Official",
            origin=BlueprintSource.OFFICIAL,
            label="bundled:python.fastapi.api",
        )

        with caplog.at_level(logging.WARNING, logger="forge.discovery"):
            resolve([winner, loser])

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        message = warnings[0].getMessage()
        assert "local:/abs/path" in message
        assert "bundled:python.fastapi.api" in message

    @pytest.mark.unit()
    def test_only_one_warning_per_collision(self, caplog: pytest.LogCaptureFixture) -> None:
        """A single collision produces a single warning, not two.

        The resolver logs exactly once per collision, after deciding
        the winner. If a future refactor logged both "found duplicate"
        *and* "decided winner", users would see two warnings for one
        problem — noise that trains them to ignore warnings.
        """
        candidates = [
            _candidate("python.fastapi.api"),
            _candidate("python.fastapi.api", origin=BlueprintSource.LOCAL),
        ]

        with caplog.at_level(logging.WARNING, logger="forge.discovery"):
            resolve(candidates)

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1


# -----------------------------------------------------------------------------
# resolve() — malformed skipping
# -----------------------------------------------------------------------------
class TestResolveMalformed:
    """Malformed candidates are skipped with a warning, never fatal."""

    @pytest.mark.unit()
    def test_malformed_is_skipped_and_warned(self, caplog: pytest.LogCaptureFixture) -> None:
        """A malformed candidate triggers exactly one warning and is not returned."""
        with caplog.at_level(logging.WARNING, logger="forge.discovery"):
            resolved = resolve([_malformed_candidate()])

        assert resolved == []
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "Skipping malformed blueprint" in warnings[0].getMessage()
        assert "local:broken" in warnings[0].getMessage()

    @pytest.mark.unit()
    def test_malformed_does_not_affect_other_candidates(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """One bad file does not prevent the good ones from loading.

        This is the "one bad file never takes down the catalogue"
        invariant (ADR-0009). Asserting only on the warning would
        pass if the resolver warned *and then raised* — the test
        needs to also assert the good candidate survives.
        """
        with caplog.at_level(logging.WARNING, logger="forge.discovery"):
            resolved = resolve(
                [
                    _malformed_candidate(),
                    _candidate("python.fastapi.api"),
                ]
            )

        assert len(resolved) == 1
        assert resolved[0].blueprint.id == "python.fastapi.api"

    @pytest.mark.unit()
    def test_each_malformed_candidate_gets_its_own_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Every malformed file is named, not just the first.

        A user with three broken local blueprints needs to fix all
        three, not chase them one warning at a time. The resolver
        warns per candidate, and each warning names its file via the
        label.
        """
        with caplog.at_level(logging.WARNING, logger="forge.discovery"):
            resolve(
                [
                    _malformed_candidate(label="local:first"),
                    _malformed_candidate(label="local:second"),
                ]
            )

        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 2
        assert any("local:first" in m for m in warnings)
        assert any("local:second" in m for m in warnings)


# -----------------------------------------------------------------------------
# _parse() — error translation
# -----------------------------------------------------------------------------
class TestParseTranslation:
    """Every failure mode becomes ``MalformedBlueprintError``."""

    @pytest.mark.unit()
    def test_parse_accepts_valid_manifest(self) -> None:
        """A valid manifest parses into a ``ResolvedCandidate``."""
        resolved = _parse(_candidate("python.fastapi.api"))

        assert resolved.blueprint.id == "python.fastapi.api"
        assert resolved.origin is BlueprintSource.OFFICIAL

    @pytest.mark.unit()
    def test_empty_bytes_becomes_malformed_error(self) -> None:
        """The empty-bytes candidate the sources yield is translated.

        Sources signal "unreadable file" as ``b""``; the resolver
        must turn that into a ``MalformedBlueprintError`` so the
        warning path handles it identically to a syntax error. A
        refactor that special-cased empty bytes outside the malformed
        branch would produce two warning formats for the same user-
        facing problem.
        """
        with pytest.raises(MalformedBlueprintError) as info:
            _parse(("local:empty", b"", BlueprintSource.LOCAL))

        assert "empty" in str(info.value).lower()

    @pytest.mark.unit()
    def test_invalid_yaml_becomes_malformed_error(self) -> None:
        """YAML syntax errors are wrapped in ``MalformedBlueprintError``."""
        with pytest.raises(MalformedBlueprintError) as info:
            _parse(("local:bad", b"not: a: valid: yaml:", BlueprintSource.LOCAL))

        assert "YAML" in str(info.value)

    @pytest.mark.unit()
    def test_non_mapping_root_becomes_malformed_error(self) -> None:
        """A document that is not a mapping is rejected with a clear message.

        ``yaml.safe_load`` returns ``None`` for an empty document and
        a scalar for a bare value. Both are blueprint-invalid; the
        message must name the failure ("expected a mapping"), not the
        Python type, so the user knows what to fix.
        """
        with pytest.raises(MalformedBlueprintError) as info:
            _parse(("local:scalar", b"just a string", BlueprintSource.LOCAL))

        assert "mapping" in str(info.value).lower()

    @pytest.mark.unit()
    def test_schema_violation_becomes_malformed_error(self) -> None:
        """A schema-invalid document is translated from ``SchemaValidationError``.

        The resolver wraps the schema's structured error in
        ``MalformedBlueprintError`` so it has one skip-and-warn branch.
        The underlying schema pointers still appear in the wrapped
        message — the user still sees ``/id: ...`` — but the resolver
        does not need to know about ``StructuredError``.
        """
        # A manifest with an obviously invalid ``id`` (uppercase,
        # not the required dotted form). The schema rejects it; the
        # resolver should wrap it without surfacing a raw
        # ``SchemaValidationError`` to the caller.
        body = (
            b'schema_version: "1.0.0"\n'
            b"id: NOTVALID\n"
            b"name: x\n"
            b"version: 1.0.0\n"
            b"description: x\n"
            b"category: api\n"
            b"verification:\n"
            b"  - id: ok\n"
            b"    type: required_file\n"
            b"    path: README.md\n"
            b"compatibility:\n"
            b"  min_forge_version: 1.0.0\n"
        )

        with pytest.raises(MalformedBlueprintError):
            _parse(("local:invalid", body, BlueprintSource.LOCAL))

    @pytest.mark.unit()
    def test_label_is_preserved_in_the_wrapped_error(self) -> None:
        """The label passed to ``_parse`` appears verbatim in the error.

        Warnings name the file to fix; the label is the only place
        the file path is carried. If it were dropped during
        translation, every malformed warning would point at nothing.
        """
        with pytest.raises(MalformedBlueprintError) as info:
            _parse(("local:/very/specific/path", b"", BlueprintSource.LOCAL))

        assert "local:/very/specific/path" in str(info.value)


# -----------------------------------------------------------------------------
# to_metadata() — projection
# -----------------------------------------------------------------------------
class TestToMetadata:
    """``to_metadata`` projects to ``DiscoveredBlueprint`` and sorts by id."""

    @pytest.mark.unit()
    def test_projects_to_discovered_blueprint(self) -> None:
        """The output type is the catalogue view, not the full document."""
        resolved = resolve([_candidate("python.fastapi.api", "FastAPI")])

        projections = to_metadata(resolved)

        assert len(projections) == 1
        assert isinstance(projections[0], DiscoveredBlueprint)
        assert projections[0].id == "python.fastapi.api"
        assert projections[0].name == "FastAPI"

    @pytest.mark.unit()
    def test_output_is_sorted_by_id(self) -> None:
        """The catalogue is sorted by id regardless of input order.

        ``forge list`` output is diff-able in CI because of this sort;
        a resolver that returned input order would make the CLI's
        output unstable and snapshot tests would flake. The sort is
        the service's contract, but it lives here — asserting it here
        pins the behaviour at its definition site.
        """
        resolved = resolve(
            [
                _candidate("zzz.last"),
                _candidate("python.fastapi.api"),
                _candidate("aaa.first"),
            ]
        )

        ids = [bp.id for bp in to_metadata(resolved)]

        assert ids == ["aaa.first", "python.fastapi.api", "zzz.last"]

    @pytest.mark.unit()
    def test_source_is_preserved_from_resolved_candidate(self) -> None:
        """The ``source`` field on the projection comes from the winner.

        A projection that defaulted to ``OFFICIAL`` would lie about
        local overrides — the exact field users rely on to find
        overrides to delete.
        """
        resolved = resolve(
            [
                _candidate("python.fastapi.api", origin=BlueprintSource.OFFICIAL),
                _candidate("python.automation.cli", origin=BlueprintSource.LOCAL),
            ]
        )

        by_id = {bp.id: bp for bp in to_metadata(resolved)}

        assert by_id["python.fastapi.api"].source is BlueprintSource.OFFICIAL
        assert by_id["python.automation.cli"].source is BlueprintSource.LOCAL

    @pytest.mark.unit()
    def test_empty_input_produces_empty_output(self) -> None:
        """An empty catalogue is a valid projection."""
        assert to_metadata([]) == []


# -----------------------------------------------------------------------------
# Integration — resolve and to_metadata together
# -----------------------------------------------------------------------------
class TestResolveThenProject:
    """End-to-end sanity: the two halves agree with each other."""

    @pytest.mark.unit()
    def test_full_pipeline_with_collision_and_malformed(self) -> None:
        """The catalogue reflects both deduplication and skip behaviour.

        A test that only exercised ``resolve`` or only exercised
        ``to_metadata`` would miss a divergence between them (for
        example, if ``to_metadata`` re-resolved from the wrong
        collection). Composing the two asserts the full pipeline
        produces the shape the service actually returns.
        """
        candidates = [
            _candidate("python.fastapi.api", "Official"),
            _candidate("python.fastapi.api", "Local", origin=BlueprintSource.LOCAL),
            _malformed_candidate(label="local:broken"),
            _candidate("typescript.frontend.react", "TS", origin=BlueprintSource.OFFICIAL),
        ]

        catalogue = to_metadata(resolve(candidates))
        by_id = {bp.id: bp for bp in catalogue}

        assert [bp.id for bp in catalogue] == [
            "python.fastapi.api",
            "typescript.frontend.react",
        ]
        assert by_id["python.fastapi.api"].name == "Local"
        assert by_id["python.fastapi.api"].source is BlueprintSource.LOCAL
        assert by_id["typescript.frontend.react"].source is BlueprintSource.OFFICIAL
