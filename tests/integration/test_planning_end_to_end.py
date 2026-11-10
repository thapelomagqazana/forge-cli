"""End-to-end tests for the planning phase across the real pipeline.

Every other test file for planning (`test_destinations.py`,
`test_collisions.py`, `test_stable_order.py`, `test_literal_copy.py`,
`test_path_traversal.py`) exercises **one behaviour in isolation**,
with fixtures constructed by hand. That isolation is what lets those
tests name exactly which rule broke when they fail — but it also means
none of them prove the *composition*. A blueprint read from a real
YAML file, passed through the loader, resolved against real variables,
and turned into a plan by the real `FileMapper` — that is what this
file exercises, and it is what a user actually runs.

What "integration" means here
-----------------------------
The test boundaries are:

    YAML file on disk
        ↓  (BlueprintLoader — parse + schema + model + fs checks)
    Blueprint (typed, validated)
        ↓  (VariableResolver — flags > prompts > defaults)
    ResolvedValues (immutable)
        ↓  (FileMapper — path substitution + kind classification + ordering)
    list[PlannedFile] (sorted, collision-checked)

Each arrow is a real function; nothing is stubbed. Files live in
`tmp_path` so each test owns its inputs and cleanup is automatic. The
assertions at the end are on the *plan* — the observable output of the
whole pipeline — not on intermediate state, because a refactor that
preserves the plan's shape is allowed, and a refactor that changes it
is not.

Why not route through the CLI
-----------------------------
`forge create` is a *command*, not a *planning API*. It adds argument
parsing, prompt dispatch, error rendering, and the writer hand-off on
top of the pipeline this file exercises. Testing the planner through
the CLI would conflate "the plan is correct" with "the CLI invoked the
planner correctly" — two properties that deserve two test files. The
CLI's own tests cover the wiring; this file covers the plan.

Fixtures
--------
Each test builds its blueprint inline, writes it to `tmp_path`, and
cleans up by virtue of `tmp_path`'s lifecycle. Two helpers do the work:

* ``write_blueprint(root, text)`` — writes ``blueprint.yaml``.
* ``write_template(root, relpath, payload)`` — creates a template file,
  creating parent directories as needed. Template payloads may be
  ``str`` (rendered) or ``bytes`` (binary) — the helper dispatches.

Both write to disk because the loader's `check_templates` phase
verifies file existence, and using a mocked loader would skip the
integration this file is meant to exercise.

A note on failures worth remembering
------------------------------------
Two bugs in earlier versions of this file were *the test's* fault, not
the production code's, and both taught the same lesson — "assert the
phase that failed, and its message, not just that some failure
happened":

* ``write_template`` originally did not create parent directories, so
  every test with a nested template path failed with ``FileNotFoundError``
  before the pipeline even ran. The failure was the test's, and the
  fix was to make the helper do what "create a template at this path"
  actually means.
* Five tests called ``BlueprintLoader().load(bp_path).value`` directly,
  assuming load succeeds. When the fixture YAML was invalid for
  unrelated reasons, the failure surfaced as ``AttributeError: 'Err'
  object has no attribute 'value'`` — an error that tells a future
  debugger almost nothing. The corrected helper ``_load_ok`` asserts
  ``is_ok()`` first and, on failure, dumps the findings, so the
  message names *which* field the schema rejected and *why*.
"""

from __future__ import annotations

from pathlib import Path

from forge.loading import BlueprintLoader
from forge.models import Blueprint
from forge.planning import FileMapper, PlannedFile, PlannedFileKind
from forge.resolution import ResolvedValues, VariableResolver


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------
def write_blueprint(root: Path, body: str) -> Path:
    """Write ``body`` as ``root/blueprint.yaml`` and return the path.

    ``body`` must already be a well-formed YAML document at column 0 —
    callers are expected to construct it with ``_minimal_blueprint`` or
    an equivalent f-string, not to indent it and rely on ``dedent``.
    That discipline is deliberate: ``dedent`` is easy to misapply when
    the string contains f-string interpolations, and a silently
    under-dedented YAML is a real source of "why did the loader reject
    this?" confusion.
    """
    path = root / "blueprint.yaml"
    path.write_text(body)
    return path


def write_template(root: Path, relpath: str, payload: str | bytes = "") -> None:
    """Create a template file at ``root / relpath`` with ``payload``.

    Creates parent directories as needed. A blueprint can (and the
    fixtures in this file do) reference templates in nested directories
    like ``templates/main.py.j2``; requiring every caller to
    ``mkdir`` the parent first was the earlier design, and it failed
    every nested-template test with ``FileNotFoundError`` before the
    pipeline even ran. The helper now does what "create a template at
    this path" means.

    ``payload`` may be ``str`` (typical for templates) or ``bytes``
    (typical for binary fixtures like ``logo.png``). ``Path.write_text``
    raises ``TypeError`` on bytes, so the branch is required, not
    decorative.
    """
    target = root / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, bytes):
        target.write_bytes(payload)
    else:
        target.write_text(payload)


def _load_ok(path: Path) -> Blueprint:
    """Load ``path`` and assert success, dumping findings on failure.

    ``BlueprintLoader().load(...).value`` was the pattern used in
    earlier tests and it failed with ``AttributeError: 'Err' object has
    no attribute 'value'`` whenever the fixture YAML was invalid for
    unrelated reasons. This helper fails with the *actual* findings, so
    a future debugger sees which field the schema rejected and why,
    instead of a cryptic attribute error.
    """
    result = BlueprintLoader().load(path)
    assert result.is_ok(), (
        "expected the loader to accept the fixture blueprint, got "
        f"{[e.format() for e in result.error]!r}"
    )
    return result.value


def _resolve_ok(blueprint: Blueprint, cli_args: dict | None = None) -> ResolvedValues:
    """Resolve ``blueprint`` against ``cli_args`` and assert success."""
    result = VariableResolver().resolve(blueprint, cli_args or {})
    assert result.is_ok(), (
        "expected the resolver to accept the fixture variables, got "
        f"{[e.format() for e in result.error]!r}"
    )
    return result.value


def _minimal_blueprint(
    *,
    files_block: str,
    variables_block: str = "",
) -> str:
    """Return a minimal-but-valid blueprint YAML with the given blocks.

    The blocks are inserted at **column 0** — callers must construct
    them that way — and the surrounding skeleton is built with plain
    string concatenation, not ``dedent``. That removes the entire class
    of "``dedent`` disagreed with ``lstrip`` disagreed with what the
    schema wanted" bugs that earlier versions of this file hit.

    The template assumes the caller's blocks contain their own leading
    indentation for the YAML items (``"  - source: …"`` for files,
    ``"  - id: …"`` for variables). That is unavoidable — YAML lists
    inside a mapping need to be indented — and it is the one place the
    helper trusts the caller.
    """
    variables_section = "variables:\n" + variables_block + "\n" if variables_block else ""
    return (
        'schema_version: "1.0.0"\n'
        "id: sample.e2e\n"
        "name: End-to-End Fixture\n"
        "version: 1.0.0\n"
        "description: A fixture for end-to-end planning tests.\n"
        "category: api\n" + variables_section + "files:\n" + files_block + "\n"
        "verification:\n"
        "  - id: ok\n"
        "    type: required_file\n"
        "    path: README.md\n"
        "compatibility:\n"
        "  min_forge_version: 1.0.0\n"
    )


# ---------------------------------------------------------------------------
# Happy path: the plan a real blueprint produces
# ---------------------------------------------------------------------------
class TestHappyPath:
    """A valid blueprint + a valid variable set → a correct plan.

    This is the test that, if everything else passed, would give the
    most confidence that the pipeline composes correctly. It asserts on
    the *whole plan* — destinations, sources, kinds, and order — so a
    regression in any phase (loader dropping a file, resolver
    mis-substituting, mapper mis-ordering) fails here.
    """

    def test_full_pipeline_produces_the_expected_plan(self, tmp_path: Path) -> None:
        """Load → resolve → map produces the exact expected plan."""
        bp_path = write_blueprint(
            tmp_path,
            _minimal_blueprint(
                variables_block=(
                    "  - id: project_name\n"
                    "    type: string\n"
                    "    description: Project name.\n"
                    "    required: true"
                ),
                files_block=(
                    "  - source: templates/main.py.j2\n"
                    "    destination: src/{{ project_name }}/main.py\n"
                    "  - source: templates/README.md.j2\n"
                    "    destination: README.md\n"
                    "  - source: assets/logo.png\n"
                    "    destination: src/{{ project_name }}/assets/logo.png"
                ),
            ),
        )
        write_template(tmp_path, "templates/main.py.j2", "{{ project_name }}\n")
        write_template(tmp_path, "templates/README.md.j2", "# {{ project_name }}\n")
        write_template(tmp_path, "assets/logo.png", b"\x89PNG\r\n\x1a\n")

        blueprint = _load_ok(bp_path)
        variables = _resolve_ok(blueprint, {"project_name": "my-app"})
        plan_result = FileMapper().map(blueprint, variables.as_dict())
        assert plan_result.is_ok(), plan_result
        plan = plan_result.value

        # Assert on the *whole plan*: destinations in order, sources
        # aligned with them, kinds correct.
        assert [(p.destination, p.source, p.kind) for p in plan] == [
            ("README.md", "templates/README.md.j2", PlannedFileKind.RENDER),
            (
                "src/my-app/assets/logo.png",
                "assets/logo.png",
                PlannedFileKind.COPY,
            ),
            (
                "src/my-app/main.py",
                "templates/main.py.j2",
                PlannedFileKind.RENDER,
            ),
        ]

    def test_plan_is_deterministic_across_runs(self, tmp_path: Path) -> None:
        """Re-running the whole pipeline produces identical plans."""
        bp_path = write_blueprint(
            tmp_path,
            _minimal_blueprint(
                files_block=(
                    "  - source: a.j2\n"
                    "    destination: out/z.py\n"
                    "  - source: b.j2\n"
                    "    destination: out/a.py\n"
                    "  - source: c.j2\n"
                    "    destination: out/m.py"
                ),
            ),
        )
        for name in ("a.j2", "b.j2", "c.j2"):
            write_template(tmp_path, name, "# {{ x }}\n")

        def run() -> list[PlannedFile]:
            bp = _load_ok(bp_path)
            variables = _resolve_ok(bp)
            plan_result = FileMapper().map(bp, variables.as_dict())
            assert plan_result.is_ok(), plan_result
            return plan_result.value

        first = run()
        second = run()
        assert first == second


# ---------------------------------------------------------------------------
# Defaults come from the blueprint, not from the caller
# ---------------------------------------------------------------------------
class TestDefaultsFlowThroughPipeline:
    """Blueprint ``default`` values flow through the resolver into the plan."""

    def test_default_variable_substitutes_into_destination(self, tmp_path: Path) -> None:
        bp_path = write_blueprint(
            tmp_path,
            _minimal_blueprint(
                variables_block=(
                    "  - id: name\n"
                    "    type: string\n"
                    "    description: Package name.\n"
                    "    required: true\n"
                    "    default: default-pkg"
                ),
                files_block=("  - source: main.j2\n" '    destination: "src/{{ name }}/main.py"'),
            ),
        )
        write_template(tmp_path, "main.j2", "# {{ name }}\n")

        bp = _load_ok(bp_path)
        variables = _resolve_ok(bp)
        plan_result = FileMapper().map(bp, variables.as_dict())
        assert plan_result.is_ok(), plan_result
        assert [p.destination for p in plan_result.value] == ["src/default-pkg/main.py"]


# ---------------------------------------------------------------------------
# Failures propagate through the pipeline
# ---------------------------------------------------------------------------
class TestFailurePropagation:
    """Failures in one phase surface *as failures from that phase*.

    A loader that swallowed a missing template, or a planner that
    silently skipped a bad destination, would still pass all the
    "happy path" tests. These tests pin that the pipeline's **contract
    is to fail loudly at the phase that can identify the problem**.
    """

    def test_missing_template_fails_at_load_time(self, tmp_path: Path) -> None:
        """A referenced template that does not exist fails the *loader*."""
        bp_path = write_blueprint(
            tmp_path,
            _minimal_blueprint(
                files_block=("  - source: does-not-exist.j2\n" "    destination: out/main.py"),
            ),
        )
        # Deliberately do NOT write the template file.

        result = BlueprintLoader().load(bp_path)
        assert result.is_err()
        # The error must be about the *missing template*, not about the
        # YAML being malformed. If a future change to the loader turned
        # this into a schema error, the assertion below would catch it
        # (the message would not contain "missing").
        assert any(
            "missing" in (e.message or "").lower() for e in result.error
        ), f"expected a missing-template error, got {[e.format() for e in result.error]!r}"

    def test_unresolved_required_variable_fails_at_resolve_time(self, tmp_path: Path) -> None:
        """A required variable with no value fails the *resolver*."""
        bp_path = write_blueprint(
            tmp_path,
            _minimal_blueprint(
                variables_block=(
                    "  - id: name\n"
                    "    type: string\n"
                    "    description: Package name.\n"
                    "    required: true"
                ),
                files_block=("  - source: main.j2\n" '    destination: "src/{{ name }}/main.py"'),
            ),
        )
        write_template(tmp_path, "main.j2", "")

        bp = _load_ok(bp_path)
        result = VariableResolver().resolve(bp, {})
        assert result.is_err()
        assert any(e.code == "required.missing" for e in result.error)

    def test_collision_fails_at_map_time(self, tmp_path: Path) -> None:
        """Two sources with the same destination fail the *planner*."""
        bp_path = write_blueprint(
            tmp_path,
            _minimal_blueprint(
                files_block=(
                    "  - source: a.j2\n"
                    "    destination: out/duplicate.py\n"
                    "  - source: b.j2\n"
                    "    destination: out/duplicate.py"
                ),
            ),
        )
        write_template(tmp_path, "a.j2", "")
        write_template(tmp_path, "b.j2", "")

        bp = _load_ok(bp_path)
        variables = _resolve_ok(bp)
        result = FileMapper().map(bp, variables.as_dict())
        assert result.is_err()
        assert any(f.code == "destination.collision" for f in result.error.findings)


# ---------------------------------------------------------------------------
# Plan ordering is the pipeline's ordering, not any phase's
# ---------------------------------------------------------------------------
class TestPipelineOrdering:
    """The plan's order comes from the composition, not any single phase."""

    def test_plan_order_matches_planner_not_blueprint(self, tmp_path: Path) -> None:
        bp_path = write_blueprint(
            tmp_path,
            _minimal_blueprint(
                files_block=(
                    "  - source: z.j2\n"
                    "    destination: out/z.py\n"
                    "  - source: a.j2\n"
                    "    destination: out/a.py\n"
                    "  - source: m.j2\n"
                    "    destination: out/m.py"
                ),
            ),
        )
        for name in ("z.j2", "a.j2", "m.j2"):
            write_template(tmp_path, name, "")

        bp = _load_ok(bp_path)
        plan_result = FileMapper().map(bp, {})
        assert plan_result.is_ok(), plan_result
        assert [p.destination for p in plan_result.value] == [
            "out/a.py",
            "out/m.py",
            "out/z.py",
        ]


# ---------------------------------------------------------------------------
# Kind classification end-to-end
# ---------------------------------------------------------------------------
class TestKindClassificationEndToEnd:
    """``render`` vs ``copy`` classification works on a real blueprint."""

    def test_mixed_binary_and_template_blueprint(self, tmp_path: Path) -> None:
        bp_path = write_blueprint(
            tmp_path,
            _minimal_blueprint(
                files_block=(
                    "  - source: templates/main.j2\n"
                    "    destination: src/main.py\n"
                    "  - source: assets/logo.png\n"
                    "    destination: static/logo.png\n"
                    "  - source: README.md\n"
                    "    destination: README.md"
                ),
            ),
        )
        write_template(tmp_path, "templates/main.j2", "# {{ x }}\n")
        write_template(tmp_path, "assets/logo.png", b"\x89PNG\r\n\x1a\n")
        write_template(tmp_path, "README.md", "# hi\n")

        bp = _load_ok(bp_path)
        plan_result = FileMapper().map(bp, {})
        assert plan_result.is_ok(), plan_result
        by_destination = {p.destination: p.kind for p in plan_result.value}
        assert by_destination["src/main.py"] is PlannedFileKind.RENDER
        assert by_destination["static/logo.png"] is PlannedFileKind.COPY
        assert by_destination["README.md"] is PlannedFileKind.COPY


# ---------------------------------------------------------------------------
# The pipeline never partially succeeds
# ---------------------------------------------------------------------------
class TestNoPartialPlans:
    """A failure at any phase produces *no* plan, not a partial one."""

    def test_mixed_valid_and_traversal_blueprint_rejects_whole_plan(self, tmp_path: Path) -> None:
        """One good file + one traversal → no plan at all.

        The traversal case is currently rejected at ``FileEntry``
        construction (Pydantic pattern), which is the earliest
        checkpoint. A future change that deferred the check to the
        loader would still reject — this test asserts on the invariant
        ("no plan is produced"), not on the phase that enforces it.
        """
        bp_path = write_blueprint(
            tmp_path,
            _minimal_blueprint(
                files_block=(
                    "  - source: ok.j2\n"
                    "    destination: out/ok.py\n"
                    "  - source: ../escape.j2\n"
                    "    destination: out/escape.py"
                ),
            ),
        )
        write_template(tmp_path, "ok.j2", "")

        # The traversal shape is rejected either at load time (schema
        # / model) or, if the model ever loosened, by the planner. The
        # invariant is: no plan is produced. Assert that, not a phase.
        result = BlueprintLoader().load(bp_path)
        assert result.is_err(), (
            "expected the loader to reject a blueprint containing a "
            "traversal source; a future phase-move is fine, silently "
            "accepting it is not"
        )

    def test_mixed_valid_and_missing_template_rejects_whole_plan(self, tmp_path: Path) -> None:
        """One good file + one missing template → no plan at all."""
        bp_path = write_blueprint(
            tmp_path,
            _minimal_blueprint(
                files_block=(
                    "  - source: present.j2\n"
                    "    destination: out/present.py\n"
                    "  - source: missing.j2\n"
                    "    destination: out/missing.py"
                ),
            ),
        )
        write_template(tmp_path, "present.j2", "")
        # Deliberately do NOT write missing.j2.

        result = BlueprintLoader().load(bp_path)
        assert result.is_err()
        # The `present.j2` file did not make it through — the plan
        # cannot be built from a blueprint whose other file is broken.
        assert any(
            "missing.j2" in (e.message or "").lower() for e in result.error
        ), f"expected a missing-template error, got {[e.format() for e in result.error]!r}"
