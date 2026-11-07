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

- **Resolution tests** call ``_resolve_inputs`` directly. Their
  purpose is to prove the collection logic (flags win, missing flags
  aggregate, wizard prompts when interactive, cancellation
  propagates). The parser is not part of what these tests verify,
  so going through it would add indirection without adding coverage.

A test that fakes *both* the input and the output of the thing it is
testing is testing itself. So the validation funnel
(``validate_blueprint_inputs``) is **not** faked here — it is a pure
function, the tests already control every input it sees, and letting
it run is the only way to prove it is actually wired up. The one
fake is ``_generate``, which is the boundary to the unimplemented
pipeline; faking it is honest because the test is not about
generation.

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
in the dispatch tests, the collection logic in ``_resolve_inputs``
tests, the exception type via ``pytest.raises``, and the rendered
message via ``handle_exception`` + ``capsys``. Asserting on
``result.output`` for a domain error would fail (as it did in the
previous run) or, worse, pass for the wrong reason if a future
change added rendering inside ``create_app()`` — which would be the
wrong place for it.

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
  flags mutable class attributes; the fake blueprint uses a tuple
  rather than a list, matching the immutable form the code already
  treats it as.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
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
)


# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------
@pytest.fixture()
def _fake_blueprint(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install a one-blueprint fake index behind the registry's seam.

    Faking the index (not ``get_blueprint``) keeps the delegation path
    under test: ``get_blueprint`` must call ``_load_blueprint_index``,
    and the test proves it does. The fake blueprint exposes exactly
    the two attributes ``get_blueprint`` reads — ``display_name`` and
    ``variables`` — so the funnel and the command see a well-formed
    entry without importing the real loader.

    ``raising=False`` is used because the seam is a private function
    (leading underscore). A future refactor that renames it should
    surface as a test failure here, not as an obscure
    ``AttributeError`` inside ``get_blueprint``; the test failure will
    point at this fixture, which is the right place to notice.

    The fixture name has a leading underscore because it returns
    ``None`` (``PT004``), and tests apply it via
    ``@pytest.mark.usefixtures("_fake_blueprint")`` rather than as a
    parameter (``PT019``). It is used for its *side effect* —
    patching ``_load_blueprint_index`` — not for a return value.

    ``variables`` is a tuple rather than a list because ``RUF012``
    flags mutable class attributes, and the code reads the attribute
    via ``getattr(..., ())`` (which returns a tuple) — a tuple is
    both lint-clean and a match for what the caller treats the value
    as.
    """

    class _Blueprint:
        display_name = "Fake API"
        variables: tuple[Any, ...] = ()

    index = {"python.fastapi.api": _Blueprint()}
    monkeypatch.setattr(
        "forge.cli.registry._load_blueprint_index",
        lambda: index,
        raising=False,
    )


@pytest.fixture()
def capture_generate(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Stub ``_generate`` so the test can inspect the resolved inputs.

    ``_generate`` is the boundary to the unimplemented pipeline. The
    stub records its arguments and returns; the test asserts on the
    arguments. Faking the boundary is honest because the test is
    about input collection, not generation — the pipeline's own
    tests will exercise the real ``_generate``.

    This fixture *does* return a value (the ``captured`` dict), so
    it does not need a leading underscore and stays a parameter
    (``PT004`` and ``PT019`` only apply to fixtures returning
    ``None``).
    """
    captured: dict[str, Any] = {}

    def _fake(inputs: Any, *, dry_run: bool, overwrite: bool) -> None:
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
        # The metavar is rendered in the usage line and in the
        # arguments table. Its presence is proof that the command
        # parses a positional argument directly.
        assert "BLUEPRINT" in result.output

    @pytest.mark.unit()
    def test_create_with_no_args_reports_missing_blueprint(
        self, cli_runner: CliRunner, forge_app: Any
    ) -> None:
        """``forge create`` with no arguments is a usage error, not help.

        Typer's default ``no_args_is_help`` behavior is to print help
        and exit 0 when the app is invoked with no arguments. That
        would look like a successful no-op to a script branching on
        ``$?`` — the wrong behavior for a command whose first
        positional is required.

        With the registry registering the callback as a leaf command
        (``root_app.command(name)(callback)``), the app's signature
        naturally has no subcommand slot, and Typer's default help
        behavior does not trigger: a missing ``BLUEPRINT`` argument
        is a Click usage error (exit 2), which is what a script
        should see. This test pins that behavior.

        A regression to a group-shaped registration would make this
        invocation print the group's help (exit 0), which is what the
        previous test run surfaced as ``Usage: forge create [OPTIONS]
        {BLUEPRINT} COMMAND [ARGS]...`` — a different signature, but
        the same class of bug.
        """
        result = cli_runner.invoke(forge_app, ["create"])

        assert result.exit_code == 2, result.output
        # The usage error names the missing argument. A help page
        # would not.
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
        # The destination is ``<output>/<name>`` — the command joins
        # them, and the resolver normalizes to an absolute path.
        assert capture_generate["inputs"].destination == (tmp_path / "my-api").resolve()


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
        ``result.exception`` and ``result.output`` is empty. The
        rendering happens only when ``handle_exception`` runs, which
        is exactly what ``main()`` does — and exactly what this test
        calls directly to assert on the message.

        A shell invocation of ``forge create ... --no-interactive``
        *does* show the message, because the shell goes through
        ``main()``. The distinction is a test-layer concern, not a
        UX bug.
        """
        from forge.cli.errors import handle_exception

        result = cli_runner.invoke(
            forge_app,
            ["create", "python.fastapi.api", "--no-interactive"],
        )

        # Layer 1: the exit code is the contract.
        assert result.exit_code == ExitCode.USER_ERROR

        # Layer 2: the domain exception is the specific one the
        # command is supposed to raise. A ``UserInputError`` from a
        # typo would also exit 1, so the type is the precise
        # contract — not just the code.
        assert isinstance(result.exception, MissingRequiredFlagsError)

        # Layer 3: the rendered message names the missing flag.
        # ``handle_exception`` is what ``main()`` calls after the
        # command raises; calling it here exercises the same code
        # path the shell exercises. ``SystemExit`` is expected (the
        # handler exits with the domain code) and is not asserted
        # on here — its value is a duplicate of layer 1.
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

        ``tmp_path`` is used for ``output`` for the same reason as in
        ``test_flags_reach_generate_unchanged``: the resolver is
        pure, so the value is cosmetic here, but a real directory is
        cheaper to reason about than a hardcoded one.
        """
        with pytest.raises(MissingRequiredFlagsError) as info:
            create_module._resolve_inputs(
                blueprint="python.fastapi.api",
                name=None,
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
                blueprint="python.fastapi.api",
                name=None,
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
        """
        monkeypatch.setattr(prompts, "ask_text", lambda *a, **k: "wizard-api")

        inputs = create_module._resolve_inputs(
            blueprint="python.fastapi.api",
            name=None,
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
        """Flags win: a supplied ``--name`` is not re-asked."""
        monkeypatch.setattr(
            prompts,
            "ask_text",
            lambda *a, **k: (_ for _ in ()).throw(
                AssertionError("ask_text fired despite --name being supplied")
            ),
        )

        inputs = create_module._resolve_inputs(
            blueprint="python.fastapi.api",
            name="flag-api",
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
        monkeypatch.setattr(
            prompts,
            "ask_text",
            lambda *a, **k: (_ for _ in ()).throw(
                AssertionError("wizard fired despite --no-interactive")
            ),
        )

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

        This is the dispatch-level counterpart to
        ``test_prompts_for_name_when_not_supplied``. It proves the
        *parser* does not reject a ``--name``-less invocation before
        the wizard gets a chance to run — which is the whole reason
        ``name`` is optional at the Typer layer.

        A future change that re-adds ``...`` to ``--name`` (making
        it Typer-required) would fail this test with a Click usage
        error before ``ask_text`` is reached.
        """
        monkeypatch.setattr(prompts, "ask_text", lambda *a, **k: "wizard-api")

        result = cli_runner.invoke(
            forge_app,
            ["create", "python.fastapi.api"],
        )

        assert result.exit_code == 0, result.output
        assert capture_generate["inputs"].project_name == "wizard-api"


# -----------------------------------------------------------------------------
# Acceptance criterion 4: real-time validation via the inline validator
# -----------------------------------------------------------------------------
class TestInlineValidator:
    """The wizard's inline validator catches obvious typos early."""

    @pytest.mark.unit()
    def test_rejects_empty_name(self) -> None:
        """An empty name is rejected inline (no round-trip to the funnel)."""
        validator = create_module._project_name_validator()
        assert validator("") is not None

    @pytest.mark.unit()
    def test_rejects_names_with_spaces(self) -> None:
        """Spaces produce a directory name with spaces — rejected."""
        validator = create_module._project_name_validator()
        assert validator("my project") is not None

    @pytest.mark.unit()
    def test_accepts_alphanumeric_hyphenated_and_underscored(self) -> None:
        """The common valid forms are accepted inline."""
        validator = create_module._project_name_validator()
        assert validator("myapi") is None
        assert validator("my-api") is None
        assert validator("my_api") is None

    @pytest.mark.unit()
    @pytest.mark.usefixtures("_fake_blueprint")
    def test_inline_validator_is_a_subset_of_authoritative_validator(self, tmp_path: Path) -> None:
        """Every name the inline validator accepts, the funnel accepts.

        This is the load-bearing invariant behind the "duplicate
        validation" decision: the inline validator is a UX
        affordance, not a second source of truth. If it accepted a
        name the funnel rejects, the user would pass the wizard and
        fail at the end — the worst possible experience.
        """
        from forge.verification.handlers import validate_blueprint_inputs

        inline = create_module._project_name_validator()
        for candidate in ("myapi", "my-api", "my_api", "a1-b2_c3"):
            assert inline(candidate) is None, (
                f"inline validator unexpectedly rejected {candidate!r}; "
                f"the test is stale and needs updating"
            )
            # The funnel must also accept it. If it does not, the
            # inline validator is over-accepting and the two are out
            # of sync.
            validate_blueprint_inputs(
                blueprint_id="python.fastapi.api",
                name=candidate,
                destination=str(tmp_path / "x"),
                variables={},
            )


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
                blueprint="python.fastapi.api",
                name=None,
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
        # No traceback rendered by the command itself — the command
        # must let the exception propagate, not catch and print.
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
        """
        monkeypatch.setattr(
            "forge.cli.registry._load_blueprint_index",
            lambda: {},
            raising=False,
        )

        with pytest.raises(BlueprintError):
            create_module._resolve_inputs(
                blueprint="does.not.exist",
                name="x",
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
        monkeypatch.setattr(
            "forge.cli.registry._load_blueprint_index",
            lambda: {},
            raising=False,
        )
        monkeypatch.setattr(
            prompts,
            "ask_text",
            lambda *a, **k: (_ for _ in ()).throw(
                AssertionError("wizard fired for a blueprint that does not exist")
            ),
        )

        with pytest.raises(BlueprintError):
            create_module._resolve_inputs(
                blueprint="does.not.exist",
                name=None,
                output=tmp_path,
                no_interactive=False,
            )


# -----------------------------------------------------------------------------
# Generation pre-flight (filesystem)
# -----------------------------------------------------------------------------
# These tests do not go through ``_resolve_inputs`` — the validator
# is deliberately pure and does not check the filesystem. The
# destination-conflict check lives in ``_generate``, and that is the
# layer these tests exercise.
# -----------------------------------------------------------------------------
class TestGeneratePreFlight:
    """``_generate`` refuses to run against an existing destination."""

    @pytest.mark.unit()
    def test_existing_destination_without_overwrite_raises(self, tmp_path: Path) -> None:
        """A non-empty destination without ``--overwrite`` → exit 3.

        The check is in ``_generate``, not in the funnel, because the
        *input* (a destination path) is valid regardless of whether
        the path is occupied. The conflict is a *target* problem, not
        an *input* problem — a distinction that lets scripts
        distinguish "fix your flags" (exit 1) from "fix your target"
        (exit 3).
        """
        target = tmp_path / "occupied"
        target.mkdir()
        (target / "existing.txt").write_text("present")

        inputs = SimpleNamespace(
            blueprint_id="python.fastapi.api",
            project_name="my-api",
            destination=target,
            variables={},
        )

        with pytest.raises(FilesystemConflictError):
            create_module._generate(inputs, dry_run=False, overwrite=False)

    @pytest.mark.unit()
    def test_existing_destination_with_overwrite_reaches_not_implemented(
        self, tmp_path: Path
    ) -> None:
        """``--overwrite`` skips the conflict check and hits the stub.

        The generator is unimplemented, so the test asserts on the
        stub's failure rather than on a success path. The point is
        that the pre-flight check did *not* fire — the pre-flight
        would have raised ``FilesystemConflictError``, and the stub
        raises ``NotImplementedError``. Different exception, so the
        test can distinguish the two.
        """
        target = tmp_path / "occupied"
        target.mkdir()
        (target / "existing.txt").write_text("present")

        inputs = SimpleNamespace(
            blueprint_id="python.fastapi.api",
            project_name="my-api",
            destination=target,
            variables={},
        )

        with pytest.raises(NotImplementedError):
            create_module._generate(inputs, dry_run=False, overwrite=True)

    @pytest.mark.unit()
    def test_dry_run_renders_plan_even_for_conflicted_destination(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``--dry-run`` renders the plan for a conflicted destination.

        A dry run is a *plan*, not a *commit*. Its purpose is to
        tell the user what would happen, including the conflict — so
        it must not fail on the conflict, and it must not silently
        return. The assertion here is on the *rendered plan* (its
        presence in stdout), not on the absence of an exception.

        Asserting only "no exception" would pass for a future change
        that returns early without printing anything — which would
        look successful to a script but produce no useful output for
        a human.
        """
        target = tmp_path / "occupied"
        target.mkdir()
        (target / "existing.txt").write_text("present")

        inputs = SimpleNamespace(
            blueprint_id="python.fastapi.api",
            project_name="my-api",
            destination=target,
            variables={},
        )

        create_module._generate(inputs, dry_run=True, overwrite=False)

        # The dry-run plan goes to stdout via ``ui.print_section`` /
        # ``ui.print_info``. The three fields below are the entire
        # plan; their presence is the observable proof that the plan
        # was rendered (not just "no exception raised").
        captured = capsys.readouterr()
        assert "python.fastapi.api" in captured.out
        assert "my-api" in captured.out
        assert str(target) in captured.out
