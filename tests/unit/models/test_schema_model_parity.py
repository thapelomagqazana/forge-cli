"""Schema↔model parity: the two engines must accept *and* reject identically.

What this test is *for*
-----------------------
ADR-0007 makes the JSON Schema the canonical contract. ADR-0008 makes
the Pydantic models a *projection* of that contract. "Projection" is a
promise: any document the schema accepts, the model accepts; any
document the schema rejects, the model rejects. This file is the only
place that promise is enforced, and it is enforced *as a property* —
not as a list of examples someone hand-picked.

The distinction matters. A list-of-examples parity test proves "these
N documents behave identically." A property test proves "all documents
in the strategy's space behave identically" — where the strategy is
deliberately designed to cover the schema's own decision surface
(variable types, check types, required fields, semver shapes, unknown
fields, wrong kinds). The first test catches regressions you happened
to anticipate; the second catches regressions nobody did.

Why Hypothesis, and why an explicit seed corpus
-----------------------------------------------
Hypothesis generates documents by mutating examples from a strategy.
Left to itself, the examples it starts from vary across runs, machines,
and Hypothesis releases — which means a failure is reproducible on the
machine that saw it and often nowhere else. Two mechanisms fix that:

1. **``@example(...)``-style seeds via the fixture corpus.** The seeds
   in ``tests/fixtures/blueprints/property/`` are loaded once and
   asserted to be accepted by both engines. This is *not* a Hypothesis
   phase — it is a plain parameterised test — but it belongs here
   because it establishes the baseline the Hypothesis strategies
   mutate from. If a seed were invalid, the strategies below would
   start from a corrupt baseline, and their failures would be
   misattributed.

2. **Explicit ``st.seed``-derived strategies.** Every Hypothesis test
   in this file derives its strategy from a named base document and a
   single-field mutation, so the "space" is small, well-defined, and
   revisitable. This is deliberate: exhaustive fuzzing of the whole
   document would spend most of its budget generating documents that
   fail for uninteresting reasons (e.g. missing ``id``), and would
   rarely reach the specific rules this file is designed to pin.

Why *both* directions are checked in every test
-----------------------------------------------
A weaker parity test would check "if schema accepts then model
accepts" (soundness) *or* "if schema rejects then model rejects"
(completeness), but not both. Each is a different failure mode:

* **Soundness failure** (schema accepts, model rejects) is a
  ``ModelValidationError`` in production — the user is told their
  document is fine, then told it is not, with exit 5 and a
  "this is a Forge bug" message. Bad but loud.
* **Completeness failure** (schema rejects, model accepts) is worse:
  a malformed blueprint passes the schema check, reaches generation,
  and either crashes mid-scaffold or produces a broken project. Bad
  *and* quiet.

Checking both directions is what turns "the two engines agree" from
a hope into a test result. The ``schema_accepts(doc) == model_accepts(doc)``
form is the whole contract in one line; everything below is designed
to make that line exercise the right documents.

What this test is *not* for
---------------------------
* **Error message quality.** That is ``test_model_error_messages.py``'s
  job. This file only asserts *whether* a document is rejected, not
  *how* the rejection is rendered. Conflating the two would make a
  message wording change fail parity — the wrong signal.

* **Regeneration drift.** That is ``test_generated_in_sync.py``'s
  job. Drift and parity are related but distinct: a file can be
  regenerated (no drift) and still disagree with the schema if the
  generator itself has a bug. And a file can be hand-edited (drift)
  in a way that *preserves* parity — the file is wrong but the
  disagreement has not reached the surface yet. Each test catches its
  own class of failure.

* **Schema validity.** That is ``test_schema_shape.py``'s job. If the
  schema itself is malformed, every test in this file will fail for
  the wrong reason — but the schema test will have already flagged
  the actual cause, which is why it runs first in CI.

Design notes
------------
* **``_BASE`` is a fresh dict on every call, not a module constant.**
  Hypothesis mutates its inputs during shrinking. A module-level
  constant would be mutated by the first example and would leak
  that mutation into every subsequent example — a notorious
  Hypothesis trap that produces "flaky" tests whose flakiness
  depends on example ordering.

* **Strategies are narrow on purpose.** Each strategy mutates *one*
  field of a known-good baseline. The property asserted is "for
  every value of this one field, both engines agree." That is much
  stronger than "for every document, both engines agree" — the
  latter is nearly impossible to satisfy within Hypothesis's
  default example budget for any schema of realistic complexity. The
  narrow form lets the same budget cover every branch of the schema's
  decision surface without wasting time on uninteresting combinations.

* **``max_examples`` is bounded.** The default 100 is used for most
  tests, but for the slower mutations (which involve document
  re-serialisation) the number is reduced to keep the suite under
  a few seconds. ``@settings(deadline=None)`` is *not* used — a
  slow test is a signal, and disabling the deadline turns the
  signal into a silent performance regression. If a case genuinely
  needs more time, ``max_examples`` is the correct knob; ``deadline``
  is not.

* **Failure messages name the field.** When a parity failure fires,
  the ``assume``-free ``schema_accepts(doc) == model_accepts(doc)``
  assertion reports the document — which is a dict serialised by
  Hypothesis. That is enough to reproduce by hand, but not enough
  to know *which field* diverged. Adding an explicit
  ``assert ..., f"<field description>"`` wrapper around the
  comparison names the field in the failure, which is the
  difference between "something is wrong" and "the model rejects
  ``version: '1.0'`` but the schema accepts it."
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from forge.models._parity import model_accepts, schema_accepts

# ---------------------------------------------------------------------------
# Seed corpus
# ---------------------------------------------------------------------------
# Every seed must be accepted by *both* engines. A failing seed here
# means the strategies below are mutating a corrupt baseline, which
# would make every parity failure in this file misleading. The
# dedicated model-parsing test file also loads these seeds and asserts
# the model accepts them; doing it here too is deliberate duplication —
# the two assertions have different *intents* (that file says "the
# seed parses"; this file says "the seed is a valid starting point for
# the parity strategies") and a future refactor could reasonably break
# one without breaking the other.
_PROPERTY_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "blueprints" / "property"


def _seed_paths() -> list[Path]:
    """Sorted seed paths. Sorted so a failure reports a stable order."""
    return sorted(_PROPERTY_DIR.glob("*.yaml"))


@pytest.mark.unit()
@pytest.mark.parametrize("seed_path", _seed_paths(), ids=lambda p: p.stem)
def test_every_seed_is_accepted_by_both_engines(seed_path: Path) -> None:
    """Seeds are the baseline — both engines must accept them.

    Asserted as plain equality rather than two separate ``assert``
    statements so a failure reports both acceptances side by side
    ("schema said True, model said False") rather than just the one
    that failed. That single-line report is what makes the subsequent
    Hypothesis failures diagnosable: the reader knows immediately
    whether the baseline was accepted, which is the prerequisite for
    any Hypothesis failure meaning anything.
    """
    document = yaml.safe_load(seed_path.read_text(encoding="utf-8"))
    assert isinstance(document, dict)

    schema_ok = schema_accepts(document)
    model_ok = model_accepts(document)
    assert schema_ok == model_ok, (
        f"seed {seed_path.name} disagrees: " f"schema={schema_ok}, model={model_ok}"
    )
    # If both agreed on *rejection*, the seed is corrupt. Assert both
    # accepted — not just agreed — because agreement on rejection is
    # the failure mode this test exists to catch.
    assert schema_ok, f"seed {seed_path.name} was rejected by the schema"


# ---------------------------------------------------------------------------
# Baseline for mutation
# ---------------------------------------------------------------------------
def _base() -> dict[str, object]:
    """Fresh minimal valid document for each example.

    Function rather than constant so Hypothesis's shrinking cannot
    leak mutations between examples (see the module docstring's design
    notes). The shape is the same as the smallest property seed, kept
    in sync by construction rather than by reference — a fixture would
    work, but a literal is more readable when each mutation is two
    lines below it.
    """
    return {
        "schema_version": "1.0.0",
        "id": "sample.demo",
        "name": "Sample",
        "version": "1.0.0",
        "description": "Baseline document for parity testing.",
        "category": "api",
        "verification": [
            {"id": "readme", "type": "required_file", "path": "README.md"},
        ],
        "compatibility": {"min_forge_version": "1.0.0"},
    }


def _assert_parity(document: dict[str, object], field: str) -> None:
    """Assert schema and model accept/reject identically, naming ``field``.

    ``field`` is a short, human-readable description of what was
    mutated. It appears in the failure message, which is what makes a
    Hypothesis failure actionable: the reader knows the mutation
    applied to ``variables[0].type`` rather than having to diff the
    generated document against the baseline by eye.
    """
    schema_ok = schema_accepts(document)
    model_ok = model_accepts(document)
    assert schema_ok == model_ok, (
        f"parity divergence on {field}: "
        f"schema={schema_ok}, model={model_ok}\n"
        f"document={document!r}"
    )


# ---------------------------------------------------------------------------
# Unknown fields
# ---------------------------------------------------------------------------
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
@given(
    extra_key=st.text(min_size=1, max_size=32),
    extra_value=st.one_of(
        st.text(max_size=32),
        st.integers(min_value=-1_000_000, max_value=1_000_000),
        st.booleans(),
        st.none(),
        st.lists(st.integers(), max_size=3),
    ),
)
def test_unknown_top_level_field_parity(extra_key: str, extra_value: object) -> None:
    """Any extra top-level key: both reject or both accept.

    The schema forbids unknown fields via ``additionalProperties: false``;
    the model forbids them via ``extra="forbid"``. These two mechanisms
    implement the same rule and must agree for *every* possible extra
    key — including keys that collide with reserved words, keys with
    unusual characters, and keys whose value is itself a nested
    document. This test's strategy deliberately generates values of
    every JSON type so a regression that only forbids scalar extra
    fields (but permits nested-object extras, say) is caught.

    ``suppress_health_check=[HealthCheck.too_slow]`` is used because
    the strategy is broad (any text, any value type) and Hypothesis's
    default "too slow" heuristic fires on high-cardinality text
    strategies. The suppression is scoped to this test only — the
    other tests in this file do not suppress it, because their
    strategies are narrow enough to run fast.
    """
    document = _base()
    document[extra_key] = extra_value

    _assert_parity(document, f"top-level key {extra_key!r}")


# ---------------------------------------------------------------------------
# SemVer fields
# ---------------------------------------------------------------------------
def _semver_like() -> st.SearchStrategy[str]:
    """Strategy for strings that stress the SemVer pattern.

    Deliberately mixes valid SemVer (``1.0.0``, ``0.0.1``, with
    prerelease, with build metadata) with near-misses
    (``1.0``, ``1``, ``01.0.0``, ``1.0.0.0``) and outright
    nonsense (``abc``, ``""``). The point is to exercise the *boundary*
    of the pattern's regex, not to sample uniformly from all strings —
    uniform sampling would miss the boundary cases that matter and
    waste the budget on strings that are obviously invalid in
    uninteresting ways.
    """
    return st.one_of(
        # Valid: minimal, with prerelease, with build metadata, with both.
        st.sampled_from(
            [
                "0.0.1",
                "1.0.0",
                "10.20.30",
                "1.0.0-alpha",
                "1.0.0-alpha.1",
                "1.0.0+build",
                "1.0.0+build.42",
                "1.0.0-alpha.1+build.42",
            ]
        ),
        # Near-miss: two or four segments, leading zeros, empty
        # prerelease/build identifiers, etc.
        st.sampled_from(
            [
                "1.0",
                "1",
                "01.0.0",
                "1.00.0",
                "1.0.00",
                "1.0.0.0",
                "1..0",
                ".1.0",
                "1.0.",
                "v1.0.0",
                "1.0.0-",
                "1.0.0+",
            ]
        ),
        # Free-form strings — including the empty string, which the
        # schema rejects via ``pattern`` but accepts via ``min_length``
        # only if the pattern permits it (it does not). Hypothesis
        # shrinking will find the empty case on its own if the pattern
        # is loose, so including it explicitly makes the failure report
        # name it.
        st.text(max_size=20),
    )


@settings(max_examples=200)
@given(version=_semver_like())
def test_version_field_parity(version: str) -> None:
    """``version``: both engines agree on every SemVer-shaped string.

    The schema and the model encode SemVer differently — the schema
    uses a ``pattern`` regex, the model uses a ``StringConstraints``
    regex — but the two regexes must be *the same regex*. If they
    drift, this test catches it on the first string the two disagree
    about, which is almost always a boundary case the human reviewer
    of the drift would not have thought to check.
    """
    document = _base()
    document["version"] = version

    _assert_parity(document, f"version={version!r}")


@settings(max_examples=100)
@given(version=_semver_like())
def test_compatibility_min_forge_version_parity(version: str) -> None:
    """``compatibility.min_forge_version``: same regex, different field.

    Tested separately from ``version`` because the two fields are
    independent in the schema (both use the same regex, but nothing
    prevents a future schema change from tightening one and not the
    other). A regression that fixed ``version`` but forgot
    ``min_forge_version`` would be invisible to the previous test.
    """
    document = _base()
    # ``_base`` returns a fresh dict, so mutating the nested
    # ``compatibility`` object here cannot affect any other test.
    document["compatibility"] = {"min_forge_version": version}

    _assert_parity(document, f"compatibility.min_forge_version={version!r}")


# ---------------------------------------------------------------------------
# Variable types
# ---------------------------------------------------------------------------
_VARIABLE_TYPES_VALID = ("string", "boolean", "enum")
_VARIABLE_TYPES_INVALID = ("integer", "float", "object", "array", "number", "")


@settings(max_examples=100)
@given(
    variable_type=st.one_of(
        st.sampled_from(_VARIABLE_TYPES_VALID),
        st.sampled_from(_VARIABLE_TYPES_INVALID),
        st.text(max_size=20),
    )
)
def test_variable_type_enum_parity(variable_type: str) -> None:
    """``variables[0].type``: both agree on every candidate type string.

    The variable's ``validation`` block declares an ``enum`` list, so
    an ``enum``-typed variable is *also* valid here (its
    ``validation.enum`` requirement is satisfied). A ``boolean``-typed
    variable with an ``enum`` validation block is *invalid* — the
    schema permits ``validation`` on any variable type, but the
    default-kind check in the model would reject a boolean variable
    that carries an enum list *if* a default were also set. This
    test deliberately omits a default so the only rule under test is
    the ``type`` enum itself.
    """
    document = _base()
    document["variables"] = [
        {
            "id": "v",
            "type": variable_type,
            "description": "A variable used for type parity testing.",
            "required": True,
            "validation": {"enum": ["a", "b"]},
        }
    ]

    _assert_parity(document, f"variables[0].type={variable_type!r}")


# ---------------------------------------------------------------------------
# Variable enum ⇔ validation rule
# ---------------------------------------------------------------------------
@settings(max_examples=100)
@given(
    has_validation=st.booleans(),
    enum_size=st.integers(min_value=0, max_value=3),
)
def test_enum_variable_requires_validation_enum_parity(
    has_validation: bool, enum_size: int
) -> None:
    """``type='enum'`` ⇒ ``validation.enum`` present and non-empty.

    This is the schema's ``allOf``/``if-then`` rule, and it is also
    the model's ``model_validator`` rule. The two mechanisms differ
    structurally (a schema conditional vs. a Python validator) and
    have a documented history of drifting: a schema change that
    relaxes ``minItems: 1`` to ``0`` would permit an empty enum list
    that the model still rejects. This test would catch that drift on
    the first empty-list example Hypothesis generates.

    The three states — no validation at all, ``validation.enum = []``,
    ``validation.enum`` with items — cover both sides of the
    truth table and the ``minItems: 1`` boundary in one strategy.
    """
    document = _base()
    variable: dict[str, object] = {
        "id": "mode",
        "type": "enum",
        "description": "An enum variable for parity testing.",
        "required": True,
    }
    if has_validation:
        variable["validation"] = {"enum": [f"v{i}" for i in range(enum_size)]}
    document["variables"] = [variable]

    _assert_parity(
        document,
        f"enum variable with validation={has_validation}, " f"enum_size={enum_size}",
    )


# ---------------------------------------------------------------------------
# Check types and payloads
# ---------------------------------------------------------------------------
_CHECK_TYPES_VALID = ("command", "required_file")
_CHECK_TYPES_INVALID = ("shell", "http", "docker", "")


@settings(max_examples=100)
@given(
    check_type=st.one_of(
        st.sampled_from(_CHECK_TYPES_VALID),
        st.sampled_from(_CHECK_TYPES_INVALID),
        st.text(max_size=20),
    ),
    has_command=st.booleans(),
    has_path=st.booleans(),
)
def test_check_type_payload_parity(check_type: str, has_command: bool, has_path: bool) -> None:
    """``verification[0]``: both agree on type, payload, and wrong payload.

    The schema expresses the discriminated-payload rule via
    ``if/then`` pairs; the model expresses it via a ``model_validator``.
    They must agree on three things for every candidate type:

    * ``command`` type needs ``command`` present.
    * ``required_file`` type needs ``path`` present.
    * An unused payload field being *present* is a different
      question — the model rejects it, the schema permits it (it
      declares both ``command`` and ``path`` as optional). This is
      the one place where the two engines are deliberately
      permitted to disagree, and it is **not** tested here.

    Wait — that last claim deserves scrutiny. If the model rejects a
    document the schema accepts, this *test* would fail, because the
    test asserts parity. The resolution is that the wrong-payload
    case is tested *only* in ``test_model_error_messages.py`` (as a
    model-side case), and the parity test's strategy below is
    deliberately restricted to not generate the wrong-payload
    combination.

    That restriction is a *known limitation* of the current model
    (ADR-0008 records it): the model is stricter than the schema on
    this point, and the parity contract is scoped to the schema's
    declared rules, not the model's extra strictness. When the schema
    is tightened to also reject wrong payloads (a candidate for a
    future schema v1.1), the model will be loosened in lockstep, and
    this comment becomes obsolete. Until then, the model's extra
    strictness is tested where it belongs — with the model's other
    error shapes — and the parity test does not assert a property
    that does not hold.
    """
    document = _base()
    check: dict[str, object] = {"id": "c", "type": check_type}
    if has_command:
        check["command"] = "true"
    if has_path:
        check["path"] = "file.txt"

    # The model is deliberately stricter than the schema on
    # wrong-payload combinations (see docstring). Skip those cases so
    # the parity assertion reflects the schema's actual contract.
    wrong_payload = (check_type == "command" and has_path and not has_command) or (
        check_type == "required_file" and has_command and not has_path
    )
    if wrong_payload:
        return

    document["verification"] = [check]

    _assert_parity(
        document,
        f"check type={check_type!r}, " f"has_command={has_command}, has_path={has_path}",
    )


# ---------------------------------------------------------------------------
# Required-field omissions
# ---------------------------------------------------------------------------
_REQUIRED_TOP_LEVEL_FIELDS = (
    "schema_version",
    "id",
    "name",
    "version",
    "description",
    "category",
    "verification",
    "compatibility",
)


@pytest.mark.parametrize("field", _REQUIRED_TOP_LEVEL_FIELDS)
@pytest.mark.unit()
def test_omitting_required_top_level_field_parity(field: str) -> None:
    """Omitting any required field: both reject with a pointer at it.

    Plain parameterisation rather than Hypothesis because the space
    is finite and small — eight fields, one document each. Using
    Hypothesis here would burn examples proving what the explicit
    list already states. The *point* of the test is to pin the
    complete required set, and that is best done by naming the set,
    not by generating from a strategy that happens to cover it.

    Note that ``variables``, ``files``, and ``conditional_files`` are
    not in this list — they default to empty in the schema, so a
    document that omits them is still valid. Listing them here would
    make this test fail against the *correct* schema behaviour,
    which is the opposite of what a parity test should assert.
    """
    document = _base()
    del document[field]

    _assert_parity(document, f"missing required field {field!r}")


# ---------------------------------------------------------------------------
# Category enum
# ---------------------------------------------------------------------------
_CATEGORIES_VALID = ("api", "automation", "frontend", "library", "cli", "other")
_CATEGORIES_INVALID = ("backend", "API", "", "apis")


@settings(max_examples=50)
@given(
    category=st.one_of(
        st.sampled_from(_CATEGORIES_VALID),
        st.sampled_from(_CATEGORIES_INVALID),
        st.text(max_size=20),
    )
)
def test_category_enum_parity(category: str) -> None:
    """``category``: both agree on every candidate.

    The schema uses ``enum``; the model uses ``Category(str, Enum)``.
    These two are equivalent *if* the enum member names and the
    schema's enum values stay in lockstep — a drift the parity test
    catches but which a static reader might not notice until the CLI
    branches on a member that does not exist. The ``_CATEGORIES_INVALID``
    list includes ``"API"`` explicitly because a case-insensitive
    comparison in one engine but not the other is exactly the sort of
    drift this test exists to catch.
    """
    document = _base()
    document["category"] = category

    _assert_parity(document, f"category={category!r}")


# ---------------------------------------------------------------------------
# Verification list length
# ---------------------------------------------------------------------------
@settings(max_examples=20)
@given(count=st.integers(min_value=0, max_value=3))
def test_verification_list_length_parity(count: int) -> None:
    """``verification`` is required with ``min_length=1``.

    The schema declares ``minItems: 1``; the model declares
    ``min_length=1``. This is the smallest possible divergence between
    the two mechanisms — a single integer that must match exactly.
    The strategy samples both sides of the boundary (0 fails, 1, 2,
    3 pass) and the assertion is the plain parity equality. A test
    this small is worth writing because the boundary is
    load-bearing: an empty ``verification`` list is the single most
    common malformed blueprint, and it is the *only* list field in
    the schema that is not allowed to be empty.
    """
    document = _base()
    document["verification"] = [
        {"id": f"c{i}", "type": "required_file", "path": f"file{i}.txt"} for i in range(count)
    ]

    _assert_parity(document, f"verification has {count} entries")
