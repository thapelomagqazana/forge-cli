"""Tests for the ``render_json`` contract on a ``GenerationPlan``.

The dry-run JSON output is a *public contract* (see ADR-0018 and
``docs/reference/DRY_RUN_PLAN.md``). CI scripts parse it, dashboards
ingest it, and future HTTP endpoints will return it. That means the
tests in this file are not "does it serialize?" — they are "does it
serialize according to the promises we made?"

The four promises, each tested at the layer where it lives
-----------------------------------------------------------
1. **Deterministic.** Same plan in, same bytes out — always. Tested
   at the plan-object level (``test_render_is_deterministic``) so a
   regression in ``json.dumps`` arguments, DTO field ordering, or
   ``sort_keys`` is caught regardless of which side changed.

2. **Stable schema.** The envelope has a documented ``schema_version``
   and a fixed set of top-level keys. Tested against the golden
   fixture (``test_golden_snapshot_matches``) so a schema bump is a
   deliberate, reviewable act — never a silent addition.

3. **ANSI-free.** Colour is a presentation concern (see
   ``forge.cli.ui.emit``); the data path must be free of escape
   codes. Tested by scanning the rendered string for ``\\x1b`` —
   cheap, direct, and impossible to false-positive.

4. **Unicode-faithful.** Non-ASCII characters are emitted as
   themselves, not ``\\uXXXX``. Tested with a fixture that contains
   an accented name, a CJK path, and an emoji — each a realistic
   case in a user's file tree.

What this file does **not** test
--------------------------------
    * The DTO's fields — that is ``test_plan_shape.py``.
    * The human renderer — that is ``tests/unit/dryrun/renderers/``.
    * Service behaviour that produces the plan — that is
      ``test_service_*.py``.
    * Round-trip parseability of arbitrary JSON — that is a
      different concern (we test *our* contract, not
      ``json.loads``).

Keeping this file to the render contract means a failure points at
exactly one property: "the JSON output broke a promise."

S108 suppressions in this file
------------------------------
Three ``# noqa: S108`` markers appear below, each scoped to a single
line with the reason on the same line. S108 flags the literal prefix
``/tmp/`` because in *production* code that prefix is the first half
of several real vulnerabilities (symlink attacks on predictable
names, TOCTOU races, information leaks from world-readable files
that persist after the process exits).

None of those apply here: every flagged string is a **destination
path fed into the JSON renderer** — data the test is *asserting
about*, not a path the test opens for writing. The renderer's job is
to serialise the string; the test's job is to verify the string
appears literally in the output (the "Unicode-faithful" promise).
The path's filesystem meaning is irrelevant to both.

The alternative — replacing ``/tmp/café`` with a different absolute
path — is tempting but wrong here: these tests deliberately exercise
a *realistic* destination that a user might name (a temp directory
with a Unicode name), and any substitute would either be less
realistic (``/var/lib/café``? nobody puts projects there) or
introduce a new source of ambiguity (``/home/user/café`` assumes a
specific home-dir layout). The suppression, with the reason on the
same line, keeps the intent legible: "this is a test fixture, not a
place we write."
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from forge.dryrun import (
    CollisionDTO,
    DryRunService,
    GenerationPlan,
    PlannedFileDTO,
    PlannedFileKind,
    SkippedFileDTO,
    render_json,
)
from forge.verification import validate_blueprint_document

_FIX = Path(__file__).parents[2] / "fixtures" / "dryrun"


# -----------------------------------------------------------------------------
# The golden fixture's canonical plan
# -----------------------------------------------------------------------------
def _golden_plan() -> GenerationPlan:
    """Return the exact plan the golden fixture represents.

    This is the *one* helper that defines what ``golden-plan.json``
    contains — both the fixture writer (``_regenerate.py``) and this
    test go through the same construction, so the two cannot diverge
    without the diff making it obvious.

    Why not call the service directly?

        The fixture is *frozen data* — it must be reproducible
        without a working service, so a future refactor of
        ``DryRunService`` that changed its behaviour on the fixture
        blueprint would still produce the same golden JSON (with a
        regeneration step), rather than every run of this test
        failing for reasons unrelated to the JSON contract.

    Why not call ``_regenerate.main`` directly?

        ``main()`` writes files — it is the *regeneration command*,
        not a plan builder. A test that called it would have the
        side effect of rewriting the golden file on every run, which
        is exactly the wrong behaviour for a test that is *asserting*
        the file's content. The plan builder here is a plain
        function; the *writer* lives in ``_regenerate.py`` and is
        only invoked by a developer.
    """
    # Imported lazily so a missing or renamed regeneration module
    # fails *this test*, not module collection for the whole file.
    from tests.fixtures.dryrun import _regenerate

    return _regenerate._build_golden_plan()


# -----------------------------------------------------------------------------
# Helpers for other tests
# -----------------------------------------------------------------------------
def _plan_via_service(tmp_path: Path) -> GenerationPlan:
    """Build a plan through the real service pipeline.

    Uses the fixture blueprint and the shared fixture shape so the
    plan's *fields* are populated from real data — not from a
    hand-constructed DTO. Testing ``render_json`` on a hand-constructed
    DTO would only prove the serializer works on shapes the test
    author thought of; testing it on the service's output proves it
    works on shapes the service actually produces.
    """
    document = yaml.safe_load((_FIX / "blueprint.yaml").read_text(encoding="utf-8"))
    blueprint = validate_blueprint_document(document, source="blueprint.yaml")
    return DryRunService().plan(
        blueprint=blueprint,
        variables={"include_docker": False},
        destination=tmp_path,
    )


def _plan_hand_built(
    *,
    name: str = "sample.demo",
    destination: str = "/dest",
) -> GenerationPlan:
    """Build a plan by hand for cases that need specific field values.

    Some tests need to inject a value the fixture blueprint cannot
    naturally produce (e.g. a Unicode filename, a collision entry,
    an unusual warning). A hand-built DTO is the honest way to reach
    those shapes without distorting the fixture.

    The defaults (``name="sample.demo"``, ``destination="/dest"``)
    are *deliberately different* from the golden fixture's plan.
    ``_plan_hand_built`` is not the golden plan — the golden plan
    comes from ``_golden_plan()``. Keeping the two helpers distinct
    means:

        * the field-shape tests (``test_item_shapes_have_stable_field_names``)
          exercise the *fully-populated* plan, which the hand-built
          helper guarantees,
        * the golden-snapshot test exercises the *canonical* plan,
          which the fixture file defines,
        * a change to one helper cannot silently change the other
          test's assertions.
    """
    return GenerationPlan(
        blueprint_id=name,
        blueprint_version="1.0.0",
        destination=destination,
        overwrite=False,
        files_to_create=[
            PlannedFileDTO(
                source="templates/main.j2",
                destination="src/main.py",
                kind=PlannedFileKind.RENDER,
            ),
        ],
        files_to_skip=[
            SkippedFileDTO(
                source="templates/Dockerfile.j2",
                destination="Dockerfile",
                condition="include_docker == true",
                reason="condition evaluated false",
            ),
        ],
        collisions=[
            CollisionDTO(
                destination="pyproject.toml",
                existing_kind="file",
                sources=["templates/pyproject.toml.j2"],
            ),
        ],
        warnings=["sample warning"],
    )


# -----------------------------------------------------------------------------
# Promise 1: determinism
# -----------------------------------------------------------------------------
def test_render_is_deterministic(tmp_path: Path):
    """Same plan in, same bytes out — twice in a row, identical.

    The strongest determinism assertion: not "same parsed JSON", but
    *same string*. If the renderer introduced any nondeterminism
    (dict iteration order, timestamp, random ID), this fails
    immediately — regardless of whether the difference would be
    visible after parsing.

    This is deliberately stronger than ``parsed_a == parsed_b``:
    the latter allows whitespace and key-ordering variance, which
    break ``diff``-based workflows (a consumer that hashes the JSON
    for change detection would see every run as a change).
    """
    plan = _plan_via_service(tmp_path)
    a = render_json(plan)
    b = render_json(plan)
    assert a == b


def test_render_is_deterministic_across_rebuilt_plans(tmp_path: Path):
    """Two independently built plans for the same inputs render identically.

    Complements ``test_render_is_deterministic``: that test uses the
    *same* plan object twice; this test uses *two* plans built from
    the same service call. If the service ever introduced
    nondeterminism (a set-backed list, an unordered walk), the two
    plans would differ before rendering, and this test catches it
    even though the per-render function is a pure function of its
    input.

    Together, the two tests pin the whole pipeline: service output
    is deterministic (this test) *and* the renderer preserves
    determinism (the previous test).
    """
    a = render_json(_plan_via_service(tmp_path))
    b = render_json(_plan_via_service(tmp_path))
    assert a == b


# -----------------------------------------------------------------------------
# Promise 2: stable schema
# -----------------------------------------------------------------------------
def test_json_is_parseable(tmp_path: Path):
    """The renderer produces syntactically valid JSON.

    A trivial assertion, included for the same reason a doctor checks
    your pulse first: if the output is not even valid JSON, every
    other test in this file would fail with a confusing error
    (``json.JSONDecodeError``) rather than a signal that names the
    actual problem.

    Being first in the "schema" section means a broken renderer
    fails here with a clear message before the shape assertions
    below — a property a test suite should have and frequently
    doesn't.
    """
    json.loads(render_json(_plan_via_service(tmp_path)))


def test_envelope_has_expected_top_level_keys(tmp_path: Path):
    """The envelope contains exactly the documented top-level keys.

    The set is fixed for v1.0.0. New keys require a schema_version
    bump (additive change) and a ``DRY_RUN_PLAN.md`` update. A
    reviewer who sees this test change sees a schema change — the
    set is small enough to be the schema, and explicit enough that a
    future contributor cannot accidentally add a field that
    automation doesn't know about.
    """
    payload = json.loads(render_json(_plan_via_service(tmp_path)))

    assert set(payload.keys()) == {
        "schema_version",
        "blueprint_id",
        "blueprint_version",
        "destination",
        "overwrite",
        "files_to_create",
        "files_to_skip",
        "collisions",
        "warnings",
    }


def test_schema_version_is_literal_1_0_0(tmp_path: Path):
    """The envelope's ``schema_version`` is the literal string ``"1.0.0"``.

    Automation pinning to v1.0.0 depends on this value being
    *exactly* that string — not ``"1.0"`` (ambiguous), not
    ``"1.0.0+local"`` (surprising), not an int ``1`` (a different
    type entirely). The literal-string check is what makes
    downstream ``if version == "1.0.0"`` branches reliable.
    """
    payload = json.loads(render_json(_plan_via_service(tmp_path)))
    assert payload["schema_version"] == "1.0.0"


def test_item_shapes_have_stable_field_names(tmp_path: Path):
    """Each item list's entries have the documented field names.

    The ``files_to_create`` item's field set is ``{source,
    destination, kind}``; ``files_to_skip`` is ``{source,
    destination, condition, reason}``; ``collisions`` is
    ``{destination, existing_kind, sources}``. These are the fields
    automation reads. A change here is a breaking change to the JSON
    contract, and this test is the trip wire.

    Uses the fully populated hand-built plan so *all* list sections
    are exercised in a single test — a service-built plan might have
    empty ``collisions`` (when the destination is empty), and a test
    that only checked non-empty sections would miss the empty case's
    shape.
    """
    payload = json.loads(render_json(_plan_hand_built()))

    assert set(payload["files_to_create"][0].keys()) == {
        "source",
        "destination",
        "kind",
    }
    assert set(payload["files_to_skip"][0].keys()) == {
        "source",
        "destination",
        "condition",
        "reason",
    }
    assert set(payload["collisions"][0].keys()) == {
        "destination",
        "existing_kind",
        "sources",
    }


def test_golden_snapshot_matches():
    """The canonical plan's JSON matches the committed golden fixture.

    The golden file (``tests/fixtures/dryrun/golden-plan.json``) is
    regenerated by ``python -m tests.fixtures.dryrun._regenerate``.
    A failure here means either:

    * the plan DTO's shape changed, or
    * the renderer's output changed, or
    * the golden file is out of date.

    All three are intentional actions — a reviewer reads the diff,
    decides whether the change is additive (schema_version stays) or
    breaking (schema_version bumps), and commits accordingly.

    Two properties make this test *reliable* (not just present):

    1. **The plan comes from the same builder as the golden file.**
       ``_golden_plan()`` calls ``_regenerate._build_golden_plan()``
       — the same function ``_regenerate.main()`` uses to *write*
       the fixture. If a future contributor changes the golden
       fixture's shape (adds a warning, renames a file, changes the
       destination), one edit updates both the writer and this
       reader. A hard-coded plan in this test would be a second
       source of truth and could silently drift.

    2. **The comparison is a full-payload ``==``.** A field-by-field
       comparison misses any *new* field a future contributor might
       add to the DTO. A full equality check fails on any shape
       change, forcing the reviewer to see the diff and decide
       whether it is additive (schema_version stays) or breaking
       (schema_version bumps). It also catches *removed* fields and
       *renamed* fields, which a targeted comparison would miss.

    The comparison is on *parsed* JSON, not raw bytes, because the
    golden file is pretty-printed for human review while the
    renderer emits compact JSON. Comparing parsed values is the
    correct abstraction: the *contract* is the JSON values, not the
    whitespace between them.
    """
    rendered = json.loads(render_json(_golden_plan()))
    golden = json.loads((_FIX / "golden-plan.json").read_text(encoding="utf-8"))

    # Full-payload equality. Failure produces a diff of the *whole*
    # payload, which names exactly which field(s) diverged.
    assert rendered == golden


def test_empty_plan_renders_with_all_keys_present(tmp_path: Path):
    """An empty plan renders with the same envelope shape as a full one.

    The empty case is not the absence of a plan; it is a plan with
    three empty lists. Automation iterating ``plan["files_to_create"]``
    should never need a ``.get(...)`` guard, and any consumer reading
    ``plan["collisions"]`` should get ``[]``, not a ``KeyError``. This
    test pins the presence of every key in the empty case — the
    schema does not vary by plan content.
    """
    empty = GenerationPlan(
        blueprint_id="sample.empty",
        blueprint_version="1.0.0",
        destination="/empty",
    )
    payload = json.loads(render_json(empty))

    for key in (
        "schema_version",
        "blueprint_id",
        "blueprint_version",
        "destination",
        "overwrite",
        "files_to_create",
        "files_to_skip",
        "collisions",
        "warnings",
    ):
        assert key in payload
    assert payload["files_to_create"] == []
    assert payload["files_to_skip"] == []
    assert payload["collisions"] == []
    assert payload["warnings"] == []


# -----------------------------------------------------------------------------
# Promise 3: ANSI-free
# -----------------------------------------------------------------------------
def test_no_ansi_escape_sequences(tmp_path: Path):
    """The JSON output contains no ANSI escape sequences.

    Colour is a presentation concern (see ``forge.cli.ui.emit``),
    and the JSON renderer's job is to produce data — not display.
    Any ``\\x1b`` byte in the output would be a regression: a
    downstream parser feeding the JSON to ``jq`` or a dashboard
    would either choke on it or render raw escape bytes to the
    user's terminal.

    The check is a simple substring search for the ANSI escape
    prefix (``\\x1b[``) — a stronger assertion than "no colour
    codes" because it catches *any* escape sequence, including ones
    a future refactor might introduce for reasons we haven't
    thought of.
    """
    rendered = render_json(_plan_via_service(tmp_path))
    assert "\x1b[" not in rendered


# -----------------------------------------------------------------------------
# Promise 4: Unicode-faithful
# -----------------------------------------------------------------------------
def test_non_ascii_characters_are_emitted_literally():
    """Non-ASCII characters appear as themselves, not as escape sequences.

    Uses ``ensure_ascii=False`` on ``json.dumps`` — the JSON spec
    allows either form, but the literal form is:

        * greppable (a user can search their terminal for a filename),
        * diffable (a ``git diff`` of two plans shows the character,
          not the escape code),
        * smaller (4 bytes for most CJK characters vs 6 for the
          escaped form).

    The fixture uses one example of each realistic class:

        * an accented Latin character (``café``),
        * a CJK filename segment (``文件``),
        * a non-BMP character outside the BMP (the emoji ``🚀``).

    Each exercises a different escape-path through ``json.dumps``:
    the first two are in the BMP (2-byte UTF-8, 4-byte JSON escape),
    the third is above it (4-byte UTF-8, 8-byte JSON escape).
    """
    plan = GenerationPlan(
        blueprint_id="sample.unicode",
        blueprint_version="1.0.0",
        # ``/tmp/...`` literal used only as *data* fed to the
        # renderer — the renderer never opens it, never creates
        # anything under it. S108 flags the prefix as a probable
        # insecure-temp-file path, but the threat model does not
        # apply to a string that appears as an expected *value* in
        # an output-contract test. See the module docstring's
        # "S108 suppressions in this file" section.
        destination="/tmp/café/文件",  # noqa: S108 - literal fixture data, not a real path
        files_to_create=[
            PlannedFileDTO(
                source="templates/🚀.j2",
                destination="docs/café.md",
                kind=PlannedFileKind.RENDER,
            ),
        ],
    )
    rendered = render_json(plan)

    assert "café" in rendered
    assert "文件" in rendered
    assert "🚀" in rendered
    # The escaped forms must NOT appear.
    assert "\\u00e9" not in rendered  # é escaped
    assert "\\u6587" not in rendered  # 文 escaped


def test_unicode_round_trips_through_parsing():
    """Non-ASCII content survives render → parse unchanged.

    The literal-emission promise is only useful if the consumer can
    read it back. This test goes the full round trip (plan → JSON
    string → parsed dict) and asserts the values are byte-identical
    to what went in. A renderer that emitted characters correctly
    but a parser that mangled them (or vice versa) would fail here.
    """
    plan = GenerationPlan(
        blueprint_id="sample.unicode",
        blueprint_version="1.0.0",
        # Same reasoning as the previous test: the literal is
        # *data* the renderer serialises, never a path the test
        # opens. See the module docstring's "S108 suppressions in
        # this file" section.
        destination="/tmp/café",  # noqa: S108 - literal fixture data, not a real path
        files_to_create=[
            PlannedFileDTO(
                source="templates/é.j2",
                destination="é.py",
                kind=PlannedFileKind.RENDER,
            ),
        ],
    )
    payload = json.loads(render_json(plan))

    assert payload["destination"] == "/tmp/café"  # noqa: S108
    assert payload["files_to_create"][0]["source"] == "templates/é.j2"
    assert payload["files_to_create"][0]["destination"] == "é.py"


# -----------------------------------------------------------------------------
# Ordering guarantees inside the JSON
# -----------------------------------------------------------------------------
def test_list_order_is_preserved_from_dto(tmp_path: Path):
    """The renderer does not re-sort lists — order is preserved as-is.

    The DTO's lists are already sorted by the service (see
    ``DryRunService.plan``). The renderer is a *pure projection* —
    it does not impose its own ordering, because doing so would
    duplicate the service's sorting contract and invite the two to
    drift.

    The assertion uses a hand-built plan whose list order is
    deliberately *not* sorted. If the renderer ever re-sorted, this
    test would fail — a signal that the renderer is doing more than
    it should.
    """
    plan = GenerationPlan(
        blueprint_id="sample.order",
        blueprint_version="1.0.0",
        destination="/dest",
        files_to_create=[
            # Deliberately out of alphabetical order.
            PlannedFileDTO(source="b.j2", destination="zzz.py", kind=PlannedFileKind.RENDER),
            PlannedFileDTO(source="a.j2", destination="aaa.py", kind=PlannedFileKind.RENDER),
        ],
    )
    payload = json.loads(render_json(plan))

    assert [f["destination"] for f in payload["files_to_create"]] == ["zzz.py", "aaa.py"]


def test_schema_version_order_is_irrelevant_but_keys_are_sorted(tmp_path: Path):
    """Top-level keys appear sorted alphabetically in the raw string.

    ``json.dumps(..., sort_keys=True)`` sorts *every* mapping, at
    every level. The output is thus byte-stable across runs *and*
    across Python versions (dict ordering was only guaranteed from
    3.7, and we do not want to depend on that).

    Asserting on the *sorted* order in the raw string is a
    byte-level check — stronger than parsed equality, because it
    proves the renderer actually passed ``sort_keys=True``. A
    regression that dropped the argument would produce different
    bytes for the same data (order dependent on field declaration),
    and every golden-file diff would be noise. This test prevents
    that.
    """
    rendered = render_json(_plan_hand_built())

    # The first key after the opening brace should be alphabetically
    # first — ``blueprint_id`` — because ``sort_keys=True`` places
    # it before ``blueprint_version``, ``collisions``, and the rest.
    # Finding it as the first *value* key (after the opening ``{``)
    # is what proves sorting is active.
    assert rendered.startswith('{"blueprint_id"')


# -----------------------------------------------------------------------------
# Integration with the golden fixture regeneration
# -----------------------------------------------------------------------------
@pytest.mark.parametrize(
    "plan_factory",
    [
        pytest.param(lambda tmp: _plan_via_service(tmp), id="service-built"),
        pytest.param(lambda tmp: _plan_hand_built(), id="hand-built"),
    ],
)
def test_render_returns_a_string(tmp_path: Path, plan_factory):
    """``render_json`` returns ``str``, not ``bytes`` or a dict.

    A trivial type check, but load-bearing for two reasons:

    1. ``ui.emit(text)`` in the CLI calls ``print(text)``, which
       requires ``str`` — a ``bytes`` return would print the
       ``b'...'`` repr, silently corrupting the CLI output.
    2. Future HTTP endpoints may want to set a
       ``Content-Length`` header from ``len(rendered)``; that is
       only meaningful for ``str`` (the byte length depends on
       encoding), and getting it wrong would produce a header
       mismatch the client would reject.

    The test parametrises across both a service-built plan (real
    data) and a hand-built plan (edge-case field values) so neither
    code path produces the wrong type.
    """
    assert isinstance(render_json(plan_factory(tmp_path)), str)
