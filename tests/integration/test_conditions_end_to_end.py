"""Integration tests for the conditional-file-inclusion pipeline.

Scope
-----
This module exercises the *whole* pipeline that a real blueprint
author runs against:

    YAML on disk
      │
      ▼  BlueprintLoader.load               (schema + model + fs checks)
    Blueprint model
      │
      ▼  VariableResolver.resolve           (precedence, type coercion)
    ResolvedValues
      │
      ▼  ConditionalEvaluator.evaluate      (parse + evaluate conditions)
    surviving groups
      │
      ▼  FileMapper.map                     (destination resolution, collisions)
    list[PlannedFile]

Every step is *real* — no fakes, no stubs, no patched imports. The
fixtures are real YAML files in ``tests/fixtures/conditions/``, loaded
by the real loader; the resolver runs against the real blueprint's
variable definitions; the evaluator parses the conditions from the
YAML text and evaluates them against the resolver's output; the
planner builds a real plan.

That is what "integration test" means here: individual steps are
covered by their own unit tests (``test_loader.py``, ``test_resolver.py``,
``test_parser.py``, ``test_evaluator.py``, ``test_mapper.py``). This
file proves the steps *compose* — that the output of each is exactly
what the next expects, on real fixtures, with real configuration.

Why integration tests are worth the runtime cost
------------------------------------------------
Unit tests miss whole classes of bugs that only appear when two
components disagree:

* The resolver produces ``ResolvedValues`` (immutable); the evaluator
  expects a plain ``Mapping``. If the resolver ever changed its return
  type, unit tests of each would still pass — the composition would
  fail here.
* The loader produces a ``Blueprint`` whose ``conditional_files``
  field is a list of Pydantic ``ConditionalFileEntry`` instances; the
  evaluator duck-types on ``.condition`` and ``.files``. A model
  rename would break the composition without breaking either
  component's own tests.
* The evaluator produces ``(Expression, list[FileEntry])`` tuples; the
  planner expects ``(Any, list[FileEntry])`` tuples. Any drift in the
  tuple's *shape* or *order* fails only at the seam.

Each of these is a real, plausible bug that no unit test in isolation
would catch. The integration tests exist to catch them.

What this file does *not* test
------------------------------
* **The individual components' internal behaviour.** If a unit test
  for the parser, evaluator, or planner were failing, this file would
  be redundant noise. Run the unit tests first.
* **The writer/generation pipeline.** The writer is a separate task
  and a separate layer. This file stops at ``list[PlannedFile]``,
  which is the writer's *input*. Testing the writer here would
  conflate two integration boundaries.
* **Rendering.** Templates are not rendered in this file. Rendering
  has its own integration tests (``test_rendering_end_to_end.py``).
* **The CLI.** ``forge create`` has its own integration tests
  (``test_create_command.py``, ``test_create_dual_mode.py``). This
  file exercises the *library* pipeline the CLI calls — one layer
  down from the CLI, which is the right abstraction for testing the
  composition of loader + resolver + evaluator + planner.
* **Integer comparison end-to-end.** The blueprint schema's
  ``VariableType`` enum is ``{string, boolean, enum}`` — none of
  which resolves to a Python ``int``. A condition like
  ``python_major == 3`` therefore cannot succeed through the resolver,
  because the resolved value is the string ``"3"`` and the type-aware
  rule correctly rejects ``"3" == 3``. This is *by design* (the whole
  point of ADR-0016). Coverage for integer comparison lives at the
  unit level in ``test_evaluator.py::TestTypeAwareComparison``, where
  AST nodes are constructed directly. If the schema ever gains an
  ``integer`` ``VariableType``, an integration fixture becomes
  possible and should be added at that time.

Fixtures
--------
All fixtures live under ``tests/fixtures/conditions/`` and are
documented in that directory's ``README.md``. They are:

* Complete, schema-valid blueprints (loader-level failures live in
  ``tests/fixtures/loader/``).
* Backed by real template stubs under ``templates/`` so the loader's
  template-existence check passes.
* **Self-contained for the ``cli_vars={}`` case.** Every fixture
  supplies a default for every variable its conditions reference, so
  running the pipeline with no CLI input exercises the *default*
  path. Fixtures that required a value with no default would fail at
  the resolver before reaching the evaluator — which is not what
  these fixtures exist to test.

This file assumes those fixtures exist; it does not construct them
in code. A fixture-based integration test is more valuable than a
hand-assembled one because the fixture *is* the shape a real
blueprint author would write — if the fixture ever stopped being a
valid blueprint, the loader would reject it and the test would fail
loudly, which is exactly what we want.

Lint conventions
----------------
* **No mutable class attributes** (``RUF012``).
* **No side-effect-only fixtures** — the helpers are module-level
  functions; the only pytest fixtures in this file are the standard
  ones (``tmp_path``, currently unused).
* **No hardcoded ``/tmp``** (``S108``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from forge.conditions import ConditionalEvaluator
from forge.loading import BlueprintLoader
from forge.loading.result import Err, Ok
from forge.models import Blueprint
from forge.planning import FileMapper, PlannedFile, PlannedFileKind
from forge.resolution import VariableResolver

# ---------------------------------------------------------------------------
# Fixture locations
#
# ``parents[1]`` walks from
# ``tests/integration/test_conditions_end_to_end.py`` to ``tests/``.
# Naming the conditions fixtures directory here means a future move
# of the fixture tree produces a single, obvious failure point
# rather than a scattered set of "file not found" errors.
# ---------------------------------------------------------------------------
_CONDITIONS_FIXTURES = Path(__file__).parents[1] / "fixtures" / "conditions"


# ---------------------------------------------------------------------------
# Helpers
#
# The four helpers below mirror the pipeline steps they invoke: load,
# resolve, evaluate, plan. Each helper takes the *inputs* a real
# caller would provide and returns the *output* the caller would get.
# Errors are surfaced as ``pytest.fail`` with a message naming the
# specific step that failed, so a red test points at the failing
# component rather than at a generic "pipeline returned Err"
# assertion.
# ---------------------------------------------------------------------------
def _load_blueprint(path: Path) -> Blueprint:
    """Load and validate a blueprint from disk, failing with context.

    The loader returns ``Result[Blueprint, list[ValidationError]]``.
    Tests in this file want the ``Blueprint`` or a clear failure
    message; asserting inside the helper means every test does not
    repeat the same five-line unwrap.
    """
    result = BlueprintLoader().load(path)
    if isinstance(result, Err):
        rendered = "\n  ".join(e.format() for e in result.error)
        pytest.fail(f"loader rejected {path}:\n  {rendered}")
    return result.value


def _resolve(blueprint: Blueprint, cli_vars: dict[str, Any]):
    """Resolve variables with the resolver, failing with context."""
    result = VariableResolver().resolve(blueprint, cli_vars, {})
    if isinstance(result, Err):
        rendered = "\n  ".join(e.format() for e in result.error)
        pytest.fail(
            f"resolver rejected variables for {blueprint.id} "
            f"(cli_vars={cli_vars!r}):\n  {rendered}"
        )
    return result.value


def _evaluate(blueprint: Blueprint, variables) -> list:
    """Evaluate conditions with the evaluator, failing with context."""
    result = ConditionalEvaluator().evaluate(blueprint, variables.as_dict())
    if isinstance(result, Err):
        rendered = "\n  ".join(f.format() for f in result.error.findings)
        pytest.fail(f"evaluator rejected {blueprint.id}:\n  {rendered}")
    return result.value


def _plan(blueprint: Blueprint, variables, *, groups: list | None = None):
    """Plan files with the mapper, failing with context.

    ``groups`` is the *surviving* conditional groups from the
    evaluator. Passing ``None`` means "the caller did not evaluate
    conditions"; passing ``[]`` means "the caller evaluated
    conditions and none survived". The distinction matters because
    the mapper's safe-by-default rule treats ``None`` and ``[]``
    identically in output but differently in *intent* — and the tests
    below assert the intent.
    """
    result = FileMapper().map(
        blueprint,
        variables.as_dict(),
        conditional_groups=groups,
    )
    if isinstance(result, Err):
        rendered = "\n  ".join(f.format() for f in result.error.findings)
        pytest.fail(f"planner rejected {blueprint.id}:\n  {rendered}")
    return result.value


def _pipeline(
    fixture_name: str,
    cli_vars: dict[str, Any],
) -> list[PlannedFile]:
    """Run the whole pipeline against a conditions fixture.

    Convenience for the majority of tests below, which want to
    assemble the four steps and inspect the resulting plan. The
    helpers above each surface their own step's failures, so a
    pipeline failure names the failing component — not "pipeline
    failed".
    """
    path = _CONDITIONS_FIXTURES / fixture_name
    blueprint = _load_blueprint(path)
    resolved = _resolve(blueprint, cli_vars)
    survivors = _evaluate(blueprint, resolved)
    return _plan(blueprint, resolved, groups=survivors)


# ---------------------------------------------------------------------------
# The happy path — every fixture loads, resolves, evaluates, and plans.
#
# This section is the *canary*. If any of the composition invariants
# (loader→resolver, resolver→evaluator, evaluator→planner, etc.) ever
# break, one or more of these fixtures will fail — and the failing
# fixture's name tells a reviewer which pipeline stage is at fault.
#
# Every fixture under ``_CONDITIONS_FIXTURES/*.yaml`` (except the
# intentionally-failing ones, which are listed separately below) is
# discovered via glob, so a new fixture is automatically covered
# without editing this file. That is the right trade: fewer places to
# forget to add coverage.
# ---------------------------------------------------------------------------
_HAPPY_FIXTURES = sorted(
    p.name
    for p in _CONDITIONS_FIXTURES.glob("*.yaml")
    if p.name
    not in {
        # Intentionally-failing fixtures: exercised in dedicated
        # sections below with assertions about *how* they fail, not
        # just that the pipeline works.
        "undefined-variable.yaml",
        "bad-syntax.yaml",
        "invalid-condition-variable.yaml",
        "duplicate-destination.yaml",
    }
)


@pytest.mark.parametrize("fixture_name", _HAPPY_FIXTURES)
def test_pipeline_runs_for_valid_fixture(fixture_name: str) -> None:
    """Every non-failing fixture runs through the whole pipeline.

    The assertion is deliberately minimal: the pipeline returns a
    plan (not an error). The *contents* of the plan are asserted by
    the specific scenario tests below. This test's job is to prove
    that the four components compose for every shape of valid
    blueprint the fixture tree contains — a canary for the whole
    integration surface.

    A fixture that suddenly fails here (but passed before) means one
    of the pipeline's seams changed shape. The failure will name the
    fixture and (via the helpers' ``pytest.fail`` messages) the
    failing step.

    Empty ``cli_vars`` means "use blueprint defaults". Every fixture
    in this corpus supplies a default for every variable its
    conditions reference, so the resolver can produce a complete
    ``ResolvedValues`` without user input. That is not an accident
    of design — it is what makes the glob-driven happy-path test
    possible: a fixture that required a value with no default would
    fail at the resolver, which is not the failure this canary is
    meant to surface. Fixtures that need to *exercise* the
    missing-required-variable path belong in the resolver's own
    tests, not here.
    """
    plan = _pipeline(fixture_name, cli_vars={})
    assert isinstance(plan, list)
    # The planner returns a *sorted* list; assert the invariant
    # rather than re-checking it later.
    assert [p.destination for p in plan] == sorted(p.destination for p in plan)


# ---------------------------------------------------------------------------
# Scenario 1 — string-equality condition includes and excludes correctly.
#
# ``database.yaml`` declares a ``database`` enum variable with a
# ``default: postgresql``. It has one unconditional file group
# (``README.md``, ``pyproject.toml``) and one conditional group
# (``db/postgres.py``, ``tests/test_postgres.py``) guarded by
# ``database == 'postgresql'``.
#
# The two tests below exercise the same fixture with two different
# variable values, proving the condition actually controls inclusion.
# A bug that always included the group (or always excluded it) would
# pass one of these tests and fail the other — that is why both are
# present, and why a single positive case would be insufficient.
# ---------------------------------------------------------------------------
class TestStringEqualityCondition:
    """``database == 'postgresql'`` controls the ``db/`` group."""

    FIXTURE = "database.yaml"

    def test_postgresql_includes_the_db_group(self) -> None:
        """``database=postgresql`` → the two ``db/`` files are planned.

        The assertion checks the *surviving* files, not the excluded
        ones — the existence of the PostgreSQL files is the positive
        signal that the condition evaluated True. Asserting on the
        *absence* of the excluded files would be a weaker test, since
        a bug in the planner (files silently dropped) could produce
        the same absence.
        """
        plan = _pipeline(self.FIXTURE, cli_vars={"database": "postgresql"})
        destinations = {p.destination for p in plan}

        assert "db/postgres.py" in destinations
        assert "tests/test_postgres.py" in destinations
        # Unconditional files are always present regardless of the
        # condition — assert one to prove the condition only gated
        # the conditional group, not the whole blueprint.
        assert "README.md" in destinations

    def test_sqlite_excludes_the_db_group(self) -> None:
        """``database=sqlite`` → the ``db/`` files are *not* planned.

        The counterpart test. The absence of ``db/postgres.py`` proves
        the condition evaluated False. The presence of ``README.md``
        proves the pipeline still produced the unconditional files —
        a bug that skipped the whole blueprint on a False condition
        would fail this assertion.

        Note the *shape* of the assertion: ``not in`` on a set of
        destinations, not on a sequence. Order does not matter for
        exclusion checks, and using a set makes that intent obvious.
        """
        plan = _pipeline(self.FIXTURE, cli_vars={"database": "sqlite"})
        destinations = {p.destination for p in plan}

        assert "db/postgres.py" not in destinations
        assert "tests/test_postgres.py" not in destinations
        assert "README.md" in destinations


# ---------------------------------------------------------------------------
# Scenario 2 — boolean condition includes and excludes correctly.
#
# ``boolean-docker.yaml`` declares ``include_docker`` (boolean,
# default ``true``) and a conditional group (``Dockerfile``) guarded
# by ``include_docker == true``.
#
# Three cases worth exercising (and one more below in the resolver
# default-flow section):
#     * default true, no override → included
#     * explicit false → excluded
#     * explicit true → included (redundant with default true, but
#       distinguishes "default flow" from "explicit flow")
# ---------------------------------------------------------------------------
class TestBooleanCondition:
    """``include_docker == true`` controls the ``Dockerfile`` group."""

    FIXTURE = "boolean-docker.yaml"

    def test_default_true_includes_dockerfile(self) -> None:
        """No CLI override → blueprint default ``true`` → Dockerfile included.

        The most common real-world case: a blueprint author ships
        Docker by default, the user does not object, the Dockerfile
        is generated. Testing this end-to-end (resolver materialises
        the default, evaluator compares against it) proves the
        resolver→evaluator handoff is correct for the default path.
        """
        plan = _pipeline(self.FIXTURE, cli_vars={})
        assert "Dockerfile" in {p.destination for p in plan}

    def test_explicit_false_excludes_dockerfile(self) -> None:
        """``--var include_docker=false`` → Dockerfile *not* planned.

        Proves the resolver's CLI-precedence rule reaches the
        evaluator. A resolver bug that dropped the CLI value would
        leave ``include_docker`` at its default (``true``) and the
        Dockerfile would silently appear — a bug a naive test would
        miss.
        """
        plan = _pipeline(self.FIXTURE, cli_vars={"include_docker": "false"})
        assert "Dockerfile" not in {p.destination for p in plan}

    def test_explicit_true_includes_dockerfile(self) -> None:
        """``--var include_docker=true`` → Dockerfile planned.

        Redundant with the default case *in outcome*, but not in
        *mechanism* — this test exercises the explicit-CLI path, and
        a resolver bug that mishandled a boolean CLI value (e.g.
        string ``"true"`` not coerced to ``True``) would fail here
        while the default case still passed.
        """
        plan = _pipeline(self.FIXTURE, cli_vars={"include_docker": "true"})
        assert "Dockerfile" in {p.destination for p in plan}


# ---------------------------------------------------------------------------
# Scenario 3 — the fixture set covers every operator and literal kind
# that can be *exercised end-to-end through the resolver*.
#
# A previous fixture (``integer-python.yaml``) was removed because the
# blueprint schema has no integer ``VariableType``: ``enum`` resolves
# to a string, and ``python_major == 3`` correctly fails under the
# type-aware rule. Integer comparison is covered at the unit level
# (``test_evaluator.py::TestTypeAwareComparison``); it is not
# integration-testable through the resolver today. See the module
# docstring's "What this file does *not* test" section.
#
# The mapping below is *data*, not a loop, so a reviewer can read
# the coverage at a glance. Each tuple is ``(fixture, cli_vars,
# expected_conditional_file)`` — a happy path that proves the fixture
# exercises the form it is named for.
# ---------------------------------------------------------------------------
class TestGrammarFormCoverage:
    """Each resolver-exercisable grammar form has a fixture that exercises it."""

    @pytest.mark.parametrize(
        ("fixture", "cli_vars", "expected_file"),
        [
            # Equality with a single-quoted string.
            ("database.yaml", {"database": "postgresql"}, "db/postgres.py"),
            # Equality with a double-quoted string.
            ("double-quoted.yaml", {"database": "postgresql"}, "db/postgres.py"),
            # Boolean equality (include_docker == true).
            ("boolean-docker.yaml", {}, "Dockerfile"),
            # Inequality with a string (database != 'sqlite').
            ("not-equals.yaml", {"database": "postgresql"}, "db/engine.py"),
            # Mixed conditions (multiple surviving groups).
            ("mixed.yaml", {"database": "postgresql"}, "db/postgres.py"),
        ],
    )
    def test_form_includes_expected_file(
        self, fixture: str, cli_vars: dict[str, Any], expected_file: str
    ) -> None:
        """The named fixture includes the file its condition guards.

        Each row proves that the fixture *actually exercises* the
        grammar form it is named for — not just that it loads. A
        typo in the condition (say, ``database == 'postgresql'``
        becoming ``database == 'postgresgl'``) would make the fixture
        load but never include the file, and this test would catch it.

        The cli_vars are typed as ``dict[str, Any]`` because most
        are strings (the resolver coerces them to the variable's
        declared type — passing ``"false"`` for a boolean is the
        *normal* case the resolver exists to handle). Typing them
        as ``str`` would mislead a reviewer into thinking raw
        strings reach the evaluator.
        """
        plan = _pipeline(fixture, cli_vars=cli_vars)
        destinations = {p.destination for p in plan}
        assert expected_file in destinations, (
            f"fixture {fixture!r} did not include {expected_file!r} "
            f"for cli_vars={cli_vars!r}; got destinations="
            f"{sorted(destinations)!r}"
        )


# ---------------------------------------------------------------------------
# Scenario 4 — intentionally-failing fixtures.
#
# Each of these fixtures is *deliberately* invalid at the condition
# layer — they load cleanly (the loader validates structure, not
# conditions) and they resolve cleanly (their declared variables all
# have defaults), so the failure surfaces at the *evaluator* with a
# condition-specific ``code``. Testing the failure mode (not just "it
# failed") is the point: each fixture is associated with the specific
# code a user would see, and the specific stage that produces it.
# ---------------------------------------------------------------------------
class TestIntentionalFailures:
    """Failing fixtures produce the documented error code, at the right stage."""

    def test_undefined_variable_fails_at_the_evaluator(self) -> None:
        """``undefined-variable.yaml`` → ``condition.undefined_variable``.

        The fixture declares no variables but has a condition
        referencing ``missing_var``. The loader accepts it (the
        schema only validates condition *strings*, not their
        references); the resolver produces ``ResolvedValues()``; the
        evaluator is the stage that fails.

        Asserting on *which stage* fails distinguishes "the evaluator
        correctly detected the problem" from "the resolver or loader
        accidentally rejected the fixture for a different reason".
        The latter would produce a valid ``Err`` but with the wrong
        code — a bug the stage-aware assertion catches.
        """
        path = _CONDITIONS_FIXTURES / "undefined-variable.yaml"
        blueprint = _load_blueprint(path)
        resolved = _resolve(blueprint, cli_vars={})  # must succeed

        result = ConditionalEvaluator().evaluate(blueprint, resolved.as_dict())
        assert isinstance(result, Err), (
            "the evaluator should have produced an error for "
            "undefined-variable.yaml; got a successful result"
        )
        codes = {f.code for f in result.error.findings}
        assert codes == {
            "condition.undefined_variable"
        }, f"expected a single undefined-variable finding, got {codes!r}"

    def test_bad_syntax_fails_at_the_evaluator(self) -> None:
        """``bad-syntax.yaml`` → ``condition.syntax``.

        Same shape as the undefined-variable case. The interesting
        difference is that the finding is produced by the *parser*
        (via ``to_finding``), not the evaluator — but from the
        caller's perspective, both are findings on a ``ConditionError``
        from the same pipeline stage. The test asserts on the code,
        not on the producer, because the producer is an implementation
        detail (see ADR-0016's "no exception hierarchy").
        """
        path = _CONDITIONS_FIXTURES / "bad-syntax.yaml"
        blueprint = _load_blueprint(path)
        resolved = _resolve(blueprint, cli_vars={})

        result = ConditionalEvaluator().evaluate(blueprint, resolved.as_dict())
        assert isinstance(result, Err)
        assert any(f.code == "condition.syntax" for f in result.error.findings), (
            f"expected a syntax finding, got " f"{[f.code for f in result.error.findings]!r}"
        )

    def test_invalid_condition_variable_fails_at_the_parser(self) -> None:
        """``invalid-condition-variable.yaml`` → ``condition.syntax`` (parse stage).

        The fixture's condition uses ``Database`` (PascalCase), which
        is not a valid identifier in the DSL. Every grammar rule's
        regex matches its ``var`` group against ``[a-z_][a-z0-9_]*``,
        so a PascalCase name fails the pattern at *match* time — the
        whole condition is rejected as a syntax error, before any
        rule-specific branch runs.

        The specific code ``condition.invalid_variable`` is a
        *defence-in-depth* check inside the parser: it only fires if
        a future grammar rule's ``var`` regex is loosened to admit
        identifier-shaped text that ``VARIABLE_NAME_PATTERN`` rejects.
        Under the current grammar that branch is intentionally
        unreachable, and a hypothetical loosening is covered by
        ``test_parser.py::test_variable_name_recheck_is_redundant_but_correct``
        rather than by this integration test.

        **The invariant this test *does* enforce** is the failure
        *stage*: the parser catches the invalid identifier before the
        evaluator ever sees the condition. If a future refactor moved
        identifier validation downstream, an invalid name would be
        classified as ``condition.undefined_variable`` — a different
        code with a different hint, telling the user "add the
        variable" when the real fix is "spell it legally". That
        specific regression is asserted on below.

        The name ``..._fails_at_the_parser`` replaces an earlier
        ``..._fails_at_the_evaluator`` because a test name that
        contradicts the actual failure stage is a name that misleads
        future readers about where to look.
        """
        path = _CONDITIONS_FIXTURES / "invalid-condition-variable.yaml"
        blueprint = _load_blueprint(path)
        resolved = _resolve(blueprint, cli_vars={})

        result = ConditionalEvaluator().evaluate(blueprint, resolved.as_dict())
        assert isinstance(result, Err), (
            "the pipeline should have produced an error for "
            "invalid-condition-variable.yaml; got a successful result"
        )
        codes = {f.code for f in result.error.findings}

        # The invariant: the failure is at *parse* time, not at
        # *evaluation* time. Either parse-stage code is acceptable.
        parse_stage_codes = {"condition.syntax", "condition.invalid_variable"}
        assert codes & parse_stage_codes, (
            f"expected a parse-stage finding (one of "
            f"{sorted(parse_stage_codes)!r}), got {codes!r}; if the "
            "identifier check has moved to a later stage, update this "
            "test and the ADR that justifies the new location"
        )

        # The regression we specifically guard against: an
        # ``undefined_variable`` classification here would mean the
        # parser accepted an invalid identifier and pushed the problem
        # downstream, where it would be misdiagnosed.
        assert "condition.undefined_variable" not in codes, (
            "an invalid identifier was classified as 'undefined "
            "variable'; the parser is letting invalid names through "
            "to the evaluator, and the error's hint will be wrong"
        )


# ---------------------------------------------------------------------------
# Scenario 5 — collisions between conditional and unconditional files.
#
# ``duplicate-destination.yaml`` has an unconditional file at
# ``README.md`` and a conditional file (guarded by
# ``include_override == true``, default true) that also targets
# ``README.md``. When the condition holds, both files reach the
# planner and collide; when it does not, only the unconditional file
# is planned.
#
# This is the most important scenario in the file, because it proves
# the planner's collision detection works *across* the conditional
# boundary — a class of bug that would be invisible in tests that
# only exercise unconditional files.
# ---------------------------------------------------------------------------
class TestConditionalCollision:
    """Conditional files participate in collision detection."""

    FIXTURE = "duplicate-destination.yaml"

    def test_collision_detected_when_condition_is_true(self) -> None:
        """Both files plan to ``README.md`` → ``destination.collision``.

        The planner's safe-by-default rule means the conditional group
        only reaches it when the condition holds. With the default
        (``include_override: true``), both files reach the planner and
        collide — the correct failure mode, and the one a blueprint
        author who wrote this fixture *should* hit.

        Asserting on the *code* (not just that an error occurred)
        distinguishes this from other planner failures (a missing
        template, an unsafe path, a bad substitution). A user reading
        the ``destination.collision`` hint knows exactly what to fix.
        """
        path = _CONDITIONS_FIXTURES / self.FIXTURE
        blueprint = _load_blueprint(path)
        resolved = _resolve(blueprint, cli_vars={})
        survivors = _evaluate(blueprint, resolved)

        result = FileMapper().map(
            blueprint,
            resolved.as_dict(),
            conditional_groups=survivors,
        )
        assert isinstance(result, Err), (
            "the planner should have reported a collision for "
            f"{self.FIXTURE}; got a successful plan"
        )
        codes = {f.code for f in result.error.findings}
        assert "destination.collision" in codes, f"expected a collision finding, got {codes!r}"

    def test_no_collision_when_condition_is_false(self) -> None:
        """``include_override=false`` → no collision, plan succeeds.

        The complementary case. If the condition is false, the
        conditional file is filtered out by the evaluator before
        reaching the planner, so no collision occurs. A bug that
        failed to filter the conditional group (say, because the
        evaluator returned survivors that the planner ignored) would
        cause the collision to persist — this test catches it.

        Note this test also proves a *property of the composition*:
        the planner's ``conditional_groups`` parameter actually
        controls inclusion. The unconditional planner API has always
        taken the flag; this test proves the evaluator's output is
        wired into it correctly.
        """
        path = _CONDITIONS_FIXTURES / self.FIXTURE
        blueprint = _load_blueprint(path)
        resolved = _resolve(blueprint, cli_vars={"include_override": "false"})
        survivors = _evaluate(blueprint, resolved)
        # The evaluator must have filtered the group out.
        assert survivors == [], (
            f"expected no surviving groups when include_override=false; " f"got {survivors!r}"
        )

        result = FileMapper().map(
            blueprint,
            resolved.as_dict(),
            conditional_groups=survivors,
        )
        assert isinstance(result, Ok), (
            f"expected a successful plan when the condition is false; " f"got {result!r}"
        )
        # Only the unconditional file is planned.
        destinations = {p.destination for p in result.value}
        assert destinations == {"README.md"}


# ---------------------------------------------------------------------------
# Scenario 6 — determinism.
#
# The whole pipeline must be deterministic: same fixture + same
# cli_vars → identical plan. This section is short but load-bearing:
# deterministic output is what makes the CLI diffable in CI and what
# makes snapshot tests of the plan stable. A future refactor that
# introduced a set-based iteration somewhere in the pipeline would
# produce the same *set* of files in a nondeterministic order, and
# downstream tooling would start flaking.
# ---------------------------------------------------------------------------
class TestPipelineDeterminism:
    """Same inputs → identical plan (identity, not just equality)."""

    @pytest.mark.parametrize(
        "fixture",
        [
            "database.yaml",
            "boolean-docker.yaml",
            "not-equals.yaml",
            "mixed.yaml",
        ],
    )
    def test_repeated_runs_produce_identical_plans(self, fixture: str) -> None:
        """Two runs of the same pipeline produce byte-identical plans.

        Compares the ``PlannedFile`` lists directly (not just the
        destinations): a nondeterministic *ordering* within a file's
        fields, or a nondeterministic choice between two files with
        the same destination, would still be caught.

        The list of fixtures is a subset of the happy-path fixtures —
        the ones most likely to exercise different pipeline paths
        (enum coercion, boolean coercion, inequality, multi-group
        filtering). A nondeterminism bug would more likely show up on
        a specific path than universally, so the subset is chosen for
        path coverage rather than exhaustiveness.
        """
        first = _pipeline(fixture, cli_vars={})
        second = _pipeline(fixture, cli_vars={})
        assert first == second, (
            f"pipeline is nondeterministic for {fixture!r}: two runs " "produced different plans"
        )


# ---------------------------------------------------------------------------
# Scenario 7 — the plan's kind classification is correct.
#
# The planner distinguishes ``RENDER`` (templated) from ``COPY``
# (binary/untemplated) based on file extensions. For conditional
# files, the classification must be correct *after* the conditional
# filter — a bug that classified files before filtering could apply
# the wrong rule to filtered-out files and, more importantly, might
# apply it inconsistently to survivors.
#
# This test exercises classification end-to-end through the
# conditional pipeline, which no other test in the suite does.
# ---------------------------------------------------------------------------
class TestPlannedFileKinds:
    """Surviving files get the correct ``RENDER``/``COPY`` classification."""

    def test_rendered_and_copied_files_classified_correctly(self) -> None:
        """``.j2`` sources are ``RENDER``; non-template sources are ``COPY``.

        Every fixture's template stubs are ``.j2`` files, so every
        planned file is ``RENDER``. If a future fixture adds a binary
        asset, this test must be extended to assert on the ``COPY``
        side of the classification — which is exactly the coverage
        the assertion structure below makes easy to add.

        The test's current value is proving that conditional-file
        planning preserves the classification logic from the
        unconditional path (which is tested in ``test_mapper.py``).
        If a future refactor split the classifier between the two
        paths, this test would catch the divergence for the survivors.
        """
        plan = _pipeline("database.yaml", cli_vars={"database": "postgresql"})
        # Every fixture currently uses .j2 templates.
        assert all(p.kind is PlannedFileKind.RENDER for p in plan), (
            f"expected all files to be RENDER (fixtures use .j2 "
            f"templates); got kinds {[p.kind for p in plan]!r}"
        )


# ---------------------------------------------------------------------------
# Scenario 8 — the plan is sorted.
#
# The planner sorts by destination. This is a *property of the API*,
# not an incidental detail — downstream tooling (staging, dry-run
# rendering, diffing generated output) depends on it. A future
# refactor of the planner's sort (e.g. replacing ``sort(key=...)``
# with a set iteration) would produce the same *set* of files in a
# different *order* and would silently break every downstream
# consumer.
# ---------------------------------------------------------------------------
class TestPlanSorting:
    """Plans are sorted by destination ascending."""

    @pytest.mark.parametrize(
        "fixture",
        ["database.yaml", "boolean-docker.yaml", "not-equals.yaml"],
    )
    def test_plans_are_sorted_by_destination(self, fixture: str) -> None:
        """The plan's destinations are in ascending lexicographic order.

        Sorted-ness is easier to assert on the raw destination strings
        than on the ``PlannedFile`` objects — the ``sort`` key is
        ``destination``, so ``sorted(destinations)`` is exactly the
        invariant. Asserting on the ``PlannedFile`` list would work
        too, but would require a key extraction that duplicates the
        planner's implementation detail.
        """
        plan = _pipeline(fixture, cli_vars={})
        destinations = [p.destination for p in plan]
        assert destinations == sorted(destinations), (
            f"plan for {fixture!r} is not sorted by destination: " f"{destinations!r}"
        )


# ---------------------------------------------------------------------------
# Sanity check on the fixture corpus.
#
# Integration tests are only as good as the fixtures they exercise.
# If a fixture is added but never referenced by any test, or removed
# while a test still references it, the mismatch is invisible until
# something breaks in production. This test catches both — it walks
# the fixtures and the referenced names, and asserts every fixture is
# exercised by at least one of the scenario classes above.
# ---------------------------------------------------------------------------
class TestFixtureCorpus:
    """Every fixture under the conditions tree is exercised by a test."""

    # The set of fixtures referenced by name in the scenario classes
    # above. Kept as an explicit list (not derived from the classes)
    # because deriving it would require reflection over pytest
    # parametrisation, which is exactly the kind of implicit
    # coupling this test exists to prevent. Explicit wins here.
    _REFERENCED = frozenset(
        {
            # Happy path (test_pipeline_runs_for_valid_fixture covers all
            # non-failing fixtures via glob, but the failing fixtures need
            # explicit references):
            "database.yaml",
            "double-quoted.yaml",
            "boolean-docker.yaml",
            "not-equals.yaml",
            "mixed.yaml",
            "empty-conditional.yaml",
            # Intentional failures (referenced by TestIntentionalFailures
            # and TestConditionalCollision):
            "undefined-variable.yaml",
            "bad-syntax.yaml",
            "invalid-condition-variable.yaml",
            "duplicate-destination.yaml",
        }
    )

    def test_every_fixture_is_referenced(self) -> None:
        """Every ``.yaml`` under the conditions fixtures is used by a test.

        An unreferenced fixture is dead weight: it costs maintenance
        and gives false confidence ("we test conditional evaluation
        thoroughly") without exercising anything. This test catches
        the "new fixture added but no test for it" case — which is
        the most common way a fixture corpus silently rots.

        The check is *strict*: unreferenced fixtures fail, not just
        unreferenced-and-unused-in-the-pipeline. A fixture that is
        loaded somewhere but not referenced by a *named* test is
        still technically exercised by the glob-driven happy-path
        test, but the glob test only asserts the pipeline runs — it
        does not assert what the fixture's *name* claims the fixture
        demonstrates. Named references are the marker that a
        reviewer has looked at the fixture and thought about what it
        proves.
        """
        on_disk = {p.name for p in _CONDITIONS_FIXTURES.glob("*.yaml")}
        unreferenced = on_disk - self._REFERENCED
        assert not unreferenced, (
            f"fixtures not referenced by any test: {sorted(unreferenced)!r}. "
            "Either add a test that exercises the fixture or delete it."
        )

    def test_every_referenced_fixture_exists(self) -> None:
        """Every name in ``_REFERENCED`` corresponds to a real file.

        The complement: a fixture that was renamed or deleted but is
        still referenced by a test name would fail *at that test's
        runtime*, which produces a less informative failure than this
        check does. Running this check first means a missing fixture
        fails with a clear "the fixture was removed" message, not with
        a downstream "pipeline could not load the blueprint" message
        that points at the wrong stage.
        """
        missing = {name for name in self._REFERENCED if not (_CONDITIONS_FIXTURES / name).is_file()}
        assert not missing, (
            f"referenced fixtures not on disk: {sorted(missing)!r}. "
            "Either the fixture was deleted (update the reference) "
            "or the reference has a typo (fix the name)."
        )
