"""Unit tests for the Forge CLI entry point.

The Forge CLI has three testable layers, and this file tests each layer
at its own layer:

1. **The app** — built by ``create_app()`` and exercised via
   ``Typer.CliRunner``. These tests prove the commands, help text, and
   option parsing work.

2. **The exception handler** — ``_handle_exception``. These tests call
   the handler directly and assert on the ``typer.Exit`` exit code and
   the captured stdout. Testing the handler in isolation is faster and
   more focused than going through ``CliRunner``, because Typer's own
   error handling intercepts exceptions before the handler ever runs
   when the app is invoked directly.

3. **The entry point** — ``main()``. Tested via ``subprocess`` in one
   integration test that exercises the real ``python -m forge.cli``
   pathway.

Coverage map
------------
Acceptance criteria:

1. ``forge --help`` displays a usage message and exits 0.
2. ``forge --version`` displays the version from package metadata.
3. ``forge ping`` prints ``pong``.
4. The root help contains a description.
5. The CLI is invokable via ``python -m forge.cli``.

Additional behaviour:

6. ``_resolve_version`` falls back to a dev string when metadata is missing.
7. The ``--debug`` flag is parsed and stored on ``ctx.obj``.
8. ``ForgeError`` renders as a red error line, never a traceback.
9. Unexpected exceptions render a generic message and exit 5.
10. ``KeyboardInterrupt`` exits cleanly with "Cancelled."
11. ``--debug`` re-raises exceptions so contributors see tracebacks.

Design principles
-----------------
- **Shared fixtures first.** ``cli_runner``, ``forge_app``, and
  ``forge_executable`` come from ``tests/conftest.py``. Local fixtures
  are declared only when a test needs a modification (e.g., registering
  a ``_probe_*`` command on a private copy of the app).
- **No Click dependency.** Typer 0.28 does not depend on Click. This
  file imports only from ``typer`` and ``typer.testing``.
- **Every test maps to a documented behaviour.** If a test fails, the
  docstring tells the next contributor exactly what broke and why.
"""

from __future__ import annotations

import importlib
import subprocess

import pytest
import typer
from typer.testing import CliRunner

from forge.cli.main import _handle_exception, create_app
from forge.core.exceptions import ExitCode, UserInputError


# =============================================================================
# Local fixtures
# =============================================================================
# The shared ``cli_runner`` and ``forge_app`` fixtures live in
# ``tests/conftest.py``. The fixtures below are local overrides used only
# when a test needs a per-test modification — for example, a test that
# registers a ``_probe_*`` command on a private copy of the app so the
# command does not leak into other tests.
#
# If you only need to invoke the CLI, use the shared fixtures. If you need
# to register commands on the app, use the local ``app`` fixture below.
@pytest.fixture()
def app() -> typer.Typer:
    """Return a fresh, private Forge ``Typer`` application.

    Unlike the shared ``forge_app`` fixture, this fixture is intended to
    be *modified* by the test — for example, by registering a
    ``_probe_*`` command. Because each test gets its own instance, the
    command does not leak into other tests.
    """
    return create_app()


# =============================================================================
# Helpers
# =============================================================================
def _assert_no_traceback(text: str) -> None:
    """Assert that a captured output contains no raw traceback.

    A raw traceback is the single clearest sign that the exception
    handler failed to convert an exception into a user-facing message.
    Every test that triggers an error path should assert this.
    """
    assert "Traceback (most recent call last)" not in text, (
        "Raw traceback leaked to the user. The exception handler in "
        "`forge.cli.main._handle_exception` should have caught this."
    )


# =============================================================================
# Layer 1 — the app (create_app + CliRunner)
# =============================================================================


# -----------------------------------------------------------------------------
# Acceptance criterion 1 — forge --help displays a usage message
# -----------------------------------------------------------------------------
@pytest.mark.unit()
def test_help_displays_usage(cli_runner: CliRunner, forge_app: typer.Typer) -> None:
    """``forge --help`` prints a usage message and exits 0.

    Explicit help is a success. Click/Typer exit with code 0 when the
    user asks for help.
    """
    result = cli_runner.invoke(forge_app, ["--help"])
    assert result.exit_code == 0, result.output
    assert "Usage:" in result.output


@pytest.mark.unit()
def test_help_short_flag(cli_runner: CliRunner, forge_app: typer.Typer) -> None:
    """The short form ``-h`` is equivalent to ``--help``."""
    result = cli_runner.invoke(forge_app, ["-h"])
    assert result.exit_code == 0, result.output
    assert "Usage:" in result.output


@pytest.mark.unit()
def test_help_lists_all_commands(cli_runner: CliRunner, forge_app: typer.Typer) -> None:
    """The root help lists every registered command.

    This is the "table of contents" contract: if a command is added to
    ``create_app()`` but not to the help, users cannot discover it.
    """
    result = cli_runner.invoke(forge_app, ["--help"])
    assert result.exit_code == 0, result.output
    for command in ("list", "create", "validate", "verify", "doctor", "ping"):
        assert command in result.output, (
            f"Command {command!r} is registered but missing from `forge --help`. "
            "Check the `app.add_typer(...)` calls in `create_app()`."
        )


# -----------------------------------------------------------------------------
# Acceptance criterion 2 — forge --version displays the package version
# -----------------------------------------------------------------------------
@pytest.mark.unit()
def test_version_displays_package_version(cli_runner: CliRunner, forge_app: typer.Typer) -> None:
    """``forge --version`` prints the version from package metadata.

    The version shown must match ``importlib.metadata.version("forge-cli")``
    exactly. This is the single source of truth — no hard-coded strings
    can drift from it.
    """
    from importlib.metadata import version as pkg_version

    expected = pkg_version("forge-cli")
    result = cli_runner.invoke(forge_app, ["--version"])
    assert result.exit_code == 0, result.output
    assert "forge, version" in result.output
    assert expected in result.output


@pytest.mark.unit()
def test_version_short_flag(cli_runner: CliRunner, forge_app: typer.Typer) -> None:
    """The short form ``-V`` is equivalent to ``--version``."""
    result = cli_runner.invoke(forge_app, ["-V"])
    assert result.exit_code == 0, result.output
    assert "forge, version" in result.output


@pytest.mark.unit()
def test_version_is_eager(cli_runner: CliRunner, forge_app: typer.Typer) -> None:
    """``--version`` runs even when an invalid subcommand is also given.

    ``--version`` is marked ``is_eager=True`` in the root callback, so
    ``forge --version nonsense`` prints the version and exits 0 without
    trying to dispatch ``nonsense``. This matches the behaviour of
    ``git --version``, ``docker --version``, and every other serious CLI.
    """
    result = cli_runner.invoke(forge_app, ["--version", "nonsense-command"])
    assert result.exit_code == 0, result.output
    assert "forge, version" in result.output


# -----------------------------------------------------------------------------
# Acceptance criterion 3 — forge ping prints pong
# -----------------------------------------------------------------------------
@pytest.mark.unit()
def test_ping_prints_pong(cli_runner: CliRunner, forge_app: typer.Typer) -> None:
    """``forge ping`` prints ``pong`` and exits 0.

    The ``ping`` command is the cheapest possible health check: if it
    works, the CLI is installed, importable, and correctly wired to
    Typer. If it does not, the problem is installation, not the command
    the user was actually trying to run.
    """
    result = cli_runner.invoke(forge_app, ["ping"])
    assert result.exit_code == 0, result.output
    assert result.output.strip() == "pong"


@pytest.mark.unit()
def test_ping_is_documented_in_help(cli_runner: CliRunner, forge_app: typer.Typer) -> None:
    """``forge ping --help`` documents the command's purpose."""
    result = cli_runner.invoke(forge_app, ["ping", "--help"])
    assert result.exit_code == 0, result.output
    assert "pong" in result.output.lower()


# -----------------------------------------------------------------------------
# Acceptance criterion 4 — root help contains a description
# -----------------------------------------------------------------------------
@pytest.mark.unit()
def test_help_contains_description(cli_runner: CliRunner, forge_app: typer.Typer) -> None:
    """The root help contains the app description from ``create_app()``.

    A CLI without a description is a CLI nobody understands. This test
    pins the description so that a future contributor cannot quietly
    reduce it to ``None``.
    """
    result = cli_runner.invoke(forge_app, ["--help"])
    assert result.exit_code == 0, result.output
    assert "engineering foundations" in result.output.lower()


# -----------------------------------------------------------------------------
# Additional behaviour — no arguments shows help, exits 2
# -----------------------------------------------------------------------------
@pytest.mark.unit()
def test_no_args_shows_help_and_exits_two(cli_runner: CliRunner, forge_app: typer.Typer) -> None:
    """``forge`` with no args shows help and exits 2 (usage error).

    This is a deliberate contract, not an oversight. ``no_args_is_help``
    prints help when no command is given, but Typer still treats the
    invocation as a *usage error* and exits with code 2. The distinction
    between this and ``forge --help`` (exit 0) is a useful signal to
    scripts: a script that accidentally drops the command name should
    fail loudly, not silently succeed.
    """
    result = cli_runner.invoke(forge_app, [])
    assert result.exit_code == 2, result.output
    assert "Usage:" in result.output


# -----------------------------------------------------------------------------
# Additional behaviour — unknown commands exit non-zero
# -----------------------------------------------------------------------------
@pytest.mark.unit()
def test_unknown_command_exits_nonzero(cli_runner: CliRunner, forge_app: typer.Typer) -> None:
    """An unknown command exits with code 2 (usage error)."""
    result = cli_runner.invoke(forge_app, ["nonsense-command"])
    assert result.exit_code == 2, result.output


# -----------------------------------------------------------------------------
# Coverage: --debug flag is parsed and stored on ctx.obj
# -----------------------------------------------------------------------------
@pytest.mark.unit()
def test_debug_flag_is_stored_on_context(cli_runner: CliRunner, app: typer.Typer) -> None:
    """``--debug`` stores ``True`` on ``ctx.obj["debug"]``.

    The flag itself is a no-op in this task; it is consumed by the
    exception handler in ``main()``. This test pins down the contract
    that the flag is parsed and stored, so a future refactor cannot
    silently drop it.

    Uses the *local* ``app`` fixture (not ``forge_app``) because it
    registers a ``_probe_debug`` command on the app — a modification
    that must not leak into other tests.
    """

    @app.command("_probe_debug")
    def _probe_debug(ctx: typer.Context) -> None:
        typer.echo(f"debug={ctx.obj.get('debug')}")

    result = cli_runner.invoke(app, ["--debug", "_probe_debug"])
    assert result.exit_code == 0, result.output
    assert "debug=True" in result.output


# -----------------------------------------------------------------------------
# Coverage: version fallback when the package is not installed
# -----------------------------------------------------------------------------
@pytest.mark.unit()
def test_version_fallback_when_package_missing(
    cli_runner: CliRunner,
    forge_app: typer.Typer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_resolve_version`` returns a dev fallback when metadata is missing.

    This path is hit when Forge is run from a source checkout without an
    editable install. ``importlib.metadata`` cannot find the package,
    and the CLI must not crash — it must show a clearly-marked dev
    version so the user knows they are not on a released build.
    """
    from importlib.metadata import PackageNotFoundError

    # `import forge.cli.main as main_module` would bind the *function*
    # `main`, because `main` shadows the module in `__all__`. Use
    # `importlib.import_module` to get the actual module object.
    main_module = importlib.import_module("forge.cli.main")

    def raise_not_found(_name: str) -> str:
        raise PackageNotFoundError

    monkeypatch.setattr(main_module, "version", raise_not_found)

    result = cli_runner.invoke(forge_app, ["--version"])
    assert result.exit_code == 0, result.output
    assert "0.0.0.dev0" in result.output


# =============================================================================
# Layer 2 — the exception handler (_handle_exception)
# =============================================================================
# These tests call ``_handle_exception`` directly. They do *not* go
# through ``CliRunner``, because Typer's default error handling intercepts
# exceptions before the handler runs when the app is invoked directly.
# Testing the handler in isolation proves it renders correctly, which is
# what the handler is for.


# -----------------------------------------------------------------------------
# Coverage: ForgeError renders as a message, not a traceback
# -----------------------------------------------------------------------------
@pytest.mark.unit()
def test_forge_error_renders_message_and_hint(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``_handle_exception`` renders a ``ForgeError`` as ``Error:`` + hint.

    The handler is the function that produces user-facing output. It
    must render a single, actionable message and exit with the error's
    declared exit code. The hint must be visible because the hint is the
    *remediation* — the whole point of a good error message.
    """
    exc = UserInputError(
        "project name is required",
        hint="pass --name my-project",
    )
    with pytest.raises(typer.Exit) as exc_info:
        _handle_exception(exc, debug=False)

    assert exc_info.value.exit_code == ExitCode.USER_ERROR

    captured = capsys.readouterr()
    assert "Error:" in captured.out
    assert "project name is required" in captured.out
    assert "Hint:" in captured.out
    assert "pass --name my-project" in captured.out
    _assert_no_traceback(captured.out)


# -----------------------------------------------------------------------------
# Coverage: unexpected exceptions render a generic message, exit 5
# -----------------------------------------------------------------------------
@pytest.mark.unit()
def test_unexpected_error_renders_generic_message(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``_handle_exception`` renders an unexpected exception generically.

    The user must not see a raw traceback, and must not see internal
    details (class names, stack frames) that could hint at a
    vulnerability. The message must point to the issue tracker so the
    user has a next step.
    """
    exc = RuntimeError("boom")
    with pytest.raises(typer.Exit) as exc_info:
        _handle_exception(exc, debug=False)

    assert exc_info.value.exit_code == ExitCode.INTERNAL_ERROR

    captured = capsys.readouterr()
    assert "unexpected internal error" in captured.out.lower()
    assert "RuntimeError" not in captured.out
    assert "boom" not in captured.out  # No internal details leak
    _assert_no_traceback(captured.out)


# -----------------------------------------------------------------------------
# Coverage: KeyboardInterrupt exits cleanly
# -----------------------------------------------------------------------------
@pytest.mark.unit()
def test_keyboard_interrupt_exits_cleanly(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``_handle_exception`` renders ``Cancelled.`` for KeyboardInterrupt.

    Users who press Ctrl+C must not see a traceback and must not get a
    success exit code (which would hide the cancellation from scripts).
    Exit code 1 is the conventional "interrupted" code.
    """
    with pytest.raises(typer.Exit) as exc_info:
        _handle_exception(KeyboardInterrupt(), debug=False)

    assert exc_info.value.exit_code == ExitCode.USER_ERROR

    captured = capsys.readouterr()
    assert "Cancelled" in captured.out
    _assert_no_traceback(captured.out)


# -----------------------------------------------------------------------------
# Coverage: --debug re-raises exceptions for contributors
# -----------------------------------------------------------------------------
@pytest.mark.unit()
def test_debug_flag_re_raises_exceptions() -> None:
    """With ``debug=True``, ``_handle_exception`` re-raises the exception.

    ``--debug`` is the escape hatch for contributors. It must bypass the
    friendly error renderer entirely, so the traceback is visible.
    """
    exc = UserInputError("boom")
    with pytest.raises(UserInputError) as exc_info:
        _handle_exception(exc, debug=True)

    # The exact instance is re-raised, not wrapped or copied.
    assert exc_info.value is exc


# =============================================================================
# Layer 3 — the entry point (main)
# =============================================================================


# -----------------------------------------------------------------------------
# Acceptance criterion 5 — python -m forge.cli works
# -----------------------------------------------------------------------------
@pytest.mark.integration()
def test_python_m_entry_point(forge_executable: str) -> None:
    """``python -m forge.cli ping`` works as an alternate entry point.

    This is the fallback for environments where the ``forge`` console
    script is not on ``PATH`` (Windows, CI containers, contributors
    running from source without an editable install). It must produce
    identical output to ``forge ping``.

    The ``forge_executable`` fixture provides the command prefix:
    either the real ``forge`` binary (if it is on ``PATH``) or a
    ``python -m forge.cli`` fallback. Splitting it into tokens lets
    ``subprocess.run`` receive the exact argv it needs.
    """
    result = subprocess.run(
        [*forge_executable.split(), "ping"],  # noqa: S603 — argv is fixed and hardcoded
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert result.stdout.strip() == "pong"
