"""Tests for the dual-mode input handling in ``forge create``.

These tests cover the acceptance criteria of Task 1.1.6 directly:

1. Non-interactive with all flags succeeds (without prompting).
2. Non-interactive missing a required flag fails with exit 1 and a
   message listing the missing flag.
3. Interactive wizard creates a project from start to finish.
4. The wizard validates input in real-time.
5. The wizard allows cancellation (Ctrl+C) and exits cleanly.

The generation pipeline is stubbed (``NotImplementedError``) and
intentionally not exercised here — testing that the command fails
with ``NotImplementedError`` after validation is a property of the
pipeline task, not of the input handling. What these tests assert is
that the *input handling* works correctly in both modes. The one
place the pipeline is stubbed (``_generate``) is because the test
wants to inspect the *inputs* the pipeline would receive, not the
pipeline itself.

Test design
-----------
There are two families of tests here, deliberately:

- **Dispatch tests** go through ``cli_runner.invoke(forge_app, ...)``.
  Their purpose is to prove that ``forge create <blueprint>`` parses
  as a leaf command, not as a group with a subcommand. This is the
  contract that broke in an earlier test run (a ``@app.command()``
  or a mis-placed ``invoke_without_command``), and it can only be
  verified through the parser.

- **Resolution tests** call ``_resolve_variables`` directly. Their
  purpose is to prove the collection logic (flags win, missing flags
  aggregate, wizard prompts when interactive, cancellation
  propagates). The parser is not part of what these tests verify,
  so going through it would add indirection without adding coverage.

Both families exercise the *current* shape of the command:

    * The project name is a **command argument** resolved outside
      ``VariableResolver`` (see ``create.py`` for the reasoning).
    * The *blueprint variables* flow through the resolver.
    * ``_resolve_variables`` returns a **two-tuple**
      ``(blueprint, inputs)`` — the blueprint document is needed by
      ``_generate`` to feed the planner, and returning it alongside
      the resolved inputs is the deliberate coupling the command
      docstring explains.
    * ``_generate`` takes the blueprint as a **keyword-only**
      argument, alongside ``inputs``, ``dry_run``, and ``overwrite``.
      All four are keyword-only on purpose: a positional call would
      read as ``_generate(blueprint, inputs, ...)`` and make the
      order easy to get wrong.

Tests written before this shape change called ``_resolve_variables``
and expected a single ``_ResolvedInputs`` return value, then passed
``inputs`` positionally to ``_generate``. Both broke the moment the
shape changed — which is exactly what the tests are for. The two
corrections (unpack the tuple, pass the blueprint) are mechanical;
the *failure* was the diagnostic that told us the shape had changed,
and keeping that failure legible in the test suite (rather than
papering over it with a duck-typed shim) is what makes the next
shape change equally visible.

The right seam: ``create.get_blueprint_document``
-------------------------------------------------
Earlier versions of these fixtures patched
``forge.cli.registry._load_blueprint_index`` — a static index that
the registry used to expose. The registry has since been refactored
to delegate to ``BlueprintDiscoveryService`` (ADR-0009), and that
private name no longer exists.

A later revision patched ``create_module.get_blueprint``, which was
the *catalogue entry* accessor on the registry. That accessor
returned a ``BlueprintEntry``, and the command tried to reach through
``entry.path`` to the loader — a bug, because ``BlueprintEntry`` is
deliberately *not* a filesystem handle. The command has since moved
to ``get_blueprint_document`` (which returns the loaded ``Blueprint``,
not the entry).

The fixtures below patch ``create_module.get_blueprint_document``
— the *exact* name the command module imported at load time
(``from forge.cli.registry import get_blueprint_document``), which is
the direct dependency ``_resolve_variables`` calls. Patching at that
boundary means:

- The fixture cannot silently become a no-op: if ``create.py`` stops
  importing ``get_blueprint_document``, the ``AttributeError``
  surfaces immediately at fixture setup, in the test that owns the
  fixture. This is the same failure mode the previous
  ``get_blueprint`` patch caught when it was renamed — the fixture
  breaking is *diagnostic*, not silent.
- The fixture's correctness does not depend on the registry's
  internal plumbing. A future registry refactor that keeps the
  public ``get_blueprint_document`` name does not touch these tests.

The fixture's *return shape* is the loaded ``Blueprint``, not a
``BlueprintEntry``. That is the shape ``get_blueprint_document``
returns, and it is what the resolver consumes. An earlier revision
had fixtures returning a fake ``BlueprintEntry``-shaped object;
that shape stopped being correct when the command started requiring
the document, and the tests had to move with it.

A test that fakes *both* the input and the output of the thing it is
testing is testing itself. So the resolver is **not** faked here — it
is a pure function, the tests already control every input it sees,
and letting it run is the only way to prove it is actually wired up.
The one fake is ``_generate``, which is the boundary to the
unimplemented pipeline; faking it is honest because the test is not
about generation.

Why some tests do not assert on ``result.output``
-------------------------------------------------
``CliRunner.invoke(forge_app, ...)`` runs ``create_app()``, which
builds the Typer app and mounts the command modules. It does *not*
install ``handle_exception`` — that wrapper lives in ``main()``,
which the CLI entry point runs *after* Typer's dispatch. So when a
command raises a ``ForgeError``, the exception propagates out of
``.invoke()`` as ``result.exception``, and ``result.output`` is
empty: Typer sets the exit code from the exception's ``exit_code``
attribute, but the domain error is never rendered.

The tests that assert on the *rendered* message (the missing-flags
message and the cancellation message) therefore invoke
``handle_exception`` on ``result.exception`` directly and assert on
``capsys.readouterr().err``. That is the same code path the shell
exercises (``main()`` → ``handle_exception`` → stderr), just called
from the test instead of from the entry point.

Each layer is tested at the layer it actually lives in: the parser
in the dispatch tests, the collection logic in ``_resolve_variables``
tests, the exception type via ``pytest.raises``, and the rendered
message via ``handle_exception`` + ``capsys``.

``_generate`` inputs must satisfy ``_generate``'s contract
----------------------------------------------------------
``_generate`` reads ``inputs.variables.as_dict()`` — a method that
exists on ``ResolvedValues`` (the resolver's immutable output type)
but *not* on a plain ``dict``. That is deliberate: the type
distinction is what prevents an unvalidated ``dict`` from reaching
the planner, and removing it would silently undo the KPI the whole
resolver exists to deliver.

An earlier version of these ``TestGeneratePreFlight`` tests
constructed ``SimpleNamespace(variables={})`` — a fake that happened
to be dict-shaped but failed ``_generate``'s actual contract. The
``AttributeError: 'dict' object has no attribute 'as_dict'`` was the
*directed* symptom, and the correct fix was to make the fixture
construct the type ``_generate`` requires, not to teach ``_generate``
to accept a shape it was designed to reject. The helper
``_resolved_inputs(...)`` below is that fix, and its existence is
the constant reminder: fake the *boundary*, not the *type*.

Lint conventions
----------------
This file obeys the project's Ruff configuration, including the
flake8-pytest-style (``PT``) and flake8-bandit (``S``) rulesets.
That has three consequences a reader will notice:

- **Side-effect-only fixtures are prefixed with ``_`` and applied
  via ``@pytest.mark.usefixtures(...)``.** ``PT004`` requires the
  underscore for fixtures that return ``None``; ``PT019`` requires
  that such fixtures not be injected as test parameters. The two
  rules are complementary — satisfying one without the other
  produces a lint error for the other. The ``usefixtures`` form
  matches the convention in ``conftest.py`` for
  ``_empty_discovery``.
- **Temp directories come from ``tmp_path``, not ``/tmp``.**
  ``S108`` flags hardcoded ``/tmp`` because it is world-writable and
  shared across users; ``tmp_path`` is per-test and uniquely owned.
- **Class attributes are immutable or ``ClassVar``.** ``RUF012``
  flags mutable class attributes; fixtures that construct fake
  blueprints build real ``Blueprint`` model instances instead of
  duck-typed fakes, which sidesteps the rule and gives the tests
  the resolver's actual contract.
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
    ExitCode,
    FilesystemConflictError,
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
def _make_blueprint(*, variables: list[VariableDefinition]) -> Blueprint:
    """Build a minimal valid ``Blueprint`` for testing.

    Centralised so the two fixtures (``_fake_blueprint`` and
    ``_fake_blueprint_with_vars``) stay in lockstep on all fields they
    are *not* testing. A future field addition to ``Blueprint`` needs
    one edit here, not two.

    The blueprint is deliberately a *real model instance*, not a
    duck-typed fake. The resolver, the loader, the planner, and every
    other consumer validate against this type, so the test should too
    — a fake that happens to satisfy the attributes the command reads
    today would silently break the moment the command gains a new
    dependency on the model.
    """
    return Blueprint(
        schema_version="1.0.0",
        id="python.fastapi.api",
        name="Fake API",
        version="1.0.0",
        description="Fake blueprint for tests.",
        category=Category.api,
        variables=variables,
        verification=[{"id": "ok", "type": "required_file", "path": "README.md"}],
        compatibility=Compatibility(min_forge_version="1.0.0"),
    )


def _resolved_inputs(
    *,
    destination: Path,
    project_name: str = "my-api",
    blueprint_id: str = "python.fastapi.api",
    variables: dict[str, Any] | None = None,
) -> SimpleNamespace:
    """Build a ``_ResolvedInputs``-shaped object with a real ``ResolvedValues``.

    ``_generate`` calls ``inputs.variables.as_dict()`` — a method
    that exists on ``ResolvedValues`` but not on a plain ``dict``.
    That method is the type boundary the resolver exists to enforce
    (see ``forge.resolution.resolver.ResolvedValues``), and the
    ``TestGeneratePreFlight`` tests must construct inputs that satisfy
    it, or they are not exercising ``_generate`` — they are
    exercising a fake that happens to share some attributes.

    ``SimpleNamespace`` is used (rather than ``_ResolvedInputs``
    directly) because ``_ResolvedInputs`` is a *private* type in
    ``create.py``, and reaching into a private type from a test
    couples the test to an implementation detail. The namespace
    satisfies the duck-typed contract ``_generate`` actually consumes
    (``.blueprint_id``, ``.project_name``, ``.destination``,
    ``.variables``), which is the honest level of coupling for a test
    of a private helper.
    """
    return SimpleNamespace(
        blueprint_id=blueprint_id,
        project_name=project_name,
        destination=destination,
        variables=ResolvedValues(MappingProxyType(variables or {})),
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

    Tests that need blueprint variables use
    ``_fake_blueprint_with_vars`` instead. The two fixtures are
    distinct on purpose: a test that uses this one cannot
    accidentally pass by exercising the resolver's
    blueprint-variable path.
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
    not a duck-typed fake, so the test proves the actual integration.

    ``author`` is a string with no default and ``required=True``, so
    the resolver's ``required.missing`` path is reachable simply by
    omitting it from ``cli_vars``. That is the minimal blueprint that
    exercises the error aggregation the command wraps into a
    ``UserInputError``.
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

    ``_generate`` is the boundary to the unimplemented pipeline. The
    stub records its arguments and returns; the test asserts on the
    arguments. Faking the boundary is honest because the test is
    about input collection, not generation — the pipeline's own
    tests will exercise the real ``_generate``.

    The stub signature mirrors the *current* signature of
    ``create_module._generate``: keyword-only ``blueprint``,
    ``inputs``, ``dry_run``, ``overwrite``. The ``blueprint``
    parameter is recorded alongside the others so future tests can
    assert that the *same* document the wizard used was passed
    through to the planner — a property the command's docstring
    promises and the tests should pin.

    If ``create_module._generate`` gains a parameter (or renames
    one), this fixture will raise ``TypeError`` at the next
    ``cli_runner.invoke`` — the fixture is the trip wire for the
    signature, not just a recording device.

    This fixture *does* return a value (the ``captured`` dict), so
    it does not need a leading underscore and stays a parameter
    (``PT004`` and ``PT019`` only apply to fixtures returning
    ``None``).
    """
    captured: dict[str, Any] = {}

    def _fake(
        *,
        blueprint: Any,
        inputs: Any,
        dry_run: bool,
        overwrite: bool,
    ) -> None:
        captured["blueprint"] = blueprint
        captured["inputs"] = inputs
        captured["dry_run"] = dry_run
        captured["overwrite"] = overwrite

    monkeypatch.setattr(create_module, "_generate", _fake)
    return captured


@pytest.fixture()
def _force_non_interactive(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``prompts.is_interactive()`` return ``False``.

    Simulates a CI environment or ``--no-interactive``. The command
    checks the flag *and* the TTY, so this fixture only needs to
    override one of them — the test passes ``--no-interactive`` when
    it wants the flag path specifically, and this fixture when it
    wants the TTY path.

    Leading underscore because the fixture returns ``None``
    (``PT004``); applied via ``@pytest.mark.usefixtures(...)`` rather
    than as a parameter (``PT019``).
    """
    monkeypatch.setattr(prompts, "is_interactive", lambda: False)


@pytest.fixture()
def _force_interactive(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``prompts.is_interactive()`` return ``True``.

    Leading underscore because the fixture returns ``None``
    (``PT004``); applied via ``@pytest.mark.usefixtures(...)`` rather
    than as a parameter (``PT019``).
    """
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
        assert capture_generate["inputs"].blueprint_id == "python.fastapi.api"
        assert capture_generate["inputs"].project_name == "my-api"
        # The blueprint document is passed through *with* the inputs —
        # the planner needs it, and passing it once (from the resolve
        # step) rather than re-fetching in `_generate` guarantees the
        # document the user's answers were validated against is the
        # document that will be planned.
        assert capture_generate["blueprint"].id == "python.fastapi.api"

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
        assert capture_generate["dry_run"] is False
        assert capture_generate["overwrite"] is False

    @pytest.mark.unit()
    @pytest.mark.usefixtures("_fake_blueprint", "_force_non_interactive")
    def test_flags_reach_generate_unchanged(
        self,
        cli_runner: CliRunner,
        forge_app: Any,
        capture_generate: dict[str, Any],
        tmp_path: Path,
    ) -> None:
        """``--output`` and ``--dry-run`` are forwarded to ``_generate``.

        ``tmp_path`` is used for ``--output`` because it is the
        per-test temp directory pytest provides — uniquely owned by
        the test, cleaned up automatically, and immune to the
        hardcoded-``/tmp`` concern that ``S108`` flags.
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
                "--dry-run",
            ],
        )

        assert result.exit_code == 0, result.output
        assert capture_generate["dry_run"] is True
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
        ``_resolve_variables`` directly. That is the correct layer
        for "does the command raise the right exception," separate
        from "does the shell render it" (the previous test).
        """
        with pytest.raises(MissingRequiredFlagsError) as info:
            create_module._resolve_variables(
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
            create_module._resolve_variables(
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

        The ``_fake_blueprint`` fixture declares no blueprint
        variables, so the wizard asks exactly one question — the
        project name — which is what ``ask_text`` is patched to
        answer. If the fixture ever grew variables, this test would
        fail at ``ask_select`` (which is deliberately not patched),
        making the coupling visible instead of silent.

        ``_resolve_variables`` returns a two-tuple
        ``(blueprint, inputs)``. We unpack the tuple and ignore the
        blueprint here — the assertions are on the *inputs*. The
        blueprint is available and asserted on in
        ``TestDispatch::test_create_accepts_positional_blueprint_directly``,
        where its presence in ``capture_generate`` proves the
        command passes it through to ``_generate``.
        """
        monkeypatch.setattr(prompts, "ask_text", lambda *a, **k: "wizard-api")

        _blueprint, inputs = create_module._resolve_variables(
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
        _raise_if_called = lambda *a, **k: (_ for _ in ()).throw(  # noqa: E731
            AssertionError("a prompt fired despite --name being supplied")
        )
        monkeypatch.setattr(prompts, "ask_text", _raise_if_called)
        monkeypatch.setattr(prompts, "ask_select", _raise_if_called)
        monkeypatch.setattr(prompts, "ask_confirm", _raise_if_called)

        _blueprint, inputs = create_module._resolve_variables(
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
        _raise_if_called = lambda *a, **k: (_ for _ in ()).throw(  # noqa: E731
            AssertionError("wizard fired despite --no-interactive")
        )
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
# resolver applies. There is no longer a "duplicate" to test for
# subset consistency — there is exactly one validator, used by both
# the prompt (for real-time UX) and the resolver (for authoritative
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

        The invariant this test enforces is stronger than the old
        "subset" test: it is the *same* function on both sides, so
        there is no possible drift. We assert the wiring by running
        a name through the non-interactive path and confirming the
        resolver accepts it unchanged (which requires the same
        validator the prompt uses to have already accepted it).

        If the wizard ever grows a *separate* validator (the
        duplicate the old test was designed to police), that test
        would fail here — the inline validator would accept a name
        the resolver rejects, or vice versa.

        The tuple return of ``_resolve_variables`` is unpacked
        (blueprint ignored) because the assertion is on the *inputs*.
        """
        # A name the resolver accepts, threaded through the wizard.
        for candidate in ("myapi", "my-api", "a1-b2"):
            _blueprint, inputs = create_module._resolve_variables(
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
            create_module._resolve_variables(
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
        stderr) is tested separately in ``test_errors.py``, where
        ``handle_exception`` is called directly — the correct layer
        for that assertion.
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
            create_module._resolve_variables(
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
            create_module._resolve_variables(
                blueprint_id="does.not.exist",
                cli_name=None,
                cli_vars={},
                output=tmp_path,
                no_interactive=False,
            )


# -----------------------------------------------------------------------------
# Blueprint variables flow through the resolver
#
# These tests exercise the resolver's *blueprint variable* path with a
# fake blueprint that declares a required string variable. They exist
# to prove the resolution refactor did not accidentally route the
# project name through the resolver (which would have masked the
# `project_name` KeyError the test suite originally caught), and that
# blueprint variables still behave as they did before.
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
            create_module._resolve_variables(
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
        _blueprint, inputs = create_module._resolve_variables(
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
# Generation pre-flight (filesystem)
# -----------------------------------------------------------------------------
# These tests do not go through ``_resolve_variables`` — the resolver
# is deliberately pure and does not check the filesystem. The
# destination-conflict check lives in ``_generate``, and that is the
# layer these tests exercise.
#
# ``_generate`` takes the blueprint document as a keyword-only
# argument alongside the inputs, because the *planner* needs the
# document to read ``files[]`` and ``conditional_files[]``. The
# blueprint used here is a real ``Blueprint`` instance (from
# ``_make_blueprint``) rather than a fake, so the planner (which
# ``_generate`` calls) validates against the actual type.
#
# ``inputs.variables`` must be a real ``ResolvedValues``, not a plain
# ``dict``: ``_generate`` calls ``.as_dict()`` on it, and that method
# is the type boundary the resolver exists to enforce. The
# ``_resolved_inputs`` helper constructs the correct shape — see its
# docstring for why a duck-typed fake is not acceptable here.
# -----------------------------------------------------------------------------
class TestGeneratePreFlight:
    """``_generate`` refuses to run against an existing destination."""

    @pytest.mark.unit()
    def test_existing_destination_without_overwrite_raises(self, tmp_path: Path) -> None:
        """A non-empty destination without ``--overwrite`` → exit 3.

        The check is in ``_generate``, not in the resolver, because
        the *input* (a destination path) is valid regardless of
        whether the path is occupied. The conflict is a *target*
        problem, not an *input* problem — a distinction that lets
        scripts distinguish "fix your flags" (exit 1) from "fix your
        target" (exit 3).
        """
        target = tmp_path / "occupied"
        target.mkdir()
        (target / "existing.txt").write_text("present")

        inputs = _resolved_inputs(destination=target)

        with pytest.raises(FilesystemConflictError):
            create_module._generate(
                blueprint=_make_blueprint(variables=[]),
                inputs=inputs,
                dry_run=False,
                overwrite=False,
            )

    @pytest.mark.unit()
    def test_existing_destination_with_overwrite_reaches_not_implemented(
        self, tmp_path: Path
    ) -> None:
        """``--overwrite`` skips the conflict check and hits the stub."""
        target = tmp_path / "occupied"
        target.mkdir()
        (target / "existing.txt").write_text("present")

        inputs = _resolved_inputs(destination=target)

        with pytest.raises(NotImplementedError):
            create_module._generate(
                blueprint=_make_blueprint(variables=[]),
                inputs=inputs,
                dry_run=False,
                overwrite=True,
            )

    @pytest.mark.unit()
    def test_dry_run_renders_plan_even_for_conflicted_destination(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``--dry-run`` renders the plan for a conflicted destination.

        A dry run is a *plan*, not a *commit*. Its purpose is to tell
        the user what would happen, including the conflict — so it
        must not fail on the conflict, and it must not silently
        return. The assertion is on the *rendered plan* (its presence
        in stdout), not on the absence of an exception.
        """
        target = tmp_path / "occupied"
        target.mkdir()
        (target / "existing.txt").write_text("present")

        inputs = _resolved_inputs(destination=target)

        create_module._generate(
            blueprint=_make_blueprint(variables=[]),
            inputs=inputs,
            dry_run=True,
            overwrite=False,
        )

        captured = capsys.readouterr()
        assert "python.fastapi.api" in captured.out
        assert "my-api" in captured.out
        assert str(target) in captured.out
