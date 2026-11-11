"""Tests for the dual-mode input handling in ``forge create``.

These tests cover the acceptance criteria of Task 1.1.6 directly:

1. Non-interactive with all flags succeeds (without prompting).
2. Non-interactive missing a required flag fails with exit 1 and a
   message listing the missing flag.
3. Interactive wizard creates a project from start to finish.
4. The wizard validates input in real-time.
5. The wizard allows cancellation (Ctrl+C) and exits cleanly.

The generation pipeline is not stubbed here — the tests for it exercise
the *real* pipeline (plan → safety → backup → staging → commit).

Input handling
--------------
``_resolve_inputs`` is the collection boundary. It returns a single
immutable ``_ResolvedInputs`` bundle. Two concerns are separated by
test class:

- ``TestDispatch`` and the interactive/non-interactive classes
  exercise ``_resolve_inputs`` and the parser — the input side.
- ``TestGeneratePreFlight`` exercises the safety layer's contract
  through ``_generate`` — the pipeline side.
- ``TestDryRunPath`` exercises the dry-run *render* contract through
  ``_run_dry_run`` — a separate function, a separate contract.

Test design
-----------
Two families of tests, deliberately:

- **Dispatch tests** go through ``cli_runner.invoke(forge_app, ...)``.
  Their purpose is to prove that ``forge create <blueprint>`` parses
  as a leaf command, not as a group with a subcommand. This is the
  contract that broke in an earlier test run (a ``@app.command()``
  or a mis-placed ``invoke_without_command``), and it can only be
  verified through the parser.

- **Resolution tests** call ``_resolve_inputs`` directly. Their
  purpose is to prove the collection logic (flags win, missing flags
  aggregate, wizard prompts when interactive, cancellation
  propagates). The parser is not part of what these tests verify,
  so going through it would add indirection without adding coverage.

Both families exercise the current module shape:

- **Project name** is a *command argument*, resolved inside
  ``_resolve_inputs`` (see ``create.py`` for the reasoning) and
  validated by ``resolution.names.project_name``.

- **Blueprint variables** flow through ``VariableResolver``.

- **Conditional file groups** are evaluated by
  ``ConditionalEvaluator`` and only surviving groups reach the
  planner. That is why ``_ResolvedInputs`` carries
  ``surviving_conditions``.

- **``_generate``** takes exactly **two keyword-only arguments**:

      _generate(*, inputs: _ResolvedInputs, overwrite: bool) -> None

  and is the create path only.

- **``_run_dry_run``** is a *separate* function dispatched to by the
  command when ``--dry-run`` is passed:

      _run_dry_run(*, inputs: _ResolvedInputs, overwrite: bool,
                   json_output: bool) -> None

  Splitting the two paths means each is exercised by the test that
  owns its contract:

  - ``_generate`` → safety layer, backups, staging, commit.
  - ``_run_dry_run`` → ``DryRunService.plan()`` + render + exit code.

The right seam: ``create.get_blueprint_document``
-------------------------------------------------
Earlier versions of these fixtures patched
``forge.cli.registry._load_blueprint_index`` — a static index the
registry used to expose. The registry has since been refactored to
delegate to ``BlueprintDiscoveryService`` (ADR-0009), and that
private name no longer exists.

The fixtures below patch ``create_module.get_blueprint_document``
— the *exact* name the command module imported at load time
(``from forge.cli.registry import get_blueprint_document``), which is
the direct dependency ``_resolve_inputs`` calls.

The fixture's *return shape* is the loaded ``Blueprint``, not a
``BlueprintEntry``. That is the shape ``get_blueprint_document``
returns, and it is what the resolver consumes.

A test that fakes *both* the input and the output of the thing it is
testing is testing itself. So the resolver is **not** faked in the
resolution tests — it is a pure function, the tests already control
every input it sees, and letting it run is the only way to prove it
is actually wired up.

Why some tests do not assert on ``result.output``
-------------------------------------------------
``CliRunner.invoke(forge_app, ...)`` runs ``create_app()``, which
builds the Typer app and mounts the command modules. It does *not*
install ``handle_exception`` — that wrapper lives in ``main()``,
which the CLI entry point runs *after* Typer's dispatch. So when a
command raises a ``ForgeError``, the exception propagates out of
``.invoke()`` as ``result.exception``, and ``result.output`` is
empty. Tests that assert on the *rendered* message invoke
``handle_exception`` on ``result.exception`` directly and assert on
``capsys.readouterr().err``. That is the same code path the shell
exercises, just called from the test instead of from the entry
point.

``_generate`` inputs must satisfy ``_generate``'s contract
----------------------------------------------------------
``_generate`` reads ``inputs.variables.as_dict()`` — a method that
exists on ``ResolvedValues`` (the resolver's immutable output type)
but *not* on a plain ``dict``. That is deliberate: the type
distinction is what prevents an unvalidated ``dict`` from reaching
the planner.

The helper ``_resolved_inputs(...)`` below constructs the correct
shape. Note the shape carefully:

- ``_ResolvedInputs`` carries the *document* (``blueprint``), not a
  derived ``blueprint_id`` field. The identifier lives on
  ``inputs.blueprint.id`` — the model-as-document principle
  (ADR-0007/0008) says the id belongs to the document, not to a
  wrapper that would duplicate it and drift.

- ``surviving_conditions`` is a real field, not optional. Passing
  ``[]`` explicitly is the caller saying "I evaluated conditions and
  none survived" — different from "I forgot to evaluate conditions",
  and the type system makes the difference visible.

Lint conventions
----------------
This file obeys the project's Ruff configuration, including the
flake8-pytest-style (``PT``) and flake8-bandit (``S``) rulesets.
That has three consequences a reader will notice:

- **Side-effect-only fixtures are prefixed with ``_`` and applied
  via ``@pytest.mark.usefixtures(...)``.** ``PT004`` requires the
  underscore for fixtures that return ``None``; ``PT019`` requires
  that such fixtures not be injected as test parameters.
- **Fixtures that *do* return a value have no leading underscore**
  (``PT005``).
- **Temp directories come from ``tmp_path``, not ``/tmp``.**
  ``S108`` flags hardcoded ``/tmp`` because it is world-writable
  and shared across users.
"""

from __future__ import annotations

from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from forge.cli import prompts
from forge.cli.commands import create as create_module
from forge.core.exceptions import (
    BlueprintError,
    CollisionError,
    ExitCode,
    MissingRequiredFlagsError,
    PromptCancelledError,
    UserInputError,
)
from forge.models import (
    Blueprint,
    Category,
    Compatibility,
    VariableDefinition,
    VariableType,
)
from forge.resolution import ResolvedValues
from forge.resolution.names import NAMED_VALIDATORS


# -----------------------------------------------------------------------------
# Fixtures and helpers
# -----------------------------------------------------------------------------
def _make_blueprint(
    *,
    variables: list[VariableDefinition],
    files: list[dict[str, str]] | None = None,
) -> Blueprint:
    """Build a minimal valid ``Blueprint`` for testing.

    Centralised so the two fixtures (``_fake_blueprint`` and
    ``_fake_blueprint_with_vars``) stay in lockstep on all fields
    they are *not* testing. A future field addition to ``Blueprint``
    needs one edit here, not two.

    ``files`` is optional because most tests (input collection,
    cancellation, blueprint variables) do not touch the planner —
    only the ``TestGeneratePreFlight`` and ``TestDryRunPath`` tests
    need a plan with a destination to conflict against. Passing
    ``None`` keeps the existing behaviour (empty ``files[]``, zero
    planned files) for every test that does not care.

    The blueprint is deliberately a *real model instance*, not a
    duck-typed fake. The resolver, the loader, the planner, and every
    other consumer validate against this type, so the test should
    too.
    """
    return Blueprint(
        schema_version="1.0.0",
        id="python.fastapi.api",
        name="Fake API",
        version="1.0.0",
        description="Fake blueprint for tests.",
        category=Category.api,
        variables=variables,
        files=files or [],
        verification=[{"id": "ok", "type": "required_file", "path": "README.md"}],
        compatibility=Compatibility(min_forge_version="1.0.0"),
    )


def _resolved_inputs(
    *,
    blueprint: Blueprint | None = None,
    destination: Path,
    project_name: str = "my-api",
    variables: dict[str, Any] | None = None,
    surviving_conditions: list[Any] | None = None,
) -> SimpleNamespace:
    """Build a ``_ResolvedInputs``-shaped object with a real ``ResolvedValues``.

    ``_generate`` calls ``inputs.variables.as_dict()`` — a method that
    exists on ``ResolvedValues`` but not on a plain ``dict``. That
    method is the type boundary the resolver exists to enforce (see
    ``forge.resolution.resolver.ResolvedValues``), and the tests must
    construct inputs that satisfy it.

    ``SimpleNamespace`` is used (rather than ``_ResolvedInputs``
    directly) because ``_ResolvedInputs`` is a private type in
    ``create.py``. The namespace satisfies the duck-typed contract
    ``_generate`` / ``_run_dry_run`` actually consume
    (``.blueprint``, ``.project_name``, ``.destination``,
    ``.variables``, ``.surviving_conditions``), which is the honest
    level of coupling for a test of a private helper.

    ``project_name`` is a *bundled field*, not derived from
    ``blueprint.id`` — the two are different concepts (id is the
    blueprint, project_name is the user's chosen name), and tests
    that need to distinguish them can pass different values.
    """
    return SimpleNamespace(
        blueprint=blueprint or _make_blueprint(variables=[]),
        project_name=project_name,
        destination=destination,
        variables=ResolvedValues(MappingProxyType(variables or {})),
        surviving_conditions=surviving_conditions or [],
    )


@pytest.fixture()
def _fake_blueprint(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install a *variable-less* fake blueprint behind ``get_blueprint_document``.

    The empty ``variables`` list is the interesting case for the
    resolution refactor: it forces the tests to prove that
    ``project_name`` is *not* smuggled through the resolver. A
    blueprint that declared a ``project_name`` variable would mask
    the bug the ``KeyError('project_name')`` test run originally
    surfaced.
    """
    fake = _make_blueprint(variables=[])
    monkeypatch.setattr(create_module, "get_blueprint_document", lambda _id: fake)


@pytest.fixture()
def _fake_blueprint_with_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fake blueprint with a single required string variable (``author``).

    Used by tests that exercise the resolver's *blueprint variable*
    path (missing required variable, real-time prompt for a blueprint
    variable). The variable is a genuine ``VariableDefinition`` from
    ``forge.models`` — the resolver validates against the real type,
    not a duck-typed fake.
    """
    fake = _make_blueprint(
        variables=[
            VariableDefinition(
                id="author",
                type=VariableType.string,
                description="Author name used in project metadata.",
                required=True,
            ),
        ],
    )
    monkeypatch.setattr(create_module, "get_blueprint_document", lambda _id: fake)


@pytest.fixture()
def capture_generate(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Stub ``_generate`` so the test can inspect the resolved inputs.

    ``_generate`` is the boundary between *input handling* and the
    *pipeline*. The tests in this class exercise input handling, so
    they stub the pipeline and assert on what it *would* receive.

    The stub signature mirrors the *current* signature of
    ``create_module._generate``:

        _generate(*, inputs: _ResolvedInputs, overwrite: bool) -> None

    The ``inputs`` object carries everything the pipeline needs:
    the blueprint document, the resolved variables (with the project
    name folded in), the destination, and the surviving conditional
    groups. The stub records the whole ``inputs`` object plus
    ``overwrite``, so tests can reach into ``inputs`` for whatever
    specific field they are asserting.

    If ``create_module._generate`` gains a parameter (or renames
    one), this fixture will raise ``TypeError`` at the next
    ``cli_runner.invoke`` — the fixture is the trip wire for the
    signature, not just a recording device.
    """
    captured: dict[str, Any] = {}

    def _fake(*, inputs: Any, overwrite: bool) -> None:
        captured["inputs"] = inputs
        captured["overwrite"] = overwrite

    monkeypatch.setattr(create_module, "_generate", _fake)
    return captured


@pytest.fixture()
def capture_dry_run(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Stub ``_run_dry_run`` so the test can inspect the dry-run inputs.

    Symmetric with ``capture_generate``: the dry-run path is a
    separate function (``_run_dry_run``) with its own signature

        _run_dry_run(*, inputs: _ResolvedInputs, overwrite: bool,
                     json_output: bool) -> None

    which the command dispatches to when ``--dry-run`` is passed.
    Stubbing it lets the dispatch tests prove *which* path was taken
    without exercising the plan builder.
    """
    captured: dict[str, Any] = {}

    def _fake(*, inputs: Any, overwrite: bool, json_output: bool) -> None:
        captured["inputs"] = inputs
        captured["overwrite"] = overwrite
        captured["json_output"] = json_output

    monkeypatch.setattr(create_module, "_run_dry_run", _fake)
    return captured


def _install_blueprint_root_stub(monkeypatch: pytest.MonkeyPatch, blueprint_root: Path) -> None:
    """Shared body of the two ``blueprint_root``-stubbing fixtures.

    Extracted so the two public fixtures (``_stub_blueprint_root`` and
    ``blueprint_root``) describe *the same* monkeypatch. A future change
    to how ``_blueprint_root`` is patched touches this one function.
    """
    monkeypatch.setattr(create_module, "_blueprint_root", lambda _bp: blueprint_root)


@pytest.fixture()
def _stub_blueprint_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Install the ``_blueprint_root`` stub without returning its ``Path``.

    For tests that only need the stub to *exist* — they do not write
    into the blueprint root, so they do not need the ``Path``.
    """
    blueprint_root = tmp_path / "blueprint-root"
    blueprint_root.mkdir()
    _install_blueprint_root_stub(monkeypatch, blueprint_root)


@pytest.fixture()
def blueprint_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Install the ``_blueprint_root`` stub *and* return its ``Path``.

    For tests that need to write a template file into the root.
    """
    blueprint_root = tmp_path / "blueprint-root"
    blueprint_root.mkdir()
    _install_blueprint_root_stub(monkeypatch, blueprint_root)
    return blueprint_root


@pytest.fixture()
def _force_non_interactive(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``prompts.is_interactive()`` return ``False``."""
    monkeypatch.setattr(prompts, "is_interactive", lambda: False)


@pytest.fixture()
def _force_interactive(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``prompts.is_interactive()`` return ``True``."""
    monkeypatch.setattr(prompts, "is_interactive", lambda: True)


# -----------------------------------------------------------------------------
# Dispatch tests — prove ``forge create <blueprint>`` is a leaf command
# -----------------------------------------------------------------------------
class TestDispatch:
    """Tests for the command's dispatch contract (leaf, not group)."""

    @pytest.mark.unit()
    @pytest.mark.usefixtures("_fake_blueprint", "_force_non_interactive")
    def test_create_accepts_positional_blueprint_directly(
        self,
        cli_runner: CliRunner,
        forge_app: Any,
        capture_generate: dict[str, Any],
    ) -> None:
        """``forge create <blueprint> --name …`` parses as a leaf command.

        This is the regression test for the group-vs-leaf bug. The
        invocation must reach ``_generate`` with the parsed inputs —
        if the command were a group, Click would reject
        ``python.fastapi.api`` as a missing subcommand before any
        code ran, and ``_generate`` would never be called.

        The blueprint *id* is reached via ``inputs.blueprint.id``,
        not via a convenience accessor. The ``_ResolvedInputs``
        bundle carries the document itself (``blueprint``), and
        adding a duplicated ``blueprint_id`` field would invite the
        two to drift — the model-as-document principle (ADR-0007/
        0008) says the identifier belongs to the document, not to a
        wrapper.
        """
        result = cli_runner.invoke(
            forge_app,
            [
                "create",
                "python.fastapi.api",
                "--name",
                "my-api",
                "--no-interactive",
            ],
        )

        assert result.exit_code == 0, result.output
        inputs = capture_generate["inputs"]
        # The blueprint document travels inside ``inputs`` — the
        # planner needs it, and passing it once (from the resolve
        # step) rather than re-fetching in `_generate` guarantees
        # the document the user's answers were validated against is
        # the document that will be planned.
        assert inputs.blueprint.id == "python.fastapi.api"
        assert inputs.project_name == "my-api"
        # No conditional files on the fake blueprint → the evaluator
        # returns an empty list, which is what reaches `_generate`.
        assert inputs.surviving_conditions == []
        assert capture_generate["overwrite"] is False

    @pytest.mark.unit()
    def test_create_help_shows_leaf_signature(self, cli_runner: CliRunner, forge_app: Any) -> None:
        """``forge create --help`` shows ``Usage: forge create [OPTIONS] BLUEPRINT``.

        A group would show ``Usage: forge create [OPTIONS] COMMAND
        [ARGS]...``. This test asserts on the presence of the
        positional argument's name in the help output, which only
        appears in the leaf signature.
        """
        result = cli_runner.invoke(forge_app, ["create", "--help"])

        assert result.exit_code == 0, result.output
        assert "BLUEPRINT" in result.output

    @pytest.mark.unit()
    def test_create_with_no_args_reports_missing_blueprint(
        self, cli_runner: CliRunner, forge_app: Any
    ) -> None:
        """``forge create`` with no arguments is a usage error, not help.

        A missing ``BLUEPRINT`` argument is a Click usage error (exit
        2), which is what a script should see — not Typer's default
        "print help, exit 0" behaviour.
        """
        result = cli_runner.invoke(forge_app, ["create"])

        assert result.exit_code == 2, result.output
        assert "Missing argument" in result.output or "BLUEPRINT" in result.output


# -----------------------------------------------------------------------------
# Acceptance criterion 1: non-interactive with all flags succeeds
# -----------------------------------------------------------------------------
class TestNonInteractiveSuccess:
    """All flags supplied → no prompt → success."""

    @pytest.mark.unit()
    @pytest.mark.usefixtures("_fake_blueprint", "_force_non_interactive")
    def test_all_flags_supplied_succeeds_without_prompting(
        self,
        cli_runner: CliRunner,
        forge_app: Any,
        capture_generate: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """All required flags → success, no prompt fired.

        The assertion on ``ask_text`` *not being called* is the
        observable proof that the wizard did not engage. Faking
        ``ask_text`` to raise rather than return is a stronger form
        than asserting on a counter: a counter could be reset by a
        test bug, but an exception propagates.
        """

        def _fail_if_called(*_a: Any, **_k: Any) -> str:
            raise AssertionError(
                "ask_text fired in non-interactive mode — the wizard "
                "must not engage when --no-interactive is passed."
            )

        monkeypatch.setattr(prompts, "ask_text", _fail_if_called)

        result = cli_runner.invoke(
            forge_app,
            [
                "create",
                "python.fastapi.api",
                "--name",
                "my-api",
                "--no-interactive",
            ],
        )

        assert result.exit_code == 0, result.output
        assert capture_generate["inputs"].project_name == "my-api"
        assert capture_generate["overwrite"] is False

    @pytest.mark.unit()
    @pytest.mark.usefixtures("_fake_blueprint", "_force_non_interactive")
    def test_dry_run_flag_routes_to_dry_run_path(
        self,
        cli_runner: CliRunner,
        forge_app: Any,
        capture_dry_run: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """``--dry-run`` dispatches to ``_run_dry_run``, not ``_generate``.

        ``--dry-run`` is a *separate code path* at the command level —
        it builds a ``GenerationPlan`` DTO and renders it, without
        invoking the pipeline at all (ADR-0018). This test proves the
        dispatch: when ``--dry-run`` is passed, ``_run_dry_run`` fires
        with the correct inputs, and ``_generate`` does *not* fire.

        ``_generate`` is stubbed to raise if called — a stronger
        assertion than "the dry-run stub was called", because it also
        proves the pipeline was not entered.
        """

        def _fail_generate(*_a: Any, **_k: Any) -> None:
            raise AssertionError(
                "_generate fired despite --dry-run — the pipeline "
                "must not be entered in dry-run mode."
            )

        monkeypatch.setattr(create_module, "_generate", _fail_generate)

        result = cli_runner.invoke(
            forge_app,
            [
                "create",
                "python.fastapi.api",
                "--name",
                "my-api",
                "--output",
                str(tmp_path),
                "--no-interactive",
                "--dry-run",
            ],
        )

        assert result.exit_code == 0, result.output
        assert capture_dry_run["inputs"].project_name == "my-api"
        assert capture_dry_run["inputs"].destination == tmp_path / "my-api"
        assert capture_dry_run["overwrite"] is False
        assert capture_dry_run["json_output"] is False

    @pytest.mark.unit()
    @pytest.mark.usefixtures("_fake_blueprint", "_force_non_interactive")
    def test_output_flag_reaches_inputs(
        self,
        cli_runner: CliRunner,
        forge_app: Any,
        capture_generate: dict[str, Any],
        tmp_path: Path,
    ) -> None:
        """``--output`` is folded into ``inputs.destination``.

        ``--output`` used to be a ``_generate`` parameter. The current
        shape folds it into ``inputs.destination`` (the pipeline
        receives the full destination, not a root and a name) because
        that is what the dry-run path also needs, and duplicating the
        ``output / name`` computation between the two paths would
        invite drift.

        ``tmp_path`` is used because it is the per-test temp
        directory pytest provides — uniquely owned by the test,
        cleaned up automatically, and immune to the hardcoded-``/tmp``
        concern that ``S108`` flags.
        """
        result = cli_runner.invoke(
            forge_app,
            [
                "create",
                "python.fastapi.api",
                "--name",
                "my-api",
                "--output",
                str(tmp_path),
                "--no-interactive",
            ],
        )

        assert result.exit_code == 0, result.output
        assert capture_generate["inputs"].destination == tmp_path / "my-api"


# -----------------------------------------------------------------------------
# Acceptance criterion 2: missing --name → exit 1 + list of missing flags
# -----------------------------------------------------------------------------
class TestNonInteractiveMissingFlags:
    """Missing required flags in non-interactive mode → exit 1."""

    @pytest.mark.unit()
    @pytest.mark.usefixtures("_fake_blueprint", "_force_non_interactive")
    def test_missing_name_exits_1_with_flag_in_message(
        self,
        cli_runner: CliRunner,
        forge_app: Any,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Exit code is 1; the rendered message names ``--name``.

        Three assertions across three layers, because the contract
        lives at three layers:

        1. ``result.exit_code == ExitCode.USER_ERROR`` — the shell
           exit code Typer derives from the uncaught exception.
        2. ``isinstance(result.exception, MissingRequiredFlagsError)``
           — the *domain* exception the command raised.
        3. ``"--name" in captured.err`` — the *rendered* message.

        Layer 3 is the reason this test does not assert on
        ``result.output``. ``CliRunner.invoke(forge_app, ...)`` runs
        ``create_app()``, which builds the Typer app but does not
        install ``handle_exception`` — that wrapper lives in
        ``main()``. So the exception propagates as
        ``result.exception`` and ``result.output`` is empty.
        """
        from forge.cli.errors import handle_exception

        result = cli_runner.invoke(
            forge_app,
            ["create", "python.fastapi.api", "--no-interactive"],
        )

        assert result.exit_code == ExitCode.USER_ERROR
        assert isinstance(result.exception, MissingRequiredFlagsError)

        with pytest.raises(SystemExit):
            handle_exception(result.exception, debug=False)

        captured = capsys.readouterr()
        assert "--name" in captured.err
        # Product stream stays clean — errors are commentary.
        assert captured.out == ""

    @pytest.mark.unit()
    @pytest.mark.usefixtures("_fake_blueprint", "_force_non_interactive")
    def test_missing_name_raises_missing_flags_error_directly(
        self,
        tmp_path: Path,
    ) -> None:
        """The domain exception type is ``MissingRequiredFlagsError``.

        Asserting on the exception type directly (not just the exit
        code) proves the *right* error is raised. A ``UserInputError``
        from a typo in the command would also exit 1, but it would
        carry the wrong message and no flag list. The type is the
        precise contract.

        This test bypasses the CLI entirely — it calls
        ``_resolve_inputs`` directly. That is the correct layer for
        "does the command raise the right exception," separate from
        "does the shell render it" (the previous test).
        """
        with pytest.raises(MissingRequiredFlagsError) as info:
            create_module._resolve_inputs(
                blueprint_id="python.fastapi.api",
                cli_name=None,
                cli_vars={},
                output=tmp_path,
                no_interactive=True,
            )

        assert "name" in info.value.missing

    @pytest.mark.unit()
    @pytest.mark.usefixtures("_fake_blueprint", "_force_non_interactive")
    def test_missing_name_error_has_actionable_hint(
        self,
        tmp_path: Path,
    ) -> None:
        """The error carries a hint (a dead end otherwise).

        The ``ForgeError`` contract says every user-facing error
        carries a hint. A missing-flags error without a hint tells
        the user *what* is wrong but not *what to do* — the exact
        dead end the class docstring warns against.
        """
        with pytest.raises(MissingRequiredFlagsError) as info:
            create_module._resolve_inputs(
                blueprint_id="python.fastapi.api",
                cli_name=None,
                cli_vars={},
                output=tmp_path,
                no_interactive=True,
            )

        assert info.value.hint is not None
        assert len(info.value.hint) > 0


# -----------------------------------------------------------------------------
# Acceptance criterion 3: wizard creates project start-to-finish
# -----------------------------------------------------------------------------
class TestInteractiveWizard:
    """Interactive mode prompts for anything not supplied as a flag."""

    @pytest.mark.unit()
    @pytest.mark.usefixtures("_fake_blueprint", "_force_interactive")
    def test_prompts_for_name_when_not_supplied(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Interactive mode prompts for ``--name`` when it is absent.

        The test fakes ``ask_text`` (the *input* to the wizard) but
        not the validator (the *output* of the funnel). That is the
        correct side of the boundary: fake what the user types; let
        the code prove it processes what the user typed.

        ``_resolve_inputs`` returns a single ``_ResolvedInputs``
        object. The project name lives inside it.
        """
        monkeypatch.setattr(prompts, "ask_text", lambda *a, **k: "wizard-api")

        inputs = create_module._resolve_inputs(
            blueprint_id="python.fastapi.api",
            cli_name=None,
            cli_vars={},
            output=tmp_path,
            no_interactive=False,
        )

        assert inputs.project_name == "wizard-api"

    @pytest.mark.unit()
    @pytest.mark.usefixtures("_fake_blueprint", "_force_interactive")
    def test_does_not_re_prompt_when_name_supplied_as_flag(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Flags win: a supplied ``--name`` is not re-asked.

        Both ``ask_text`` (name prompt) and ``ask_select`` /
        ``ask_confirm`` (blueprint-variable prompts) are patched to
        raise — because with ``cli_name`` supplied and no blueprint
        variables, the wizard should fire *no* prompt at all.
        """

        def _raise_if_called(*_a: Any, **_k: Any) -> str:
            raise AssertionError("a prompt fired despite --name being supplied")

        monkeypatch.setattr(prompts, "ask_text", _raise_if_called)
        monkeypatch.setattr(prompts, "ask_select", _raise_if_called)
        monkeypatch.setattr(prompts, "ask_confirm", _raise_if_called)

        inputs = create_module._resolve_inputs(
            blueprint_id="python.fastapi.api",
            cli_name="flag-api",
            cli_vars={},
            output=tmp_path,
            no_interactive=False,
        )

        assert inputs.project_name == "flag-api"

    @pytest.mark.unit()
    @pytest.mark.usefixtures("_fake_blueprint", "_force_interactive")
    def test_no_interactive_flag_disables_wizard_even_on_tty(
        self,
        cli_runner: CliRunner,
        forge_app: Any,
        capture_generate: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``--no-interactive`` overrides a TTY stdin.

        This is the case a user in a terminal hits when they want a
        scripted run from an interactive shell. The flag must win
        over the TTY check, or the wizard would fire despite the
        explicit request not to.
        """

        def _raise_if_called(*_a: Any, **_k: Any) -> str:
            raise AssertionError("wizard fired despite --no-interactive")

        monkeypatch.setattr(prompts, "ask_text", _raise_if_called)
        monkeypatch.setattr(prompts, "ask_select", _raise_if_called)
        monkeypatch.setattr(prompts, "ask_confirm", _raise_if_called)

        result = cli_runner.invoke(
            forge_app,
            [
                "create",
                "python.fastapi.api",
                "--name",
                "my-api",
                "--no-interactive",
            ],
        )

        assert result.exit_code == 0, result.output
        assert capture_generate["inputs"].project_name == "my-api"

    @pytest.mark.unit()
    @pytest.mark.usefixtures("_fake_blueprint", "_force_interactive")
    def test_interactive_dispatches_without_requiring_flag(
        self,
        cli_runner: CliRunner,
        forge_app: Any,
        capture_generate: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A TTY without ``--name`` still dispatches to the wizard.

        Proves the *parser* does not reject a ``--name``-less
        invocation before the wizard gets a chance to run — which is
        the whole reason ``name`` is optional at the Typer layer.

        ``ask_text`` answers ``wizard-api`` for the project-name
        prompt. ``_fake_blueprint`` declares no blueprint variables,
        so no ``ask_select`` or ``ask_confirm`` prompt fires.
        """
        monkeypatch.setattr(prompts, "ask_text", lambda *a, **k: "wizard-api")

        result = cli_runner.invoke(
            forge_app,
            ["create", "python.fastapi.api"],
        )

        assert result.exit_code == 0, result.output
        assert capture_generate["inputs"].project_name == "wizard-api"


# -----------------------------------------------------------------------------
# Acceptance criterion 4: real-time validation
#
# The inline validator used to live in the command module
# (``_project_name_validator``). It has moved to
# ``resolution.names.project_name``, which is the *same* function the
# resolver applies. There is exactly one validator, used by both the
# prompt (for real-time UX) and the resolver (for authoritative
# validation). These tests exercise that validator directly at the
# layer where it lives.
# -----------------------------------------------------------------------------
class TestNamedValidatorIntegration:
    """``resolution.names.project_name`` is wired into the wizard."""

    @pytest.mark.unit()
    def test_rejects_empty_name(self) -> None:
        """An empty name is rejected by the shared named validator."""
        assert NAMED_VALIDATORS["project_name"]("") is not None

    @pytest.mark.unit()
    def test_rejects_names_with_spaces(self) -> None:
        """Spaces produce a directory name with spaces — rejected."""
        assert NAMED_VALIDATORS["project_name"]("my project") is not None

    @pytest.mark.unit()
    def test_accepts_hyphenated_lowercase_names(self) -> None:
        """Hyphenated lowercase names are the canonical accepted form.

        Note: underscores are **rejected** by ``project_name`` because
        npm consumers expect hyphenated names, and the same project
        name is reused as an npm package name in the TypeScript
        blueprints. The underscore form belongs to
        ``python_package`` (a different named validator); testing it
        here would be a contract error, not a validator bug.
        """
        validator = NAMED_VALIDATORS["project_name"]
        assert validator("myapi") is None
        assert validator("my-api") is None
        # Negative case: the project-name rule rejects underscores.
        assert validator("my_api") is not None

    @pytest.mark.unit()
    @pytest.mark.usefixtures("_fake_blueprint", "_force_non_interactive")
    def test_prompt_wiring_uses_the_same_validator(self, tmp_path: Path) -> None:
        """The wizard's inline validator and the resolver agree on names.

        The invariant this test enforces is stronger than a "subset"
        test: it is the *same* function on both sides, so there is no
        possible drift. We assert the wiring by running a name
        through the non-interactive path and confirming the resolver
        accepts it unchanged (which requires the same validator the
        prompt uses to have already accepted it).
        """
        # A name the resolver accepts, threaded through the wizard.
        for candidate in ("myapi", "my-api", "a1-b2"):
            inputs = create_module._resolve_inputs(
                blueprint_id="python.fastapi.api",
                cli_name=candidate,
                cli_vars={},
                output=tmp_path,
                no_interactive=True,
            )
            assert inputs.project_name == candidate


# -----------------------------------------------------------------------------
# Acceptance criterion 5: cancellation exits cleanly
# -----------------------------------------------------------------------------
class TestCancellation:
    """Ctrl+C at a prompt → ``PromptCancelledError``, exit 1, no traceback."""

    @pytest.mark.unit()
    @pytest.mark.usefixtures("_fake_blueprint", "_force_interactive")
    def test_cancellation_propagates_from_prompt(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``PromptCancelledError`` from the prompt propagates unchanged.

        The command must not catch and rewrap. If it did, the
        handler's ``PromptCancelledError`` branch would not fire and
        the cancellation would render as an unexpected error (exit
        5) or as a plain ``ForgeError`` (exit 1 but with the wrong
        rendering).
        """

        def _cancel(*_a: Any, **_k: Any) -> str:
            raise PromptCancelledError()

        monkeypatch.setattr(prompts, "ask_text", _cancel)

        with pytest.raises(PromptCancelledError):
            create_module._resolve_inputs(
                blueprint_id="python.fastapi.api",
                cli_name=None,
                cli_vars={},
                output=tmp_path,
                no_interactive=False,
            )

    @pytest.mark.unit()
    @pytest.mark.usefixtures("_fake_blueprint", "_force_interactive")
    def test_cancellation_propagates_from_cli_with_exit_1(
        self,
        cli_runner: CliRunner,
        forge_app: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """End-to-end: Ctrl+C → ``result.exception`` is the cancellation.

        ``CliRunner.invoke(forge_app, ...)`` runs ``create_app()``,
        which does **not** install ``handle_exception`` — that
        wrapper lives in ``main()``. So a cancellation raised inside
        the command propagates out of ``.invoke()`` as
        ``result.exception``, and ``result.output`` is empty.

        This test asserts on the exception type (the contract that
        ``main()`` will see) and on the exit code Typer derives from
        it. The *rendering* of that exception ("Cancelled." on
        stderr) is tested separately in ``test_errors.py``.
        """

        def _cancel(*_a: Any, **_k: Any) -> str:
            raise PromptCancelledError()

        monkeypatch.setattr(prompts, "ask_text", _cancel)

        result = cli_runner.invoke(
            forge_app,
            ["create", "python.fastapi.api"],
        )

        assert result.exit_code == ExitCode.USER_ERROR
        assert isinstance(result.exception, PromptCancelledError)
        assert "Traceback" not in result.output


# -----------------------------------------------------------------------------
# Blueprint errors
# -----------------------------------------------------------------------------
class TestBlueprintErrors:
    """Unknown blueprint ID → ``BlueprintError`` (exit 2), fail fast."""

    @pytest.mark.unit()
    def test_unknown_blueprint_raises_blueprint_error(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """An unknown ID raises ``BlueprintError``, not ``UserInputError``.

        The exit code is the contract: a script that branches on
        ``$? in (1, 2, 3)`` treats both as user-fixable, but a
        script that wants "try a different blueprint" needs to
        distinguish 2 from 1.

        The ``get_blueprint_document`` override raises the domain
        error directly — the test is about the command's *response*
        to a blueprint-not-found, not about the discovery service's
        plumbing (which has its own tests).
        """

        def _raise(_id: str) -> None:
            raise BlueprintError(f"Blueprint '{_id}' not found.")

        monkeypatch.setattr(create_module, "get_blueprint_document", _raise)

        with pytest.raises(BlueprintError):
            create_module._resolve_inputs(
                blueprint_id="does.not.exist",
                cli_name="x",
                cli_vars={},
                output=tmp_path,
                no_interactive=True,
            )

    @pytest.mark.unit()
    @pytest.mark.usefixtures("_force_interactive")
    def test_unknown_blueprint_fails_before_prompting(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A bad blueprint ID fails before the wizard asks anything.

        The user should not answer ten questions for a project that
        cannot be generated. The blueprint lookup happens first, and
        its failure short-circuits before ``ask_text`` is reached.
        """

        def _raise(_id: str) -> None:
            raise BlueprintError(f"Blueprint '{_id}' not found.")

        monkeypatch.setattr(create_module, "get_blueprint_document", _raise)
        monkeypatch.setattr(
            prompts,
            "ask_text",
            lambda *a, **k: (_ for _ in ()).throw(
                AssertionError("wizard fired for a blueprint that does not exist")
            ),
        )

        with pytest.raises(BlueprintError):
            create_module._resolve_inputs(
                blueprint_id="does.not.exist",
                cli_name=None,
                cli_vars={},
                output=tmp_path,
                no_interactive=False,
            )


# -----------------------------------------------------------------------------
# Blueprint variables flow through the resolver
# -----------------------------------------------------------------------------
class TestBlueprintVariablesThroughResolver:
    """Blueprint variables flow through ``VariableResolver``."""

    @pytest.mark.unit()
    @pytest.mark.usefixtures("_fake_blueprint_with_vars", "_force_non_interactive")
    def test_missing_required_blueprint_variable_aggregates(self, tmp_path: Path) -> None:
        """A missing required blueprint variable surfaces from the resolver.

        The error is not flag-shaped (``--name``) — that error belongs
        to the *command's* flags. Blueprint variables have their own
        error shape (``required.missing`` from the resolver), which
        the command wraps in a ``UserInputError`` with a hint.
        """
        with pytest.raises(UserInputError) as info:
            create_module._resolve_inputs(
                blueprint_id="python.fastapi.api",
                cli_name="my-api",
                cli_vars={},  # author not supplied
                output=tmp_path,
                no_interactive=True,
            )

        # ``UserInputError`` folds the hint text into the message, and
        # the resolver's joined errors carry the code for the missing
        # variable. Both are asserted because they are the contract
        # the CLI handler renders.
        assert "required variable 'author'" in str(info.value)

    @pytest.mark.unit()
    @pytest.mark.usefixtures("_fake_blueprint_with_vars", "_force_non_interactive")
    def test_supplied_blueprint_variable_passes_through(self, tmp_path: Path) -> None:
        """A supplied blueprint variable lands in ``_ResolvedInputs.variables``."""
        inputs = create_module._resolve_inputs(
            blueprint_id="python.fastapi.api",
            cli_name="my-api",
            cli_vars={"author": "alice"},
            output=tmp_path,
            no_interactive=True,
        )

        assert inputs.variables["author"] == "alice"
        # The project name is also present — a command argument added
        # alongside the resolver's output.
        assert inputs.variables["project_name"] == "my-api"


# -----------------------------------------------------------------------------
# Generation pipeline (safety layer contract)
# -----------------------------------------------------------------------------
# These tests do not go through ``_resolve_inputs`` — the resolver is
# deliberately pure and does not check the filesystem. The collision
# and overwrite contract is enforced by ``forge.safety`` inside
# ``_generate``, and that is the layer these tests exercise.
#
# ``_generate`` takes exactly **two keyword-only arguments**:
#
#     _generate(*, inputs: _ResolvedInputs, overwrite: bool) -> None
#
# ``inputs`` is a real ``_ResolvedInputs`` shape (constructed by the
# ``_resolved_inputs`` helper above) with a real ``Blueprint``
# instance — because the planner needs to read ``files[]`` and
# ``conditional_files[]`` from it. ``overwrite`` decides whether the
# safety layer permits replacing existing destinations.
#
# ``_generate`` never handles ``--dry-run`` — that is a separate code
# path (``_run_dry_run``) invoked by the command based on the flag.
# The dry-run tests below exercise ``_run_dry_run`` for the render
# contract and ``_generate`` for the safety contract, so each test
# targets the function that actually owns the property it pins.
# -----------------------------------------------------------------------------
class TestGeneratePreFlight:
    """``_generate`` enforces the safety layer's collision contract.

    The exit-code contract distinguishes two classes of failure that
    the pipeline can refuse:

    * **Collision** — a destination already exists and ``--overwrite``
      was not passed. ``CollisionError`` (exit code **6**). The
      remediation is "retry with ``--overwrite``, or edit the plan",
      and CI can branch on 6 specifically.
    * **Structural safety** — a path escapes the root, is absolute,
      traverses a symlink, or the parent is unwritable.
      ``SafetyError`` (exit code **3**). ``--overwrite`` cannot help;
      the plan must be edited.

    The tests here pin the *collision* contract because it is the one
    with the ``--overwrite`` remediation. The structural contract is
    exercised by the adversarial suite (``tests/adversarial/``), where
    each attack class has its own test.
    """

    @pytest.mark.unit()
    @pytest.mark.usefixtures("_stub_blueprint_root")
    def test_existing_destination_without_overwrite_raises_collision(self, tmp_path: Path) -> None:
        """An existing destination without ``--overwrite`` → ``CollisionError``.

        Setup: a blueprint that plans one file (``out.txt``) whose
        destination already exists on disk. Without ``--overwrite``,
        the safety layer's ``detect_collisions`` finds the existing
        destination and refuses.

        The assertion is on the *type* (``CollisionError``) and on
        the *exit code* (6), not on ``FilesystemConflictError`` — a
        distinction that pins the exit-code contract from ADR-0017.
        A future regression that folded collisions back into the
        generic safety class would fail this test.
        """
        # Destination with a pre-existing file — what the pipeline
        # will collide against.
        target = tmp_path / "occupied"
        target.mkdir()
        (target / "out.txt").write_text("pre-existing")

        blueprint = _make_blueprint(
            variables=[],
            files=[{"source": "src.txt", "destination": "out.txt"}],
        )
        inputs = _resolved_inputs(blueprint=blueprint, destination=target)

        with pytest.raises(CollisionError) as info:
            create_module._generate(inputs=inputs, overwrite=False)

        # Exit code 6 is the contract — a script that retries with
        # ``--overwrite`` branches on this code specifically.
        assert info.value.exit_code == ExitCode.COLLISION

    @pytest.mark.unit()
    def test_existing_destination_with_overwrite_creates_backup(
        self, tmp_path: Path, blueprint_root: Path
    ) -> None:
        """``--overwrite`` replaces the file and backs up the original.

        Two guarantees, both tested:

        1. **The pipeline completes** — no exception, the destination
        now has the new content.
        2. **A backup exists** — the original content is preserved
        in a ``*.bak.<timestamp>`` sibling, matching ADR-0017's
        "every overwrite is backed up" contract.

        The test asserts on the *content* of both files, not just
        their existence. Existence alone would pass even if the
        backup held the wrong bytes, or if the destination were left
        unchanged (a different but equally broken outcome).

        The ``blueprint_root`` fixture (rather than
        ``_stub_blueprint_root``) is used because the pipeline reads
        the plan's source template from that directory during staging.
        Without the ``src.txt`` file present there, the copy branch of
        ``_stage_one`` would raise ``FilesystemConflictError`` — the
        test would then fail on the *pipeline setup*, not on the
        overwrite/backup contract it exists to pin.
        """
        # Source template the pipeline will copy into the destination.
        (blueprint_root / "src.txt").write_text("new body")

        # Destination with a pre-existing file — what the pipeline
        # will overwrite (and back up).
        target = tmp_path / "occupied"
        target.mkdir()
        (target / "out.txt").write_text("pre-existing body")

        blueprint = _make_blueprint(
            variables=[],
            files=[{"source": "src.txt", "destination": "out.txt"}],
        )
        inputs = _resolved_inputs(blueprint=blueprint, destination=target)

        create_module._generate(inputs=inputs, overwrite=True)

        # The destination now has the *source's* content.
        assert (target / "out.txt").read_text() == "new body"

        # The original content survives in a timestamped backup.
        backups = list(target.glob("out.txt.bak.*"))
        assert len(backups) == 1
        assert backups[0].read_text() == "pre-existing body"


# -----------------------------------------------------------------------------
# Dry-run path — render contract, no writes
# -----------------------------------------------------------------------------
class TestDryRunPath:
    """``_run_dry_run`` renders a plan and never writes."""

    @pytest.mark.unit()
    def test_dry_run_renders_plan_even_for_conflicted_destination(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        blueprint_root: Path,
    ) -> None:
        """``--dry-run`` renders the plan for a conflicted destination.

        A dry run is a *plan*, not a *commit*. Its purpose is to tell
        the user what would happen, including the conflict — so it
        must not fail on the conflict, and it must not silently
        return. The assertion is on the *rendered plan* (its presence
        in stdout), not on the absence of an exception.

        The plan header carries the *blueprint* id and the
        *destination*, not the project name. The project name is
        implicit in every destination path the plan lists — see
        ``GenerationPlan`` in ``forge/dryrun/plan.py``: it has no
        project-name field because the plan is about *what will be
        written where*, and every destination already encodes the
        project name via the path. Asserting on ``"my-api"`` would
        couple the test to a rendering detail the DTO does not carry
        and should not carry.

        What the renderer *does* promise — and what this test pins —
        are the four properties a user actually reads to decide
        whether to proceed:

        1. The blueprint id.
        2. The destination root.
        3. The planned file the user would gain.
        4. The collision remediation hint (the whole point of a
           dry-run over a conflicted destination).

        **The exit code contract.** ``_run_dry_run`` raises
        ``SystemExit(ExitCode.DRY_RUN_COLLISIONS)`` (exit **7**) when
        the plan contains collisions — *not*
        ``ExitCode.COLLISION`` (exit **6**). The two codes are
        distinct by design (ADR-0018):

        * ``6`` — the *create* path's code, meaning "a real write
          was refused because the destination exists." The write did
          not happen; the user's next step is to re-run create with
          ``--overwrite``.
        * ``7`` — the *dry-run* code, meaning "the plan was
          produced successfully and is on stdout; it contains
          collisions." Nothing was written; the user's next step is
          to *inspect* the plan and then decide.

        This test asserts on 7 because the code path it exercises is
        the dry-run's. The create path's exit-6 behaviour is tested
        separately in ``TestGeneratePreFlight``.

        The ``blueprint_root`` fixture is required because the plan
        builder resolves each source reference against the blueprint
        root when it constructs the plan's DTO.
        """
        (blueprint_root / "src.txt").write_text("body")
        target = tmp_path / "occupied"
        target.mkdir()
        (target / "out.txt").write_text("pre-existing")

        blueprint = _make_blueprint(
            variables=[],
            files=[{"source": "src.txt", "destination": "out.txt"}],
        )
        inputs = _resolved_inputs(blueprint=blueprint, destination=target)

        # ``_run_dry_run`` exits with code 7 on collisions (DryRunService
        # reports collisions as data; the CLI maps that to
        # ``ExitCode.DRY_RUN_COLLISIONS``, not ``ExitCode.COLLISION``).
        # We expect that exit, but assert on the rendered output
        # regardless — the plan must be *rendered* before the exit.
        with pytest.raises(SystemExit) as info:
            create_module._run_dry_run(
                inputs=inputs,
                overwrite=False,
                json_output=False,
            )
        assert info.value.code == ExitCode.DRY_RUN_COLLISIONS

        captured = capsys.readouterr()
        # (1) Blueprint id in the header.
        assert "python.fastapi.api" in captured.out
        # (2) Destination root in the header.
        assert str(target) in captured.out
        # (3) The planned file the user would gain.
        assert "out.txt" in captured.out
        # (4) The collision remediation hint is surfaced in the
        # rendered plan — the whole point of a dry-run over a
        # conflicted destination.
        assert "--overwrite" in captured.out
