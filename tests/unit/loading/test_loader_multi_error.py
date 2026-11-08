"""Unit tests for `BlueprintLoader.load` on multi-error documents.

What this suite must prove
--------------------------
The loader's central claim is **aggregation**: when a document has
several problems, the user gets *all* of them in one `Err([...])`,
not one-at-a-time across re-runs. That claim is the reason the loader
exists as a pipeline rather than a chain of `assert` calls, and it is
the reason the schema phase collects every violation instead of
raising on the first.

This suite pins four properties that together define "aggregation":

1. **Multiplicity.** Multiple violations produce multiple entries in
   `result.error`. A single-item list would mean the loader reported
   only the first problem — the exact behaviour the aggregation
   design exists to prevent.

2. **Completeness.** *Every* declared problem appears in the list.
   The test fixtures violate a known set of rules; the loader must
   report each one, not a subset. A refactor that silently dropped
   errors (e.g. deduped across phases by accident) would pass the
   multiplicity test but fail this one.

3. **Phase attribution.** Each error's path names the phase that
   produced it (`schema.*` vs `model.*`). This is what lets a
   maintainer distinguish "user wrote bad YAML" (schema) from "our
   models have drifted from the schema" (model). Without
   attribution, a parity regression looks identical to a user
   mistake.

4. **Precise location.** Each error's path names the *offending
   value*, not its parent. `jsonschema` reports `required` and
   `additionalProperties` violations at the parent object, with the
   field name in the message; the loader's aggregator promotes the
   field name into the path so the caller sees `/id` (missing `id`
   at document root), not `/` (the whole document is missing
   something). This is a small detail that matters enormously in a
   multi-error report: `/<root>` times 6 errors tells the user nothing;
   `/id`, `/version`, `/category` tells them everything.

Design note on ``len(...) == N`` vs ``len(...) >= N``
-----------------------------------------------------
Two of the tests in ``TestScopedAggregation`` use an exact count
(``== 2``) and one uses a lower bound (``>= 2``). The distinction
is deliberate:

* **Exact counts** are used where the test's *name* commits to a
  specific number ("two violations aggregate"). A fixture with
  three violations and an ``== 2`` assertion is a bug, not a
  fixture to loosen — the fixture is what drifted, not the
  assertion. Making the test ``>= 2`` would silently accept the
  drift and stop the test from proving the property it is named
  after.

* **Lower bounds** are used where the test is about a *class* of
  violation ("root AND nested both appear"). A future loader
  improvement that surfaces additional violations should not break
  the test; the test cares that both classes are represented, not
  that the total is a specific number.

When in doubt: pick the assertion whose failure message a future
maintainer would most want to read. "Expected exactly 2, got 3,
and here they are" is more useful than "expected at least 2, got 3"
when the extra error indicates the *fixture* drifted. Conversely,
"expected at least 2" is more useful than "expected exactly 2" when
the extra error is a legitimate new check.

Fixture strategy
----------------
`tests/fixtures/loader/multi-error.yaml` is shared with
`tests/integration/test_validate_command.py`: the CLI test needs a
document that produces a multi-error report so it can assert the
report format, and sharing the fixture guarantees the CLI test and
the loader test agree about what "multi-error" means. Both tests
assert the *same* set of violated fields, so a fixture change that
drops one violation fails both tests — which is the desired coupling
(a fixture change is a semantic change to the whole suite).

The other tests use inline YAML so their specific violation shapes
are visible in the test body. The shared fixture is the canonical
"three violations" case; the inline ones are the "specific property
under test" cases.
"""

from __future__ import annotations

from pathlib import Path

from forge.loading import BlueprintLoader, Err
from forge.loading.errors import ValidationError
from forge.loading.result import Ok

# Shared with tests/integration/test_validate_command.py.
_FIXTURES = Path(__file__).parents[2] / "fixtures" / "loader"
_MULTI_ERROR_FIXTURE = _FIXTURES / "multi-error.yaml"


def _load_bytes(tmp_path: Path, raw: bytes) -> Ok | Err:
    """Stage ``raw`` as a file and run the loader against it.

    Local helper for the same reason as in the other loader suites:
    the loader reads from disk, and staging a temp file per test keeps
    each test's body focused on the assertion rather than on
    file-writing boilerplate.
    """
    blueprint = tmp_path / "blueprint.yaml"
    blueprint.write_bytes(raw)
    return BlueprintLoader().load(blueprint)


# =============================================================================
# The shared fixture — three violations, all reported
# =============================================================================
class TestSharedMultiErrorFixture:
    """The canonical "three violations" case, loaded once per test class.

    Every test in this class starts from the same `Err` produced by
    the shared fixture. Constructing it in a pytest fixture method
    would work too, but pytest fixtures add a layer of indirection
    that would obscure the relationship between the fixture file's
    content and the assertions — and for a suite whose whole point
    is "here is exactly what the loader reports", that relationship
    is the thing a reader most needs to see.
    """

    def _result(self):
        return BlueprintLoader().load(_MULTI_ERROR_FIXTURE)

    # -- Multiplicity -----------------------------------------------------

    def test_returns_err(self):
        result = self._result()
        assert isinstance(result, Err)

    def test_reports_at_least_three_errors(self):
        """The fixture violates three fields; the loader must report all three.

        The assertion is ``>= 3``, not ``== 3``, because a future
        loader improvement could legitimately surface additional
        violations (e.g. adding a stricter filesystem check). The
        contract is "at least the known violations appear", not
        "exactly the known violations appear and nothing else" —
        the latter would be a snapshot test disguised as a
        correctness test, and would fail on any harmless improvement.
        """
        result = self._result()
        assert isinstance(result, Err)
        assert len(result.error) >= 3

    def test_every_error_is_a_validation_error(self):
        """No stray types, no ``None``, no strings.

        The CLI's renderer iterates ``result.error`` and calls
        ``.format()``. If a future refactor mixed in a raw
        ``jsonschema.ValidationError`` (e.g. before translation),
        the renderer would crash — this test catches that before the
        CLI does.
        """
        result = self._result()
        assert isinstance(result, Err)
        for error in result.error:
            assert isinstance(
                error, ValidationError
            ), f"non-ValidationError in aggregate: {type(error).__name__}"

    # -- Completeness -----------------------------------------------------

    def test_reports_missing_id(self):
        result = self._result()
        assert isinstance(result, Err)
        paths = [e.path for e in result.error]
        assert any(
            p.endswith("id") for p in paths
        ), f"missing-id violation not reported; paths: {paths}"

    def test_reports_invalid_version(self):
        result = self._result()
        assert isinstance(result, Err)
        paths = [e.path for e in result.error]
        assert any(
            p.endswith("version") for p in paths
        ), f"invalid-version violation not reported; paths: {paths}"

    def test_reports_invalid_category(self):
        result = self._result()
        assert isinstance(result, Err)
        paths = [e.path for e in result.error]
        assert any(
            p.endswith("category") for p in paths
        ), f"invalid-category violation not reported; paths: {paths}"

    # -- Phase attribution ------------------------------------------------

    def test_all_errors_carry_phase_prefix(self):
        """Every error's path begins with a phase name.

        The loader's aggregator prefixes every error with its phase
        (``schema.`` or ``model.``). A refactor that removed the
        prefix would still pass "reports three errors" tests, but a
        maintainer could no longer tell which validator rejected the
        document — which is exactly the information needed to
        distinguish user error from Forge bug (ADR-0007 vs ADR-0008).
        """
        result = self._result()
        assert isinstance(result, Err)
        prefixes = ("schema.", "model.")
        for error in result.error:
            assert error.path.startswith(
                prefixes
            ), f"error path {error.path!r} missing phase prefix"

    def test_schema_violations_are_attributed_to_schema_phase(self):
        """All three fixture violations are schema-shape errors, so all
        three must carry the ``schema.`` prefix.

        If any carries ``model.``, the pipeline is running the model
        phase on a document the schema has already rejected — which
        would produce duplicate errors with different prefixes and
        confuse the "which validator rejected this?" question.
        """
        result = self._result()
        assert isinstance(result, Err)
        for error in result.error:
            assert error.path.startswith("schema."), (
                f"expected schema-phase error, got {error.path!r}. "
                f"This suggests the model phase ran on a schema-invalid "
                f"document, which the loader should have short-circuited."
            )

    # -- Precise location -------------------------------------------------

    def test_missing_id_path_is_precise(self):
        """The missing-``id`` error's path ends with ``id``, not with
        just the phase prefix.

        ``jsonschema`` reports ``required`` violations at the *parent*
        object's path, with the field name in the message. The
        loader's aggregator promotes the field name into the path so
        the user sees ``schema.id`` (or, if the parent is the root,
        ``schema.id`` too — the exact form is the aggregator's
        concern; what matters is that ``id`` appears in the path
        somewhere beyond the prefix).
        """
        result = self._result()
        assert isinstance(result, Err)
        missing_id_errors = [
            e for e in result.error if e.path.startswith("schema.") and e.path.endswith("id")
        ]
        assert missing_id_errors, (
            f"no error path ends with 'id'; paths: " f"{[e.path for e in result.error]}"
        )

    # -- Actionability ----------------------------------------------------

    def test_every_error_has_a_hint(self):
        """The funnel's KPI: every error carries a fix suggestion."""
        result = self._result()
        assert isinstance(result, Err)
        for error in result.error:
            assert error.hint, f"empty hint on {error.path!r}"

    def test_every_error_has_a_message(self):
        result = self._result()
        assert isinstance(result, Err)
        for error in result.error:
            assert error.message, f"empty message on {error.path!r}"

    def test_every_error_names_the_fixture_as_source(self):
        """The ``source`` field identifies the file, not a stub.

        A user with three errors in two blueprints needs the source
        to know which file to open. A stub label (``<memory>``,
        empty string) would leave that ambiguous.
        """
        result = self._result()
        assert isinstance(result, Err)
        expected = str(_MULTI_ERROR_FIXTURE)
        for error in result.error:
            assert error.source == expected, (
                f"error source {error.source!r} does not match the " f"fixture path {expected!r}"
            )


# =============================================================================
# Ordering — deterministic output across runs
# =============================================================================
class TestDeterministicOrdering:
    """The error list is identical across two loads of the same file.

    Why this matters
    ----------------
    Deterministic ordering means a user re-running ``forge validate``
    sees the same output twice, which is a minimum requirement for
    scripts that parse the output and for diffing two versions of
    the same file. It also means a test can assert *specific*
    positions rather than a set, which makes a failing test's
    message much more readable ("expected path 'schema.id' at
    index 0, got 'schema.category'" rather than "expected some
    permutation to contain 'schema.id'").

    Determinism is provided by the aggregator's sort (``sorted`` by
    ``(pointer, message)``); this test pins the observable result.
    """

    def test_two_loads_produce_identical_path_lists(self):
        first = BlueprintLoader().load(_MULTI_ERROR_FIXTURE)
        second = BlueprintLoader().load(_MULTI_ERROR_FIXTURE)
        assert isinstance(first, Err)
        assert isinstance(second, Err)
        assert [e.path for e in first.error] == [e.path for e in second.error]

    def test_two_loads_produce_identical_message_lists(self):
        first = BlueprintLoader().load(_MULTI_ERROR_FIXTURE)
        second = BlueprintLoader().load(_MULTI_ERROR_FIXTURE)
        assert isinstance(first, Err)
        assert isinstance(second, Err)
        assert [e.message for e in first.error] == [e.message for e in second.error]


# =============================================================================
# Scoped aggregation — inline multi-error cases
# =============================================================================
class TestScopedAggregation:
    """Aggregation works for *any* set of violations, not just the
    three the shared fixture happens to contain.

    The shared fixture proves aggregation for the schema phase on
    three specific fields. These inline cases prove the same
    aggregation behaviour for combinations the shared fixture does
    not cover:

    - Nested violations (variables, files) — these exercise
      `_format_pointer`'s array-index handling and the aggregator's
      recursive path construction.
    - Combination of root-level and nested violations — these prove
      aggregation is not per-level.
    - A single violation — this is the trivial case where
      "aggregation" degenerates to "return one error", but proving
      it explicitly prevents a future refactor from adding extra
      noise on single-error documents (e.g. a summary line as a
      fake error).

    Note on assertions: tests whose *name* commits to a specific
    count (``test_two_root_level_violations_aggregate``) use an
    exact ``==`` assertion and are paired with a fixture that
    produces exactly that count. Tests whose name describes a
    *class* of violation (``test_root_and_nested_violations_aggregate``)
    use ``>=`` because the test cares about presence, not total.
    """

    def test_single_violation_produces_exactly_one_error(self, tmp_path: Path):
        """The trivial case: aggregation of one error is one error."""
        raw = b"""
schema_version: "1.0.0"
id: sample.ok
name: OK
version: "1.0"
description: d
category: api
files: []
verification: [{ id: ok, type: required_file, path: README.md }]
compatibility: { min_forge_version: "1.0.0" }
"""
        result = _load_bytes(tmp_path, raw)
        assert isinstance(result, Err)
        assert len(result.error) == 1
        assert result.error[0].path.endswith("version")

    def test_two_root_level_violations_aggregate(self, tmp_path: Path):
        """Two violations at the same nesting level aggregate.

        The fixture must contain exactly two violations — hence the
        explicit ``id:`` field, which makes the ``id`` requirement
        satisfied and leaves only ``version`` and ``category`` as
        violations. Two gotchas matter here:

        * **Omitting ``id`` adds a violation.** A fixture without
          ``id:`` has three violations (missing id, invalid version,
          invalid category), not two.
        * **An invalid ``id`` adds a violation.** The schema's id
          pattern is ``^[a-z][a-z0-9]*(\\.[a-z][a-z0-9]*)+$`` — no
          hyphens, no underscores, digits allowed. ``sample.two-errors``
          and ``sample.two_errors`` are both *invalid*; ``sample.twoerrors``
          is valid. This is not an obvious rule, and a fixture
          author who chooses a "natural-looking" id may accidentally
          substitute one violation for another.
        """
        raw = b"""
schema_version: "1.0.0"
id: sample.twoerrors
name: Two Errors
version: "1.0"
description: d
category: also-wrong
files: []
verification: [{ id: ok, type: required_file, path: README.md }]
compatibility: { min_forge_version: "1.0.0" }
"""
        result = _load_bytes(tmp_path, raw)
        assert isinstance(result, Err)
        assert len(result.error) == 2
        paths = [e.path for e in result.error]
        assert any(p.endswith("version") for p in paths)
        assert any(p.endswith("category") for p in paths)

    def test_root_and_nested_violations_aggregate(self, tmp_path: Path):
        """Root-level and nested violations appear in the same list.

        This is the case that proves aggregation is *not* per-level:
        a naive implementation might report root violations first and
        require a second pass for nested ones. The loader's single
        pass collects both.

        Uses ``>=`` because the point is *presence of both classes*,
        not a specific count. A future loader check that adds a
        fourth violation (e.g. a stricter `description` rule) would
        make a ``== 2`` assertion fail on a legitimate improvement.
        """
        raw = b"""
schema_version: "1.0.0"
id: sample.root-and-nested
name: Mixed
version: "1.0"
description: d
category: api
files: []
verification: [{ id: ok, type: required_file, path: README.md }]
compatibility: { min_forge_version: "1.0.0" }
variables:
  - id: bad_type_var
    type: integer
    description: d
    required: true
"""
        result = _load_bytes(tmp_path, raw)
        assert isinstance(result, Err)
        assert len(result.error) >= 2
        paths = [e.path for e in result.error]
        assert any(p.endswith("version") for p in paths), paths
        assert any("variables" in p and p.endswith("type") for p in paths), paths


# =============================================================================
# Unknown field — the additionalProperties case
# =============================================================================
class TestUnknownField:
    """Unknown top-level fields are rejected with a precise pointer.

    The schema declares ``additionalProperties: false`` at the top
    level, so a document with an unrecognised field is invalid. This
    is a deliberate strictness: silently ignoring extra fields is a
    footgun for blueprint authors, who reasonably expect their
    custom field to be meaningful — a silent ignore means they ship
    a blueprint that does nothing with the field, and they find out
    (if ever) by reading the loader's source.

    The precision test matters here more than usual: the
    ``additionalProperties`` keyword is one of the two that
    ``jsonschema`` reports at the *parent* path, and the loader's
    aggregator must promote the field name from the message into
    the path. Without that promotion, the user would see
    ``schema.<root>`` (the whole document is somehow wrong) instead
    of ``schema.unknown_field`` (this specific field is not
    allowed).
    """

    def test_unknown_field_is_rejected(self, tmp_path: Path):
        raw = b"""
schema_version: "1.0.0"
id: sample.unknown
name: Unknown
version: "1.0.0"
description: d
category: api
unknown_field: not-allowed
files: []
verification: [{ id: ok, type: required_file, path: README.md }]
compatibility: { min_forge_version: "1.0.0" }
"""
        result = _load_bytes(tmp_path, raw)
        assert isinstance(result, Err)

    def test_unknown_field_path_names_the_field(self, tmp_path: Path):
        """The error path names ``unknown_field``, not just the phase.

        This is the assertion that proves the aggregator's
        ``additionalProperties`` handling works — it promotes the
        field name from the message into the path.
        """
        raw = b"""
schema_version: "1.0.0"
id: sample.unknown
name: Unknown
version: "1.0.0"
description: d
category: api
unknown_field: not-allowed
files: []
verification: [{ id: ok, type: required_file, path: README.md }]
compatibility: { min_forge_version: "1.0.0" }
"""
        result = _load_bytes(tmp_path, raw)
        assert isinstance(result, Err)
        # The field name appears somewhere in the path of at least
        # one error. The exact form (``schema.unknown_field`` vs
        # some other encoding) is the aggregator's concern; what the
        # user needs is that the name is in the path.
        matched = any("unknown_field" in e.path for e in result.error)
        assert matched, (
            f"unknown-field violation not located in path; paths: "
            f"{[e.path for e in result.error]}"
        )


# =============================================================================
# Multiple-error messages remain informative
# =============================================================================
class TestMessageDistinctness:
    """Each error's message describes *its* violation, not a generic one.

    A subtle failure mode: if the aggregator reused a shared message
    template (e.g. "invalid value"), a multi-error report would have
    N identical messages attached to N different paths. The user
    would see *where* each error is but not *what* is wrong with
    each — a report that is only half useful.

    This test asserts that the messages are distinct — that each
    describes its specific violation. It does not assert *which*
    message goes with *which* path (that is the model's contract,
    tested in `test_model_error_messages.py`); it asserts only that
    the loader faithfully carries distinct messages through
    aggregation.
    """

    def test_multi_error_messages_are_distinct(self):
        result = BlueprintLoader().load(_MULTI_ERROR_FIXTURE)
        assert isinstance(result, Err)
        messages = [e.message for e in result.error]
        # At least three distinct messages for at least three
        # distinct violations. A future stricter loader check could
        # add more; the assertion is a lower bound.
        assert len(set(messages)) >= 3, (
            f"expected ≥3 distinct messages for ≥3 distinct "
            f"violations; got {len(set(messages))}: {messages}"
        )

    def test_multi_error_hints_are_present_for_each_distinct_violation(self):
        """Each distinct violation gets its own hint.

        The hints catalogue (`loading/hints.py`) matches on path
        shape; a bug that produced the default hint for every field
        would pass a "hint is non-empty" test but leave the user
        with a generic "see the docs" pointer for every error — the
        opposite of the actionable hint the funnel promises.
        """
        result = BlueprintLoader().load(_MULTI_ERROR_FIXTURE)
        assert isinstance(result, Err)
        hints = {e.path: e.hint for e in result.error}
        # At least two of the three known violations must have
        # *different* hints, proving the catalogue matched them by
        # path rather than falling through to the default for all.
        distinct_hints = set(hints.values())
        assert len(distinct_hints) >= 2, (
            f"expected path-specific hints for the known violations; "
            f"got {len(distinct_hints)} distinct hint(s): {hints}"
        )
